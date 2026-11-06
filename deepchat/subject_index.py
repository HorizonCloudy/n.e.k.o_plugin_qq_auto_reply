# -*- coding: utf-8 -*-
"""宿主记忆的**社交坐标索引**：只记「谁 × 哪个域」写过，不记内容。

## 为什么需要它

遗忘一个用户要按「用户轴」枚举他写过的所有 subject；遗忘一个群要按「群轴」枚举。
宿主 API 没有"列出某人的所有 subject"的端点，所以只能自己登记——否则只能猜
（猜错就是漏删，而漏删在遗忘场景里是隐私事故，不是功能瑕疵）。

## 双轴删除

```
用户轴 forget_user(user_id)   → DELETE ... WHERE user_id = ?
群轴   memory_forget(group_id) → DELETE ... WHERE group_id = ?
```
两条轴独立可测：删用户不该动别人的群坐标，删群不该把用户私聊域一起清掉。

## 顺序（重要）

写入：**授权闸 → 发前 consent 复检 → upsert(intent) → HTTP**
遗忘：**HTTP 先成功 → 再删索引行**

- 写前登记失败 → **fail-closed**：不发 HTTP（宁可少记一条，也不要"发出去但没人知道"）；
- HTTP 失败 → **索引行留着无害**（下次遗忘时多删一次幂等 subject，比漏删好）；
- 遗忘时若 HTTP 失败 → **索引行保留**，这样重试还能枚举到——删早了就再也找不到它。

## 隐私

本表是社交关系坐标（谁跟哪个群/谁有记忆往来），**禁止明文备份/导出**；
`forget_user` / `memory_forget` 都会删对应轴的行。
"""

from __future__ import annotations

import time
from typing import Any

#: ``participant``：私聊个人域；``group_participant``：群成员域（按群分桶）。
KIND_PARTICIPANT = "participant"
KIND_GROUP_PARTICIPANT = "group_participant"
SUBJECT_KINDS = (KIND_PARTICIPANT, KIND_GROUP_PARTICIPANT)


def parse_subject(subject: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """把 bridge 的 subject 字典解析成 ``(kind, group_id, user_id)``。

    解析不了就返回 ``None`` —— 调用方按"这条不登记"处理（不猜、不造坐标）。
    subject_id 形态由 ``memory_bridge`` 决定：
    ``participant`` → ``qq:{user}``；``group_participant`` → ``qq:{group}:{user}``；
    ``group_chat`` → ``qq:{group}``（群本体不是"谁写过"，不登记）。
    """
    if not isinstance(subject, dict):
        return None
    kind = str(subject.get("subject_kind") or "").strip()
    raw = str(subject.get("subject_id") or "").strip()
    if kind not in SUBJECT_KINDS or not raw:
        return None
    body = raw[3:] if raw.startswith("qq:") else raw
    if kind == KIND_PARTICIPANT:
        user_id = body.strip()
        return (KIND_PARTICIPANT, "", user_id) if user_id else None
    group_id, _, user_id = body.partition(":")
    group_id, user_id = group_id.strip(), user_id.strip()
    if not group_id or not user_id:
        return None
    return (KIND_GROUP_PARTICIPANT, group_id, user_id)


class SubjectIndex:
    """``deepchat_subject_index`` 的读写封装。"""

    def __init__(self, store: Any) -> None:
        self.store = store

    def upsert(
        self,
        *,
        user_id: str,
        subject_kind: str,
        group_id: str = "",
        now: float | None = None,
    ) -> bool:
        """登记一条坐标（幂等）。参数不合法返回 False，不抛。"""
        kind = str(subject_kind or "").strip()
        uid = str(user_id or "").strip()
        gid = str(group_id or "").strip()
        if kind not in SUBJECT_KINDS or not uid:
            return False
        if kind == KIND_GROUP_PARTICIPANT and not gid:
            return False
        stamp = time.time() if now is None else float(now)
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO deepchat_subject_index(
                    subject_kind, group_id, user_id, last_written_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_kind, group_id, user_id) DO UPDATE SET
                    last_written_at = excluded.last_written_at
                """,
                (kind, gid, uid, stamp),
            )
        return True

    def upsert_subject(self, subject: dict[str, Any] | None, *, now: float | None = None) -> bool:
        """直接从 bridge 的 subject 字典登记（解析不了就跳过）。"""
        parsed = parse_subject(subject)
        if parsed is None:
            return False
        kind, group_id, user_id = parsed
        return self.upsert(user_id=user_id, subject_kind=kind, group_id=group_id, now=now)

    def delete_user(self, user_id: str) -> int:
        """用户轴：删这个人的所有坐标（含各群成员域）。"""
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        with self.store.connection() as conn:
            return int(
                conn.execute(
                    "DELETE FROM deepchat_subject_index WHERE user_id = ?", (uid,)
                ).rowcount
            )

    def delete_group(self, group_id: str) -> int:
        """群轴：删这个群的成员域坐标（不动用户私聊域）。"""
        gid = str(group_id or "").strip()
        if not gid:
            return 0
        with self.store.connection() as conn:
            return int(
                conn.execute(
                    "DELETE FROM deepchat_subject_index WHERE group_id = ?", (gid,)
                ).rowcount
            )

    def rows_for_user(self, user_id: str) -> list[dict[str, Any]]:
        uid = str(user_id or "").strip()
        if not uid:
            return []
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT subject_kind, group_id, user_id, last_written_at "
                "FROM deepchat_subject_index WHERE user_id = ? "
                "ORDER BY last_written_at DESC",
                (uid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rows_for_group(self, group_id: str) -> list[dict[str, Any]]:
        gid = str(group_id or "").strip()
        if not gid:
            return []
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT subject_kind, group_id, user_id, last_written_at "
                "FROM deepchat_subject_index WHERE group_id = ? "
                "ORDER BY last_written_at DESC",
                (gid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self.store.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM deepchat_subject_index").fetchone()
        return int(row[0]) if row else 0
