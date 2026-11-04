# -*- coding: utf-8 -*-
"""主动私聊的**唯一**计数源：有由头搭话与深聊自主联系共用一本账。

## 为什么必须唯一

两个功能都是「她主动私聊」，各自记一笔会让使用者设的
``proactive_initiative_max_per_day = 2`` 实际变成 4 条。历史上前者用
``_today_count``、后者用 ``deepchat_autonomy.count_today``，两套账互不知情——
这就是 P2-① 要修的超发通道。

## 三态预占（宁可少发，不可重发）

```
reserved --commit--> committed     真的发出去了，计入额度
    |
    +---release---> released        放弃/未发送，额度退回
```

- **只有 ``reserved`` 可以 ``release``**：一旦进入 ``sending``（已经在等 HTTP 返回），
  就按**已消耗**处理——因为无法证明对方没收到。这与「宁可少发，不可重发」一致：
  一次含糊的失败宁可少说一条，也别冒重发的风险。
- 未提交的预占也**占额度**（``remaining_today`` 把 in-flight 一起算进去），
  否则两个来源在同一个 tick 里各预占一次，就会双双越过上限。

## 落盘

状态由持有方（``QQProactiveInitiativeService``）序列化到自己的 JSON 里，
键名保持 ``today`` / ``today_count`` / ``last_sent_at`` 以兼容旧存档；
``today_count`` 现在是**两个来源合计**（这正是本模块的目的）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

#: 预占状态。``sending`` 之后一律按已消耗处理（见模块 docstring）。
STATE_RESERVED = "reserved"
STATE_SENDING = "sending"
STATE_COMMITTED = "committed"
STATE_RELEASED = "released"


@dataclass
class BudgetReservation:
    """一次预占。``state`` 只能单向推进，``release`` 仅对 ``reserved`` 有效。"""

    token: str
    source: str
    at: float
    state: str = STATE_RESERVED
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def releasable(self) -> bool:
        return self.state == STATE_RESERVED


class ProactiveBudget:
    """进程内共享账本。两个来源都只能通过它计数。

    ``max_per_day`` / ``min_gap_seconds`` 传**取值函数**而不是数值：配置改动要立刻
    生效（与本仓库「配置热生效」的口径一致），而不是等下一次重建对象。
    """

    def __init__(
        self,
        *,
        max_per_day: Callable[[], int],
        min_gap_seconds: Callable[[], float],
    ) -> None:
        self._max_per_day = max_per_day
        self._min_gap_seconds = min_gap_seconds
        self._day = ""
        self._committed: list[tuple[str, float]] = []
        self._in_flight: dict[str, BudgetReservation] = {}
        self._last_sent_at = 0.0

    # ── 内部 ────────────────────────────────────────────────────────

    @staticmethod
    def _day_of(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def _roll(self, now: float) -> None:
        """跨天翻篇：清掉昨天的已提交记录与残留预占。"""
        today = self._day_of(now)
        if today == self._day:
            return
        self._day = today
        self._committed.clear()
        for token, reservation in list(self._in_flight.items()):
            if self._day_of(reservation.at) != today:
                self._in_flight.pop(token, None)

    # ── 读 ──────────────────────────────────────────────────────────

    def sent_today(self, now: float | None = None) -> int:
        """今天已消耗的次数（已提交 + 在飞预占）。"""
        stamp = time.time() if now is None else float(now)
        self._roll(stamp)
        return len(self._committed) + len(self._in_flight)

    def remaining_today(self, now: float | None = None) -> int:
        stamp = time.time() if now is None else float(now)
        try:
            cap = max(0, int(self._max_per_day()))
        except (TypeError, ValueError):
            cap = 0
        return max(0, cap - self.sent_today(stamp))

    def last_sent_at(self) -> float:
        """全局最后一次**真的发出去**的时刻（两个来源共用）。"""
        return float(self._last_sent_at)

    def min_gap_seconds(self) -> float:
        try:
            return max(0.0, float(self._min_gap_seconds()))
        except (TypeError, ValueError):
            return 0.0

    def gap_remaining(self, now: float | None = None) -> float:
        """距离允许下一次开口还差多少秒（0 = 可以开口）。"""
        stamp = time.time() if now is None else float(now)
        last = self.last_sent_at()
        if not last:
            return 0.0
        return max(0.0, self.min_gap_seconds() - (stamp - last))

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        stamp = time.time() if now is None else float(now)
        self._roll(stamp)
        try:
            cap = max(0, int(self._max_per_day()))
        except (TypeError, ValueError):
            cap = 0
        sent = self.sent_today(stamp)
        return {
            "sent_today": sent,
            "remaining": max(0, cap - sent),
            "max_per_day": cap,
            "last_sent_at": self.last_sent_at(),
            "in_flight": len(self._in_flight),
        }

    # ── 写（三态） ──────────────────────────────────────────────────

    def try_reserve(self, source: str, *, now: float | None = None) -> BudgetReservation | str:
        """尝试占一个额度。成功返回预占对象，失败返回**原因字符串**。"""
        stamp = time.time() if now is None else float(now)
        self._roll(stamp)
        if self.remaining_today(stamp) <= 0:
            return "今天已经说够了"
        if self.gap_remaining(stamp) > 0:
            return "距上次太近"
        reservation = BudgetReservation(token=uuid.uuid4().hex, source=str(source or ""), at=stamp)
        self._in_flight[reservation.token] = reservation
        return reservation

    def mark_sending(self, reservation: BudgetReservation) -> None:
        """进入发送阶段：此后**不可回收**（见模块 docstring）。"""
        if reservation.state == STATE_RESERVED:
            reservation.state = STATE_SENDING

    def commit(self, reservation: BudgetReservation) -> None:
        """确认发出：计入额度并更新全局 last_sent_at。"""
        self._in_flight.pop(reservation.token, None)
        reservation.state = STATE_COMMITTED
        self._committed.append((reservation.source, reservation.at))
        self._last_sent_at = max(self._last_sent_at, reservation.at)

    def release(self, reservation: BudgetReservation) -> bool:
        """退回额度。**只有 ``reserved`` 能退**；返回是否真的退了。"""
        if not reservation.releasable:
            return False
        self._in_flight.pop(reservation.token, None)
        reservation.state = STATE_RELEASED
        return True

    def record(self, source: str, *, now: float | None = None) -> BudgetReservation | str:
        """「已经发出去了」的简写：预占并立即提交。返回预占对象或失败原因。"""
        reservation = self.try_reserve(source, now=now)
        if isinstance(reservation, str):
            return reservation
        self.commit(reservation)
        return reservation

    # ── 持久化 ──────────────────────────────────────────────────────

    def to_payload(self) -> dict[str, Any]:
        """给持有方落盘用。键名与历史存档保持一致。"""
        return {
            "today": self._day,
            "today_count": len(self._committed),
            "last_sent_at": self._last_sent_at,
        }

    def restore(self, payload: dict[str, Any] | None) -> None:
        """从历史存档恢复。坏数据按空账处理（宁可多发一条，也不要卡死启动）。"""
        self._committed.clear()
        self._in_flight.clear()
        self._last_sent_at = 0.0
        if not isinstance(payload, dict):
            return
        self._day = str(payload.get("today") or "")
        try:
            count = max(0, int(payload.get("today_count") or 0))
        except (TypeError, ValueError):
            count = 0
        try:
            self._last_sent_at = float(payload.get("last_sent_at") or 0.0)
        except (TypeError, ValueError):
            self._last_sent_at = 0.0
        # 历史存档只有合计计数，没有来源区分：用一个占位来源把额度先占住，
        # 避免"重启后当天额度凭空复原"。
        self._committed = [("legacy", self._last_sent_at) for _ in range(count)]
