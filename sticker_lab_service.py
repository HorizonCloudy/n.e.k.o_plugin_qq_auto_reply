"""表情包：她自己在聊天里收，收哪些、从哪个群收，都归一个三档开关管。

上游的表情包库只有「用户手动上传」一条入口——她手上有什么全看用户传了什么，
所以提示词里的 ``{sticker_catalog}`` 常年是空的，「会用表情包」这个能力等于不存在。

这里补的是**入口**：收到图之后她可以自己收进库。但群聊里什么图都有（别人的自拍、
工作截图、二维码），她能判断"这张好玩"，判断不了"这张能不能当表情包发到别的群"。
所以把「收」这个动作放在三档闸后面（``sticker_collect_policy``）：

* ``off``     不许收，只能用用户手动传的；
* ``current`` 只收**当前这个会话**里收到的（默认）；
* ``any``     任何群、私聊收到的都能收。

默认 ``current`` 的理由：在 A 群看到的表情用在 A 群是安全的；跨群搬运等于"把 A 群
的东西带到 B 群"，那是使用者的决定，不该由模型替她做。

数据结构与上游完全一致（``data/sticker.json`` → ``{"1": {"desc", "path"}}``，
图在 ``data/sticker/``），只往条目里多加几个字段，旧库直接能用、UI 面板照旧能看。

**发送不在这里。** 上游已经有 ``<sticker>ID</sticker>`` 的投递链路
（``reply_delivery_node._send_sticker``），模型看到目录后自己写标签就行，
这里只负责让目录里有东西。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx

from .sticker_scope import collection_scope, entry_scope

#: 收集策略。名字与 ``settings_schema.STICKER`` 的 enum 严格一致。
POLICY_OFF = "off"
POLICY_CURRENT = "current"
POLICY_ANY = "any"
_POLICIES = (POLICY_OFF, POLICY_CURRENT, POLICY_ANY)
DEFAULT_POLICY = POLICY_CURRENT

#: 单张图上限。表情包是随手发的东西，6MB 已经足够，再大就不是表情了。
_MAX_BYTES = 6 * 1024 * 1024

#: 描述长度上限。这句话会进提示词目录，也会进工具返回值，一涨就是几百字上下文。
_MAX_DESC = 60

#: 最近媒体缓冲长度。只存在内存里——URL 带 rkey，几小时就失效，落盘没有意义。
_MAX_RECENT = 24

#: 图的有效期。QQ 的图 URL 带 rkey，几小时就变成死链；而且"她说的这张图"在
#: 半小时之后基本已经不是同一件事了。超过这个窗口的图不再算"刚收到的"，
#: 也就不再为它挂工具（挂工具会给整轮加超时预算，见 ``has_fresh``）。
_FRESH_SECONDS = 1800.0

#: 描述请求的超时。**必须大于 ``_vlm_describe_locator`` 内部的 15 秒**，否则
#: 外层会先抢跑成"超时"，把一次本来能拿到结果的看图判成失败。
_DESCRIBE_TIMEOUT = 18.0

#: 一次最多收几张。上限定 2 而不是更多：每张都是「下载 + 看图」两次往返，
#: 收 3 张就会把这一轮的超时预算（``STICKER_TOOL_BUDGET_SECONDS``）撑爆，
#: 而"把刚才那几张全存了"在真实对话里几乎不出现。
_MAX_SAVE_COUNT = 2

#: 描述提示词。要的是"这张图什么情绪、画了什么"，不是客观描述一张图片。
_STICKER_VLM_PROMPT = (
    "这是一张聊天里用的表情包。用一句中文说清它画了什么、传达什么情绪，"
    "不超过 20 个字，不要引号，不要以「这张图」开头。"
)

# 收到表情包时的段类型。LLBot 上报 ``market_face``，NapCat 老格式是 ``mface``。
_IN_FACE_TYPES = ("market_face", "mface")


def _is_hex32(text: str) -> bool:
    return len(text) == 32 and all(c in "0123456789abcdef" for c in text.lower())


class _NullLogger:
    """``plugin.logger`` 可能是 None（上游别处也是先判空再用）。

    收集侧的日志大多写在 ``except`` 分支里——那里再抛一次 AttributeError，
    异常就会从 ``observe`` 冒到消息分发路径上，为了一行日志把消息处理带崩。
    """

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass


def _normalize_local(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """``image`` 段里挑出**收藏表情**。普通照片返回 None。

    判据只有一个：``subType == 1``。LLBot 解析 QQ 的 ``customFace`` 时直接降级成
    普通图片段（``elem.customFace → ElementType.Pic``），emoji_id / key 全丢，
    只剩一个 md5 文件名和一条带 rkey 的 URL —— 这个字段是唯一还留着的标记。
    """
    try:
        sub_type = int(data.get("subType", data.get("sub_type", 0)) or 0)
    except (TypeError, ValueError):
        sub_type = 0
    if sub_type != 1:
        return None
    raw_file = str(data.get("file") or "").strip()
    url = str(data.get("url") or "").strip()
    name = raw_file.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _dot, ext = name.partition(".")
    # QQ 的图片文件名就是这张图的 md5（大写）。同一张表情每次转发都带同一个
    # 文件名，所以拿它当去重键比 URL 稳——URL 里的 rkey 几个小时就换一茬。
    md5 = stem.lower() if _is_hex32(stem) else ""
    if not md5:
        md5 = hashlib.md5((url or raw_file or "").encode("utf-8")).hexdigest()
    return {
        "mode": "local",
        "md5": md5,
        "url": url,
        "file": name,
        "ext": ("." + ext.lower()) if ext else ".jpg",
    }


def normalize_segment(segment: Any) -> Optional[dict[str, Any]]:
    """把两种命名的表情段归一成同一份字段；不像表情包就返回 None。"""
    if not isinstance(segment, dict):
        return None
    seg_type = str(segment.get("type") or "")
    data = segment.get("data")
    if not isinstance(data, dict):
        return None
    if seg_type == "image":
        return _normalize_local(data)
    if seg_type not in _IN_FACE_TYPES:
        return None

    def pick(*names: str) -> Any:
        for name in names:
            value = data.get(name)
            if value not in (None, ""):
                return value
        return None

    emoji_id = str(pick("emoji_id", "emojiId") or "").strip()
    if not emoji_id:
        return None
    try:
        pkg = int(pick("emoji_package_id", "emojiPackageId") or 0)
    except (TypeError, ValueError):
        pkg = 0
    return {
        "mode": "mface",
        "emoji_id": emoji_id,
        "pkg": pkg,
        "key": str(pick("key") or "").strip(),
        "summary": str(pick("summary") or "").strip(),
        "url": str(pick("url") or "").strip(),
    }


class QQStickerLab:
    """表情包的收集侧：记下收到了什么、按闸决定能不能收、收进来就落盘入库。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        #: 取一次就固定，别在每个 except 里再 getattr 一遍。
        self.logger = getattr(plugin, "logger", None) or _NullLogger()
        #: 最近收到的媒体（新的一头在右边）。locator 带时效，所以只在内存里。
        self._recent: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT)
        self._lock = asyncio.Lock()
        #: 每个可能要"静默失败"的环节都留计数。
        #: 不计数的话，功能空着和"本来就没有"长得一模一样，日志还一片安静。
        self.diag: dict[str, int] = {
            "seen": 0,
            "observe_error": 0,
            "policy_block": 0,
            "save_ok": 0,
            "dup": 0,
            "download_fail": 0,
            "describe_empty": 0,
            "write_fail": 0,
            "no_item": 0,
        }

    # ── 路径 ────────────────────────────────────────────────
    #
    # 一律走 ``data_path(name)``，和 UI 面板（``__init__.py`` 的 upload/delete）
    # 与提示词目录（``session_instruction_service._load_sticker_catalog``）同一
    # 个入口。自己拼 ``根 / "sticker.json"`` 在宿主换过实现之后就未必同源，
    # 而这里的产物要正好落在他们读写的那份文件上。

    @property
    def _index_path(self) -> Path:
        return Path(self.plugin.data_path("sticker.json"))

    @property
    def _dir(self) -> Path:
        return Path(self.plugin.data_path("sticker"))

    # ── 策略 ────────────────────────────────────────────────

    def policy(self) -> str:
        """当前收集范围。

        ``_apply_plain_settings`` 对 str 只做去空白、不校验 enum（见 settings_service），
        所以这里自己兜一层白名单：宁可退回安全的默认值，也不要让一个手改过的
        config 键把闸打开。
        """
        raw = ""
        try:
            raw = str((self.plugin._qq_settings or {}).get("sticker_collect_policy") or "")
        except Exception:
            raw = ""
        raw = raw.strip().lower()
        return raw if raw in _POLICIES else DEFAULT_POLICY

    def check(
        self, *, item_group: str, current_group: str,
        item_sender: str = "", current_sender: str = "",
    ) -> str:
        """能不能收这一张。允许返回空串；拒绝返回**给模型看的一句话**。

        返回话而不是布尔：模型收到"这张不是这个群里的，我不拿别处的东西"会照实
        回绝对方；收到一个 False 只会自己编理由。
        """
        policy = self.policy()
        if policy == POLICY_OFF:
            self.diag["policy_block"] += 1
            return "我这边没在收集表情包，用文字接话就行。"
        if policy == POLICY_ANY:
            return ""
        if str(item_group or "") != str(current_group or ""):
            self.diag["policy_block"] += 1
            return "这张不是这个群里的，我不拿别处的东西。"
        if not current_group and (not current_sender or item_sender != current_sender):
            self.diag["policy_block"] += 1
            return "这张不是这段私聊里的，我不拿别人的私聊图片。"
        return ""

    # ── 收流：只记，不下载不识别 ────────────────────────────

    def observe(self, message: Any) -> None:
        """收到消息时记一笔媒体。**同步、不落盘、不调模型**。

        收流路径上不能 await 任何网络动作：一条表情包拖慢的是整轮回复，
        而"她到底收不收"要等她看完上下文才决定。
        """
        try:
            if not isinstance(message, dict):
                return
            group_id = str(message.get("group_id") or "").strip()
            sender_id = str(message.get("user_id") or "").strip()
            segments = message.get("message")
            if not isinstance(segments, list):
                return
            now = time.time()
            for segment in segments:
                norm = normalize_segment(segment)
                if norm is None:
                    continue
                locator = str(norm.get("url") or "").strip()
                if not locator:
                    # 收藏表情偶尔只有 file（本地路径），那种也能收，但要走下载
                    # 分支以外的路径；这里先按"没有可下载地址"跳过并计数。
                    self.diag["download_fail"] += 1
                    continue
                self._recent.append({
                    "locator": locator,
                    "kind": norm["mode"],
                    "md5": str(norm.get("md5") or ""),
                    "summary": str(norm.get("summary") or ""),
                    "group_id": group_id,
                    "sender_id": sender_id,
                    "at": now,
                })
                self.diag["seen"] += 1
        except Exception:
            # 记录失败绝不影响消息处理本身。
            self.diag["observe_error"] += 1
            self.logger.debug("[Sticker] 记录最近媒体失败", exc_info=True)

    def recent(
        self,
        *,
        limit: int = 1,
        sender_id: str = "",
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """取最近媒体。group_id=None 不限会话，空串只匹配私聊。"""
        out: list[dict[str, Any]] = []
        for item in reversed(self._recent):
            if sender_id and item["sender_id"] != sender_id:
                continue
            if group_id is not None and item["group_id"] != group_id:
                continue
            if item["at"] < time.time() - _FRESH_SECONDS:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def has_fresh(self, window: float = _FRESH_SECONDS) -> bool:
        """最近 ``window`` 秒内有没有收到过图。决定**这一轮要不要挂工具**。

        工具不是常挂的：挂上它就会往 prompt 里追加工具轮指令、并给整轮多留
        一份超时预算（见 ``reply_generation_service._arm_turn_tools`` 的返回值）。
        一份"本轮已提供可调用工具"的指令写在根本没有工具可用的轮次上，等于
        每句话都在提醒她"你有工具"，模型会开始凭空调度不存在的能力。

        而收表情包这件事有个天然前提：**得先有人发图**。没有新图的时候，
        ``save_sticker`` 无论怎么描述都无事可做，挂着纯属负担。
        """
        if not self._recent:
            return False
        deadline = time.time() - window
        return any(item["at"] >= deadline for item in self._recent)

    def should_offer_save(self) -> bool:
        """这一轮该不该把 ``save_sticker`` 挂给模型。

        两个前提缺一不可：闸没关（``off`` 时收进来也没用），并且确实有新图。
        判据放在这里而不是生成层，是为了让"什么情况下她手上会有这个工具"
        只有一个出处——散在两层各判一半，迟早对不上。
        """
        if self.policy() == POLICY_OFF:
            return False
        return self.has_fresh()

    # ── 存取 ────────────────────────────────────────────────

    def _load_index(self) -> dict[str, Any]:
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.logger.warning(f"[Sticker] 读 sticker.json 失败: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _write_index(self, data: dict[str, Any]) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 目录是缓存的（session_instruction_service._sticker_catalog_cache），
        # 不清的话新收的表情要等到重启才进提示词。
        service = getattr(self.plugin, "session_instruction_service", None)
        if service is not None:
            try:
                service._sticker_catalog_cache = ""
            except Exception:
                pass

    async def _download(self, locator: str) -> bytes:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        timeout = httpx.Timeout(15.0, connect=8.0)
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=True,
        ) as client:
            resp = await client.get(locator)
            resp.raise_for_status()
            return resp.content

    @staticmethod
    def _guess_ext(raw: bytes) -> str:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if raw[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    async def describe(self, path: Path) -> str:
        """让看图模型说一句。失败返回空串（调用方决定兜底）。

        走插件既有的 ``_vlm_describe_locator``：插件里"看图"的地方共用这一条，
        另抄一份的话模型配置迟早会漂移。
        """
        fn = getattr(self.plugin, "_vlm_describe_locator", None)
        if not callable(fn):
            self.diag["describe_empty"] += 1
            return ""
        try:
            text = await asyncio.wait_for(
                fn(str(path), prompt=_STICKER_VLM_PROMPT, max_tokens=60),
                timeout=_DESCRIBE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.diag["describe_empty"] += 1
            self.logger.info("[Sticker] 看图超时，这张不入库")
            return ""
        except Exception as exc:
            self.diag["describe_empty"] += 1
            self.logger.info(
                f"[Sticker] 看图失败: {type(exc).__name__}: {exc}"
            )
            return ""
        text = " ".join(str(text or "").split()).strip().strip("\"'“”")
        return text[:_MAX_DESC]

    async def save(
        self,
        locator: str,
        *,
        note: str = "",
        origin: str = "",
        group_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """把一张图收进库；失败返回 None 并留下计数。

        ``note`` 是她自己给这张图起的名字。她自己说的通常比看图描述更贴她的用法
        （"就是那张无语的猫"），所以有 note 时放在前面。
        """
        url = str(locator or "").strip()
        if not url:
            self.diag["download_fail"] += 1
            return None

        try:
            raw = await self._download(url)
        except Exception as exc:
            self.diag["download_fail"] += 1
            self.logger.info(
                f"[Sticker] 下载失败: {type(exc).__name__}: {exc}"
            )
            return None
        if not raw:
            self.diag["download_fail"] += 1
            return None
        if len(raw) > _MAX_BYTES:
            self.diag["download_fail"] += 1
            self.logger.info(f"[Sticker] 图太大跳过: {len(raw)} bytes")
            return None

        digest = hashlib.md5(raw).hexdigest()[:12]
        scope = collection_scope(group_id=group_id, sender_id=origin, policy=self.policy())
        async with self._lock:
            data = self._load_index()
            # 同一作用域内去重；跨会话保留独立文件，避免删除互相影响。
            for sid, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                if digest in str(entry.get("path") or "") and entry_scope(entry) == scope:
                    self.diag["dup"] += 1
                    return {"id": sid, **entry}

            ext = self._guess_ext(raw)
            scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
            filename = f"sticker_{digest}_{scope_digest}{ext}"
            target = self._dir / filename
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_bytes, raw)
            except Exception:
                self.diag["write_fail"] += 1
                self.logger.warning("[Sticker] 写图片失败", exc_info=True)
                return None

            desc = await self.describe(target)
            if note:
                desc = f"{note} {desc}".strip()[:_MAX_DESC]
            if not desc:
                self.diag["describe_empty"] += 1
                self.logger.info("[Sticker] 描述为空且没备注，不入库")
                try:
                    target.unlink(missing_ok=True)
                except Exception:
                    pass
                return None

            next_id = 1
            while str(next_id) in data:
                next_id += 1
            sid = str(next_id)
            entry = {
                "desc": desc,
                "path": filename,
                "added_at": time.time(),
                "sent": 0,
                "last_sent_at": 0.0,
                "origin": str(origin or ""),
                "group": str(group_id or ""),
                "scope": scope,
            }
            data[sid] = entry
            try:
                await asyncio.to_thread(self._write_index, data)
            except Exception:
                self.diag["write_fail"] += 1
                self.logger.warning("[Sticker] 写索引失败", exc_info=True)
                return None

        self.diag["save_ok"] += 1
        self.logger.info(f"[Sticker] 收进第 {len(data)} 张: {desc[:30]}")
        return {"id": sid, **entry}

    # ── 工具 ────────────────────────────────────────────────

    def build_save_definition(self) -> Any:
        """``save_sticker``：把刚才看到的图收进表情包库。"""
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name="save_sticker",
            description=(
                "把聊天里刚收到的图收进自己的表情包库，以后能当表情包发出去。"
                "适合「这个好玩，存了」这种场面。收之前不用问对方。"
                "存的时候可以顺手给它起个名字（note），起过名字的更好找。"
                "不收自拍、工作截图、二维码、聊天记录或广告图。"
                "可以静默收藏；收到纯表情包时仍无需回复。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "给这张图起的名，比如「无语的猫」「猫猫点赞」。可不填。",
                    },
                    "count": {
                        "type": "integer",
                        "description": "收几张，默认 1，最多 2。",
                    },
                },
                "required": [],
            },
            metadata={"source": "qq_auto_reply"},
        )

    async def execute_save(
        self,
        arguments: Any,
        *,
        sender_id: str = "",
        group_id: str = "",
    ) -> str:
        """工具入口。返回的是**给她看的话**，她通常拿这句去回对方。"""
        args = arguments if isinstance(arguments, dict) else {}
        note = str(args.get("note") or "").strip()[:_MAX_DESC]
        try:
            count = int(args.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, _MAX_SAVE_COUNT))

        policy = self.policy()
        if policy == POLICY_OFF:
            self.diag["policy_block"] += 1
            return "我这边没在收集表情包，用文字接话就行。"

        # 找"要收哪一张"：先在当前会话里按人找，找不到再退到当前会话任何人。
        # 群聊里"这张存下来"经常指的不是当前发言者那张（A 发图、B 说这图好玩）。
        items = self.recent(limit=count, sender_id=sender_id, group_id=group_id)
        if not items and group_id:
            items = self.recent(limit=count, group_id=group_id)
        if not items and policy == POLICY_ANY:
            items = self.recent(limit=count)
        if not items:
            self.diag["no_item"] += 1
            return "最近没收到图，没东西可收。"

        saved: list[str] = []
        for item in items:
            refusal = self.check(
                item_group=str(item.get("group_id") or ""),
                current_group=group_id,
                item_sender=str(item.get("sender_id") or ""),
                current_sender=sender_id,
            )
            if refusal:
                return refusal
            entry = await self.save(
                str(item.get("locator") or ""),
                note=note,
                origin=str(item.get("sender_id") or ""),
                group_id=str(item.get("group_id") or ""),
            )
            if entry:
                saved.append(str(entry.get("desc") or "")[:24])

        if not saved:
            return "这张没存进去（可能刚收过了，或者没认出来画的是什么）。"

        total = len(self._load_index())
        return f"收好了，现在手上能用的表情包 {total} 张。这张是：{'；'.join(saved)}"

    def stats(self) -> dict[str, Any]:
        return {
            "policy": self.policy(),
            "recent": len(self._recent),
            "total": len(self._load_index()),
            "diag": dict(self.diag),
        }
