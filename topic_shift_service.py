"""转移话题与烂梗筛选：把"该换话题了""这是烂梗"从感觉变成计数。

之前提示词里只有一句静态规则（"同一话题来回超过 3 轮、或群友变敷衍，就换话题"），
但**没人替她数轮数** —— 模型对"这个话题已经绕了几遍"没有可靠感知，规则等于没写；
烂梗同理："别接烂梗"她听得懂，可哪些算烂梗、对方刚发的算不算，没有任何判定。

本服务补的就是判定：

* **观察**：dispatcher 在门控**之前**把每条群消息原文递进来（被闸掉的敷衍短回复
  恰恰是"话题干涸"的信号，观察不能只收放行的）。
* **干涸判定**（`is_dry`）：近 4 条里 ≥3 条共用同一个双字词 → 同一关键词绕圈；
  连续 3 条归一化后 ≤4 字 → 群友在敷衍。任一成立即"该换话题"。
* **指令注入**（`turn_directives`）：判定成立才在本轮提示词前贴一行
  「转移话题指令」，并带 **15 分钟冷却** —— 同一个干话题不连环催。
* **烂梗表**（`meme_hit` / `stale_meme_list` 设置）：用户维护的过时梗列表，
  归一化（剥 CQ 码/标签/标点/大小写，与出站去重同一份指纹）后做包含匹配。
  入站命中 → 本轮提示词告诉她"对方在玩烂梗，别接别复读"；
  出站命中 → `outbound_guard_service` 直接拦（跟着复读/转发/她的回复都不放）。

指令只管"该换了"和"这是烂梗"，**新话题内容由模型自己拿** —— 服务不替她编。

`_normalize`/指纹复用 `QQOutboundGuardService.fingerprint`：同一句在复读去重、
烂梗匹配、干涸判定三处算出来的必须是同一个东西，两份真相早晚漂移。
"""

from __future__ import annotations

import time
from typing import Any

from .outbound_guard_service import QQOutboundGuardService

#: 干涸判定：看最近几条；几条共用同一关键词算绕圈；几条 ≤ 几字算敷衍。
DRY_WINDOW = 4
DRY_REPEATS = 3
CURT_CHARS = 4
CURT_RUN = 3
#: 「转移话题指令」触发后的冷却（秒）：她换过一次没换动，也不连环催。
HINT_COOLDOWN_SECONDS = 900
#: 每个群保留多少条观察、留多久。
MAX_OBSERVED = 16
OBSERVE_TTL_SECONDS = 30 * 60
#: 烂梗表进提示词的行宽上限（列表再长也先给模型看前这么多字）。
LIST_PROMPT_MAX_CHARS = 300


def _normalize(text: str) -> str:
    return QQOutboundGuardService.fingerprint(text)


def _bigrams(fp: str) -> set[str]:
    """归一化文本的所有 2 字窗口（含字母数字串）。"""
    return {fp[i:i + 2] for i in range(len(fp) - 1)}


#: 单字兜底要跳开的虚词表 —— 中文高频单字里只有这些几乎不单独构成话题词。
#: 拿不准就少判（漏判只是少贴一行指令，错判会把语气词当成"绕圈"）。
_FUNCTION_CHARS = set("的了呢吗吧啊呀哦哈嗯是我你他她它们这那就要有和在不要很太还也又")


def _single_chars(fp: str) -> set[str]:
    return {ch for ch in fp if "\u4e00" <= ch <= "\u9fff" and ch not in _FUNCTION_CHARS}


class QQTopicShiftService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        #: group_id → [(归一化文本, 时刻)]，只留文字（发言人身份不归它管）。
        self._observed: dict[str, list[tuple[str, float]]] = {}
        #: group_id → 上次贴「转移话题指令」的时刻。
        self._last_hint_at: dict[str, float] = {}

    # ── 配置 ────────────────────────────────────────────────────

    def _setting(self, key: str, default: Any) -> Any:
        settings = getattr(self.plugin, "_qq_settings", None) or {}
        value = settings.get(key)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._setting("topic_shift_enabled", True))

    def _meme_enabled(self) -> bool:
        return bool(self._setting("stale_meme_enabled", True))

    def meme_raw_list(self) -> list[str]:
        return [str(x).strip() for x in (self._setting("stale_meme_list", []) or []) if str(x).strip()]

    # ── 观察 ────────────────────────────────────────────────────

    def observe(self, *, key: str, text: str, now: float | None = None) -> None:
        """记一条会话原文的归一化指纹。键：群=group_id，私聊=``private:{user_id}``。"""
        key = str(key or "").strip()
        if not key or not self._enabled():
            return
        fp = _normalize(text)
        if not fp:
            return
        ts = float(now if now is not None else time.time())
        rows = self._observed.setdefault(key, [])
        rows.append((fp, ts))
        rows[:] = [r for r in rows if ts - r[1] <= OBSERVE_TTL_SECONDS][-MAX_OBSERVED:]

    def forget(self, key: str) -> None:
        self._observed.pop(str(key or "").strip(), None)

    # ── 判定 ────────────────────────────────────────────────────

    def meme_hit(self, text: str) -> str:
        """命中烂梗表则返回表里那条原文（最长命中优先），否则 ``""``。

        比对两边都走同一份归一化：`你干嘛~哎哟` 与 `你干嘛 哎哟！` 算同一条梗。
        归一化后不足 2 字的条目**不参与匹配** —— 单字/空串当黑名单会把所有话拦死。
        """
        if not self._meme_enabled():
            return ""
        # 指纹只剥标点/标签、**不折叠大小写**：这里自己折，让 City不City 命中 city不city。
        fp = _normalize(text).lower()
        if not fp:
            return ""
        best = ""
        best_len = 0
        for raw in self.meme_raw_list():
            m = _normalize(raw).lower()
            if len(m) < 2:
                continue
            if m in fp and len(m) > best_len:
                best, best_len = raw, len(m)
        return best

    def is_dry(self, key: str, *, now: float | None = None) -> tuple[bool, str]:
        """当前话题是否已干涸。返回 (是否, 一句原因)。"""
        if not self._enabled():
            return False, ""
        ts = float(now if now is not None else time.time())
        rows = [r for r in self._observed.get(str(key or "").strip(), []) if ts - r[1] <= OBSERVE_TTL_SECONDS]
        if len(rows) < DRY_WINDOW:
            return False, ""
        window = [fp for fp, _at in rows[-DRY_WINDOW:]]
        counts: dict[str, int] = {}
        for fp in window:
            for bg in _bigrams(fp):
                counts[bg] = counts.get(bg, 0) + 1
        # 并列时按"先在窗口里出现"定序，判定才可复现。
        hot, hot_count = "", 0
        for fp in window:
            for bg in sorted(_bigrams(fp)):
                if counts.get(bg, 0) > hot_count:
                    hot, hot_count = bg, counts[bg]
        if hot_count >= DRY_REPEATS:
            return True, f"近 {DRY_WINDOW} 条里 {hot_count} 条都在绕「{hot}」"
        # 单字兜底：中文话题词也常只有一个字（"猫"、"展"）。bigram 一个都没到
        # 阈值时才轮到它，虚词先排掉。
        char_counts: dict[str, int] = {}
        for fp in window:
            for ch in sorted(_single_chars(fp)):
                char_counts[ch] = char_counts.get(ch, 0) + 1
        hot1, hot1_count = "", 0
        for fp in window:
            for ch in sorted(_single_chars(fp)):
                if char_counts.get(ch, 0) > hot1_count:
                    hot1, hot1_count = ch, char_counts[ch]
        if hot1_count >= DRY_REPEATS:
            return True, f"近 {DRY_WINDOW} 条里 {hot1_count} 条都在绕「{hot1}」"
        tail = [fp for fp, _at in rows[-CURT_RUN:]]
        if tail and all(len(fp) <= CURT_CHARS for fp in tail):
            return True, f"连续 {CURT_RUN} 条都是敷衍短句"
        return False, ""

    # ── 本轮指令 ────────────────────────────────────────────────

    def turn_directives(self, *, key: str, message: str, now: float | None = None) -> str:
        """本轮提示词前缀（群/私聊通用）。无事发生返回 ``""``（不污染上下文）。"""
        lines: list[str] = []
        if self._meme_enabled():
            raw_list = self.meme_raw_list()
            if raw_list:
                shown = "、".join(raw_list)[:LIST_PROMPT_MAX_CHARS]
                lines.append(
                    f"【烂梗表】这些梗已经过时，你自己绝对不要用，对方玩也不要接：{shown}"
                )
                hit = self.meme_hit(message)
                if hit:
                    lines.append(
                        f"【烂梗筛选】对方刚在玩表里的旧梗「{hit}」：不要复读原句，不要接梗；"
                        "简单应一句就过，或者把话头挪到你自己的事上。"
                    )
        if self._enabled():
            dry, why = self.is_dry(key, now=now)
            if dry:
                k = str(key or "").strip()
                ts = float(now if now is not None else time.time())
                if ts - float(self._last_hint_at.get(k, 0.0)) >= HINT_COOLDOWN_SECONDS:
                    self._last_hint_at[k] = ts
                    lines.append(
                        f"【转移话题指令】这个话题已经聊干了（{why}）。本轮不要再接原话题，"
                        "也不要用提问换话题；说说你自己的近况或看法，把新话题带起来。"
                    )
        return "".join(line + "\n" for line in lines)
