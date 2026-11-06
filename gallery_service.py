"""本地图库：把她见过的图、表情包，连描述一起收进 Q 盘。

为什么要单独一层，而不是塞进 ``sticker_service``：

- ``sticker.json`` 管的是「她要**发**的表情包」，每个条目带情绪和用法，
  是为了「按感觉找一张甩出去」优化的；
- 图库管的是「她**见过**的图」，是素材本身，除了发，还要能当头像、
  能回看、能被检索。两张表的生命周期不一样：表情包会随手删，图库
  希望尽量留着。

存法（都在 ``<data>/gallery/`` 下）::

    blobs/<md5>.<ext>     原图，按内容去重，同一张图只落一份
    index.json            元数据，id → {md5, file, desc, kind, ...}

**钱只花在描述上，而且只花一次**。收图时描述已经在识图那步拿过了，
这里直接复用（``desc`` 参数），不额外调模型；只有库里出现「有图没描述」
的老条目时，才由 ``describe_missing`` 批量补——那时也先查
``vision.peek`` 的缓存，同一张图绝不问第二遍。

## 搬到这个仓库时改的四处

原实现跑在另一个分支的插件里，依赖两个宿主侧的东西，这里都换掉了：

* ``vision_service.fetch_bytes`` → 自带的 ``_fetch_bytes``（httpx + file://）；
* ``vision_service.describe`` → 插件的 ``_vlm_describe_locator``，复用既有模型配置，
  不另拉一份；
* ``vision.describe`` 的缓存查询（``peek``）→ 不需要了：只有 ``desc`` 为空时才补，
  同一张图本来就不会问第二遍；
* ``client.get_qq_avatar()`` → 直连 qlogo CDN。**上游连接器没有这个接口**
  （部分 OneBot 实现才有），而反查头像必须能拿到当前头像，所以改成拼公开 URL。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Optional

BROWSE_TOOL_NAME = "browse_gallery"
AVATAR_TOOL_NAME = "set_avatar"

#: QQ 头像最大也就显示那么大，存太大的原图没意义，也拖慢上传。
_AVATAR_SIDE = 640

#: 补描述用的提示词。要的是"以后能靠它找到这张图"的那种描述，
#: 不是逐像素的复述——搜库的人不会去搜"左边有一块灰色矩形"。
_DESCRIBE_PROMPT = (
    "用一句不超过 20 字的中文描述这张图的内容和情绪，"
    "像给表情包起名一样（例如：猫耳少年一脸无语、卡通小狗点赞）。"
)


def _ext_from_bytes(data: bytes) -> str:
    """按魔数判扩展名。QQ 返回的 Content-Type 常常是 octet-stream，不能信。"""
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return "jpg"


class QQGalleryService:
    """图库。所有方法都假设跑在插件事件循环里。"""

    INDEX_VERSION = 1
    #: 上限。一张 200KB 算，1200 张就是 240MB，差不多是能接受的尽头。
    MAX_ITEMS = 1200
    #: 落盘去抖：收图是很频繁的事，不能每张都写一次 JSON。
    SAVE_DEBOUNCE_SECONDS = 3.0
    #: 一次补描述最多补几张。补描述要花钱，不能一口气补两百张。
    DESCRIBE_BATCH = 6
    #: 换头像最多试几次。QQ 那边偶发「回执 ok 但没落地」，重试能过。
    #: 每轮顺延等 1.5s / 3.0s / 4.5s 再反查，最坏十秒出头——换头像是低频操作，值。
    AVATAR_ATTEMPTS = 3

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.logger = getattr(plugin, "logger", None)
        self._items: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self._loaded = False
        self._dirty = False
        self._save_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        #: 吞异常必须留数：分不清「没权限」「本来就没有」「调用失败」，
        #: 事后就只剩一句「图库是空的」，看不出发生了什么。
        self._diag: dict[str, int] = {
            "remember_ok": 0,
            "remember_dup": 0,
            "remember_fail": 0,
            "fetch_fail": 0,
            "describe_ok": 0,
            "describe_fail": 0,
            "avatar_ok": 0,
            "avatar_fail": 0,
            "avatar_retry": 0,
            "avatar_unverified": 0,
            #: 单次 set_qq_avatar 调用抛异常的次数（重试前的原始失败）
            "avatar_err": 0,
        }

    # ── 路径 ────────────────────────────────────────────────

    def root_dir(self) -> Path:
        return Path(self.plugin.data_path()) / "gallery"

    def blobs_dir(self) -> Path:
        d = self.root_dir() / "blobs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self) -> Path:
        return self.root_dir() / "index.json"

    def inbox_dir(self) -> Path:
        """投喂目录：往里丢图，下次启动 / 调 import_inbox 就进库。

        做成目录而不是只留 entry，是因为「找几张好看的图当头像」这件事
        用资源管理器做最顺手——从浏览器另存、从聊天记录拖出来，都是一个
        动作，不该逼人先转成 base64 再走接口。
        """
        d = self.root_dir() / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        return d

    #: 认这些后缀。不是图片的东西放在 inbox 里会被跳过而不是报错。
    INBOX_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    async def import_inbox(self, *, limit: int = 40) -> dict[str, Any]:
        """把投喂目录里的图收进库，收完删掉源文件。"""
        # 不能无条件 await self.start()：start 末尾会调本函数，虽然那时
        # _loaded 已经置位不会真递归，但这种「靠调用顺序才不死」的写法
        # 太脆，显式判一下。
        if not self._loaded:
            await self.start()
        inbox = self.inbox_dir()
        imported = 0
        skipped = 0
        failed = 0
        try:
            entries = await asyncio.to_thread(lambda: sorted(inbox.iterdir()))
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    f"[Gallery] 读投喂目录失败: {type(exc).__name__}: {exc}"
                )
            return {"imported": 0, "skipped": 0, "failed": 0, "error": str(exc)}
        for path in entries:
            if imported >= max(1, int(limit or 40)):
                break
            try:
                if not path.is_file() or path.suffix.lower() not in self.INBOX_SUFFIXES:
                    skipped += 1
                    continue
                data = await asyncio.to_thread(path.read_bytes)
            except Exception:
                failed += 1
                continue
            item_id = await self.remember_bytes(
                data, kind="image", desc="", source="upload",
            )
            if not item_id:
                failed += 1
                continue
            imported += 1
            try:
                await asyncio.to_thread(path.unlink)
            except Exception:
                # 删不掉不算失败：图已经进库了，源文件留着只是难看。
                pass
        if imported and self.logger:
            self.logger.info(f"[Gallery] 投喂目录导入 {imported} 张")
        return {
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "inbox": str(inbox),
        }

    # ── 载入 / 落盘 ─────────────────────────────────────────

    async def start(self) -> None:
        if self._loaded:
            return
        async with self._start_lock:
            if self._loaded:
                return
            self.root_dir().mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(self._load_sync)
            except Exception as exc:
                if self.logger:
                    self.logger.warning(f"[Gallery] 载入索引失败: {type(exc).__name__}: {exc}")
                self._loaded = True
        # 投喂目录里有图就顺手收进来。失败只记日志——图库读不了不该
        # 拖垮插件启动。
        try:
            result = await self.import_inbox()
            if result.get("imported"):
                if self.logger:
                    self.logger.info(
                        f"[Gallery] 启动时从投喂目录收进 {result['imported']} 张"
                    )
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    f"[Gallery] 投喂目录导入失败: {type(exc).__name__}: {exc}"
                )
        if self.logger:
            with_desc = sum(1 for v in self._items.values() if v.get("desc"))
            self.logger.info(
                f"[Gallery] 已载入 {len(self._items)} 张图（{with_desc} 张有描述）"
            )

    def _load_sync(self) -> None:
        path = self._index_path()
        if not path.exists():
            self._loaded = True
            return
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            self._loaded = True
            return
        items = raw.get("items")
        self._items = dict(items) if isinstance(items, dict) else {}
        try:
            self._next_id = int(raw.get("next_id") or 1)
        except (TypeError, ValueError):
            self._next_id = len(self._items) + 1
        self._loaded = True

    def _save_sync(self) -> None:
        import json

        payload = {
            "version": self.INDEX_VERSION,
            "next_id": self._next_id,
            "items": self._items,
        }
        path = self._index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(path)
        self._dirty = False

    def _schedule_save(self) -> None:
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._save_task and not self._save_task.done():
            return
        self._save_task = loop.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        try:
            await asyncio.sleep(self.SAVE_DEBOUNCE_SECONDS)
            async with self._lock:
                if self._dirty:
                    await asyncio.to_thread(self._save_sync)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"[Gallery] 写盘失败: {type(exc).__name__}: {exc}")

    async def flush(self) -> None:
        if self._save_task and not self._save_task.done():
            # 写盘可能已进入工作线程，cancel 不会停止线程。先 join，避免
            # 两次保存争用同一个 index.json.tmp，或旧快照覆盖新索引。
            await self._save_task
        async with self._lock:
            if self._dirty:
                try:
                    await asyncio.to_thread(self._save_sync)
                except Exception as exc:
                    if self.logger:
                        self.logger.warning(f"[Gallery] 关闭前写盘失败: {type(exc).__name__}")

    async def remember_message(self, message: dict[str, Any], *, segments: Any = None) -> list[str]:
        """Collect inbound images after eligibility checks, reusing VLM descriptions."""
        if not isinstance(segments, list):
            from .enrichment import QQMessageEnricher

            segments = QQMessageEnricher._message_segments(message)
        images: dict[str, str] = {}
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            if kind not in {"image", "mface", "market_face"}:
                continue
            data = segment.get("data") or {}
            locator = str(data.get("url") or data.get("file") or "").strip()
            if locator:
                sticker = kind != "image" or str(data.get("subType", data.get("sub_type", ""))) == "1"
                images[locator] = "sticker" if sticker else "image"
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict) or attachment.get("type") not in {"image", "image_url"}:
                continue
            locator = str(attachment.get("url") or attachment.get("path") or attachment.get("file") or "").strip()
            if locator:
                images.setdefault(locator, "image")
        descriptions = message.get("_image_descriptions") or {}
        saved: list[str] = []
        for locator, kind in list(images.items())[:3]:
            item_id = await self.remember(
                locator, kind=kind, desc=str(descriptions.get(locator) or ""),
                sender_id=str(message.get("user_id") or ""),
                sender_name=str(message.get("user_nickname") or ""),
            )
            if item_id:
                saved.append(item_id)
        return saved

    # ── 收图 ────────────────────────────────────────────────

    # ── 取图 / 描述（宿主无关的两件小工具）───────────────

    async def _fetch_bytes(self, locator: str, *, timeout: float = 15.0) -> bytes:
        """取一张图的字节。认 http(s)、``file:///`` 和裸路径。

        为什么不复用别的下载实现：插件里没有通用的"给我这张图的字节"入口
        （``_vlm_describe_locator`` 内部自己下，但不外露，而且它只回描述）。
        连接器的上传是另一条路（走 OneBot 的 base64），也不共用。

        取不到一律回空 bytes 而不是抛异常：调用方（收图、头像反查）都把"取不到"
        当成一次可计数的失败，不需要区分是网络问题还是文件没了。
        """
        text = str(locator or "").strip()
        if not text:
            return b""
        if text.startswith("file://"):
            from urllib.parse import unquote, urlparse

            parsed = urlparse(text)
            path_text = unquote(parsed.path)
            if parsed.netloc and parsed.netloc != "localhost":
                path_text = f"//{parsed.netloc}{path_text}"
            elif re.match(r"^/[A-Za-z]:/", path_text):
                path_text = path_text[1:]
            path = Path(path_text)
            try:
                return await asyncio.to_thread(path.read_bytes)
            except Exception:
                return b""
        if text.startswith(("http://", "https://")):
            try:
                import httpx

                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as client:
                    resp = await client.get(text)
                    resp.raise_for_status()
                    return resp.content
            except Exception as exc:
                if self.logger:
                    self.logger.debug(f"[Gallery] 下载失败 {text[:60]}: {type(exc).__name__}")
                return b""
        path = Path(text)
        if path.is_file():
            try:
                return await asyncio.to_thread(path.read_bytes)
            except Exception:
                return b""
        return b""

    async def _describe(self, locator: str) -> str:
        """让 VLM 写一句能当检索词的描述。

        走插件里那个共用助手（``_vlm_describe_locator``），不在这里另读一次模型
        配置——表情包自动描述用的也是它，两处配置漂移过一次就再也对不上了。
        """
        helper = getattr(self.plugin, "_vlm_describe_locator", None)
        if not callable(helper):
            return ""
        try:
            desc = await helper(locator, prompt=_DESCRIBE_PROMPT, max_tokens=60)
        except Exception as exc:
            if self.logger:
                self.logger.debug(f"[Gallery] 描述失败: {type(exc).__name__}: {exc}")
            return ""
        return str(desc or "").strip()

    async def remember(
        self,
        locator: str,
        *,
        kind: str = "image",
        desc: str = "",
        source: str = "incoming",
        sender_id: str = "",
        sender_name: str = "",
    ) -> Optional[str]:
        """把一张图收进图库，返回 id；收不下就返回 None。

        ``desc`` 传已有的描述（识图那步刚拿到的），这里不会再多花一次
        VLM 调用。真没有描述也能存，``describe_missing`` 事后补。
        """
        locator = str(locator or "").strip()
        if not locator:
            return None
        await self.start()
        try:
            data = await self._fetch_bytes(locator, timeout=15.0)
        except Exception as exc:
            self._diag["fetch_fail"] += 1
            if self.logger:
                self.logger.info(f"[Gallery] 取图失败，跳过入库: {type(exc).__name__}: {exc}")
            return None
        if not data:
            self._diag["fetch_fail"] += 1
            return None
        return await self.remember_bytes(
            data,
            kind=kind,
            desc=desc,
            source=source,
            sender_id=sender_id,
            sender_name=sender_name,
        )

    async def remember_bytes(
        self,
        data: bytes,
        *,
        kind: str = "image",
        desc: str = "",
        source: str = "incoming",
        sender_id: str = "",
        sender_name: str = "",
    ) -> Optional[str]:
        """已经拿到字节时走这条（UI 上传、本地文件都复用）。"""
        if not data:
            return None
        await self.start()
        md5 = hashlib.md5(data).hexdigest()
        async with self._lock:
            # 去重：同一张图群里转二十次也只占一份，但次数要记——
            # 「这张又出现了」本身就是有用的信息。
            for item_id, item in self._items.items():
                if item.get("md5") != md5:
                    continue
                item["times"] = int(item.get("times") or 1) + 1
                item["last_seen"] = time.time()
                if desc and not item.get("desc"):
                    item["desc"] = desc
                self._diag["remember_dup"] += 1
                self._schedule_save()
                return item_id
            if len(self._items) >= self.MAX_ITEMS:
                self._evict_one()
            ext = _ext_from_bytes(data)
            name = f"{md5}.{ext}"
            target = self.blobs_dir() / name
            try:
                if not target.exists():
                    await asyncio.to_thread(target.write_bytes, data)
            except Exception as exc:
                self._diag["remember_fail"] += 1
                if self.logger:
                    self.logger.warning(
                        f"[Gallery] 写图失败: {type(exc).__name__}: {exc}"
                    )
                return None
            item_id = f"g{self._next_id}"
            self._next_id += 1
            now = time.time()
            self._items[item_id] = {
                "md5": md5,
                "file": name,
                "kind": "sticker" if kind == "sticker" else "image",
                "desc": str(desc or "").strip(),
                "source": source,
                "sender": str(sender_id or ""),
                "sender_name": str(sender_name or ""),
                "added_at": now,
                "last_seen": now,
                "times": 1,
                "bytes": len(data),
                "avatar_at": 0.0,
            }
            self._diag["remember_ok"] += 1
            self._schedule_save()
            if self.logger:
                brief = str(desc or "").strip()[:30] or "（还没描述）"
                self.logger.info(f"[Gallery] 收进第 {item_id} 张: {brief}")
        return item_id

    def _evict_one(self) -> None:
        """满了先淘汰「没描述、也从没被用过」的老图。

        有描述的条目是检索的原料，用过的（发过/当过头像）是她的偏好，
        都往后排；实在只剩这两种了，才动最老的一张。
        """
        def rank(item_id: str) -> tuple:
            item = self._items.get(item_id) or {}
            useful = 1 if item.get("desc") else 0
            used = 1 if (item.get("avatar_at") or item.get("sent")) else 0
            # 第二维用 added_at 原值：min 取最小 → 同优先级里淘汰**最老**的。
            # 写成 -added_at 会淘汰最新的，跟"越老越该让位"正好相反。
            return (useful + used, float(item.get("added_at") or 0))

        victim = min(self._items, key=rank, default=None)
        if victim:
            self._items.pop(victim, None)

    # ── 查询 ────────────────────────────────────────────────

    def _sorted_items(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted(
            self._items.items(),
            key=lambda kv: -float(kv[1].get("added_at") or 0),
        )

    def list_items(
        self, *, kind: str = "", query: str = "", limit: int = 20,
        with_desc_only: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 20), 100))
        query = str(query or "").strip()
        out: list[dict[str, Any]] = []
        for item_id, item in self._sorted_items():
            if kind and str(item.get("kind") or "") != kind:
                continue
            if with_desc_only and not item.get("desc"):
                continue
            if query and not self._matches(item, query):
                continue
            row = dict(item)
            row["id"] = item_id
            out.append(row)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _matches(item: dict[str, Any], query: str) -> bool:
        desc = str(item.get("desc") or "")
        if not desc:
            return False
        if query in desc:
            return True
        # 中文没有空格，2-gram 是最省事的「部分命中」。
        q = query.strip()
        if len(q) < 2:
            return False
        grams = {q[i: i + 2] for i in range(len(q) - 1)}
        return any(g in desc for g in grams)

    def resolve(self, key: str) -> Optional[tuple[str, dict[str, Any]]]:
        """``g3`` / 描述关键词 / 序号 都能取到同一张。"""
        key = str(key or "").strip()
        if not key:
            return None
        if key in self._items:
            return key, self._items[key]
        # 允许只写数字：她说「第 3 张」也能对
        if key.isdigit():
            candidate = f"g{key}"
            if candidate in self._items:
                return candidate, self._items[candidate]
        rows = self.list_items(query=key, limit=1)
        if rows:
            return rows[0]["id"], self._items[rows[0]["id"]]
        return None

    def path_of(self, item: dict[str, Any]) -> Optional[Path]:
        name = str(item.get("file") or "").strip()
        if not name:
            return None
        path = self.blobs_dir() / name
        return path if path.exists() else None

    def stats(self) -> dict[str, Any]:
        total = len(self._items)
        with_desc = sum(1 for v in self._items.values() if v.get("desc"))
        stickers = sum(1 for v in self._items.values() if v.get("kind") == "sticker")
        size = 0
        for item in self._items.values():
            try:
                size += int(item.get("bytes") or 0)
            except (TypeError, ValueError):
                pass
        return {
            "total": total,
            "with_desc": with_desc,
            "stickers": stickers,
            "images": total - stickers,
            "bytes": size,
            "root": str(self.root_dir()),
            "diag": dict(self._diag),
        }

    def catalog_text(self, *, limit: int = 8) -> str:
        """给模型看的清单：编号 + 描述。只列有描述的。"""
        rows = self.list_items(limit=limit, with_desc_only=True)
        if not rows:
            return ""
        lines = []
        for row in rows:
            tag = "表情" if row.get("kind") == "sticker" else "图"
            desc = str(row.get("desc") or "").strip()
            desc = re.sub(r"\s+", " ", desc)[:70]
            lines.append(f"[{row['id']}]（{tag}）{desc}")
        return "\n".join(lines)

    # ── 补描述 ──────────────────────────────────────────────

    async def describe_missing(self, limit: int = DESCRIBE_BATCH) -> int:
        """给「有图没描述」的老条目补描述。先查缓存，缓存没有才花钱。"""
        await self.start()
        todo = [
            (item_id, item)
            for item_id, item in self._sorted_items()
            if not item.get("desc")
        ][: max(1, int(limit or self.DESCRIBE_BATCH))]
        done = 0
        for item_id, item in todo:
            path = self.path_of(item)
            if path is None:
                continue
            try:
                desc = await asyncio.wait_for(
                    self._describe(f"file:///{path.as_posix()}"),
                    timeout=25.0,
                )
            except Exception as exc:
                self._diag["describe_fail"] += 1
                if self.logger:
                    self.logger.info(
                        f"[Gallery] 补描述失败 {item_id}: {type(exc).__name__}"
                    )
                continue
            if not desc:
                self._diag["describe_fail"] += 1
                continue
            item["desc"] = desc
            self._diag["describe_ok"] += 1
            done += 1
            self._schedule_save()
        return done

    # ── 换头像 ──────────────────────────────────────────────

    def prepare_avatar_file(self, item: dict[str, Any]) -> Optional[Path]:
        """把原图弄成一张方形 jpg。QQ 那边不做裁切，非方图上去会压扁。"""
        path = self.path_of(item)
        if path is None:
            return None
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(path) as image:
                if getattr(image, "is_animated", False):
                    image.seek(0)
                image = image.convert("RGB")
                side = min(image.size)
                if side <= 0:
                    return None
                left = (image.width - side) // 2
                top = (image.height - side) // 2
                image = image.crop((left, top, left + side, top + side))
                if side > _AVATAR_SIDE:
                    image = image.resize((_AVATAR_SIDE, _AVATAR_SIDE), Image.LANCZOS)
                buf = BytesIO()
                image.save(buf, format="JPEG", quality=92)
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    f"[Gallery] 头像预处理失败: {type(exc).__name__}: {exc}"
                )
            return None
        target = self.root_dir() / f"avatar_{item.get('md5', 'x')[:12]}.jpg"
        try:
            target.write_bytes(buf.getvalue())
        except Exception:
            return None
        return target

    async def set_avatar(self, key: str) -> dict[str, Any]:
        """把图库里的一张设成 QQ 头像。

        ``set_qq_avatar`` 回 ``status=ok`` 只代表 Highway 上传成功——
        QQ 那边有没有真的换上，它不保证（2026-11-01 实测：同样的调用、
        同样的文件，13:24/13:26 两次没落地，13:32 之后连续三次都成功，
        中间没改过任何配置，纯偶发）。所以这里**每次设完都反查一遍**，
        没落地就自动重来，最多 :attr:`AVATAR_ATTEMPTS` 次。
        """
        await self.start()
        found = self.resolve(key)
        if not found:
            return {"ok": False, "reason": "图库里没有这一张", "key": str(key or "")}
        item_id, item = found
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return {"ok": False, "reason": "QQ 还没连上", "id": item_id}
        avatar_path = await asyncio.to_thread(self.prepare_avatar_file, item)
        if avatar_path is None:
            return {"ok": False, "reason": "这张图的格式处理不了", "id": item_id}
        locator = f"file:///{avatar_path.as_posix()}"
        result: dict[str, Any] = {}
        verified: Optional[bool] = None
        attempts_used = 0
        call_ok = False
        for attempt in range(1, self.AVATAR_ATTEMPTS + 1):
            attempts_used = attempt
            try:
                result = await client.set_qq_avatar(locator)
                call_ok = True
            except Exception as exc:
                # 只记「这一次调用挂了」；总的成败在循环外面定，
                # 否则每次重试都 +1，三次全败会数出 4 次失败，看日志会误判。
                self._diag["avatar_err"] += 1
                if self.logger:
                    self.logger.warning(
                        f"[Gallery] 换头像第 {attempt} 次调用失败: {type(exc).__name__}: {exc}"
                    )
                continue
            status = str((result or {}).get("status") or "").lower()
            # call_action 只把 response["data"] 回传上来，那里面**没有 status 字段**
            # （status=failed 会在 call_action 内部抛异常，能走到这里就是 QQ 侧已确认）。
            # 以前在这里判 status == "ok"，恒为 False —— 头像明明换好了，却回一句
            # 「没换成」，模型于是不敢跟对方提，看着就像"没换上"。
            if status == "failed":
                if self.logger:
                    self.logger.warning(f"[Gallery] 换头像第 {attempt} 次被拒: {result}")
                continue
            # 回 ok 不代表真换了，反查一次。
            await asyncio.sleep(1.5 * attempt)
            verified = await self._avatar_landed(client, avatar_path)
            if self.logger:
                state = {True: "已落地", False: "没落地", None: "查不了"}[verified]
                self.logger.info(
                    f"[Gallery] 换头像 {item_id} 第 {attempt} 次: 回执 ok，反查{state} "
                    f"(status={status or 'n/a'})"
                )
            if verified is not False:
                break
            self._diag["avatar_retry"] += 1

        # set_qq_avatar 成功时 data 是空 dict（结果全靠异常表达），
        # 所以不能拿 bool(result) 判成功——那是把「成功」判成失败的老坑换了个皮。
        ok = call_ok and str((result or {}).get("status") or "").lower() != "failed"
        if ok:
            self._diag["avatar_ok"] += 1
            item["avatar_at"] = time.time()
            self._schedule_save()
            if verified is None:
                self._diag["avatar_unverified"] += 1
        else:
            self._diag["avatar_fail"] += 1
        return {
            "ok": ok,
            "id": item_id,
            "desc": str(item.get("desc") or "")[:60],
            "status": str((result or {}).get("status") or "").lower(),
            "verified": verified,
            "attempts": attempts_used,
            "message": str((result or {}).get("message") or (result or {}).get("wording") or ""),
        }

    async def _avatar_landed(self, client: Any, avatar_path: Path) -> Optional[bool]:
        """反查 QQ 侧当前头像，跟本地这张比。``None`` = 查不了（不当失败）。"""
        try:
            uin = str(getattr(client, "self_id", "") or "").strip()
            if not uin:
                return None
            # 上游连接器没有 get_qq_avatar（只有部分 OneBot 实现提供），所以直接
            # 问 qlogo：这是 QQ 头像的公开 CDN，知道自己的 uin 就能取，不需要
            # 任何鉴权。省掉一次不确定的 action 调用。
            #
            # 参数挂一个变动值是因为它的缓存很黏：不挂的话，刚换完立刻去拉，
            # 很可能还是上一张，反查就成了"永远没落地"。
            url = (
                f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"
                f"&_={int(time.time() * 1000)}"
            )
            data = await self._fetch_bytes(url, timeout=12.0)
            if not data:
                return None
            return await asyncio.to_thread(self._same_picture, avatar_path, data)
        except Exception as exc:
            if self.logger:
                self.logger.debug(f"[Gallery] 头像反查失败: {type(exc).__name__}: {exc}")
            return None

    @staticmethod
    def _dhash(image: Any, size: int = 8) -> int:
        """差值哈希。QQ 会把头像重新压一遍，逐像素比不了，这个够糙也够稳。"""
        from PIL import Image

        small = image.convert("L").resize((size + 1, size), Image.LANCZOS)
        pixels = list(small.getdata())
        bits = 0
        index = 0
        for row in range(size):
            base = row * (size + 1)
            for col in range(size):
                if pixels[base + col] > pixels[base + col + 1]:
                    bits |= 1 << index
                index += 1
        return bits

    @classmethod
    def _same_picture(cls, local_path: Path, remote_bytes: bytes, threshold: int = 12) -> bool:
        from io import BytesIO

        from PIL import Image

        with Image.open(local_path) as left, Image.open(BytesIO(remote_bytes)) as right:
            distance = bin(cls._dhash(left) ^ cls._dhash(right)).count("1")
        return distance <= threshold

    # ── 工具定义 ────────────────────────────────────────────

    def build_browse_definition(self):
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name=BROWSE_TOOL_NAME,
            description=(
                "翻自己的图库。库里存着对方发过、你收下的图和表情包，每条都带描述。"
                "想看有什么素材、想找特定的图、想挑一张当头像，都用它。"
                "不带关键字就返回最近收到的几张。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "想找什么样的图，比如'猫耳''无语''Q版'。留空就是最近几张",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "看几张，默认 8，最多 20",
                    },
                },
                "required": [],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    def build_avatar_definition(self):
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name=AVATAR_TOOL_NAME,
            description=(
                "把自己的 QQ 头像换成图库里的某一张。"
                "**对方让你换头像、或者发图说“你用这张当头像”“天使这个”时，必须调用它**，"
                "只在回复里说「好」「换了」不算做到。"
                "id 填图库编号（比如 g3），也可以直接填描述里的关键词。"
                "不确定编号就先 browse_gallery 看一眼。"
                "换头像是显眼的事，成功后用一句话告诉对方。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "图库编号 g3，或描述关键词（如'猫耳 Q版'）",
                    },
                },
                "required": ["id"],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )
