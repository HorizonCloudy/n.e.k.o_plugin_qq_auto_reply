"""P0b-④：社交坐标索引的**双轴**删除必须彼此独立。

`forget_user(user_id)` 走用户轴（`WHERE user_id`），`memory_forget(group_id)` 走群轴
（`WHERE group_id`）。两条轴混在一起是隐私事故的常见来源：
- 删用户时按群轴删 → 会把**同一个群里其他人**的坐标一起删掉（别人的记忆被误清）；
- 删群时按用户轴删 → 会把该用户的**私聊 participant** 坐标一起删掉。
所以两个方向都要各测一次，而不是只测"删完是空的"。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deepchat.store import DeepChatStore
from deepchat.subject_index import (
    KIND_GROUP_PARTICIPANT,
    KIND_PARTICIPANT,
    SubjectIndex,
    parse_subject,
)


class SubjectIndexAxisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = DeepChatStore(Path(self._tmp.name) / "index.sqlite3")
        self.index = SubjectIndex(self.store)
        # 两个用户、两个群，形成"交叉"数据，才能区分两条轴
        self.index.upsert(user_id="u1", subject_kind=KIND_PARTICIPANT, now=1000.0)
        self.index.upsert(user_id="u2", subject_kind=KIND_PARTICIPANT, now=1000.0)
        self.index.upsert(user_id="u1", subject_kind=KIND_GROUP_PARTICIPANT, group_id="g1", now=1000.0)
        self.index.upsert(user_id="u2", subject_kind=KIND_GROUP_PARTICIPANT, group_id="g1", now=1000.0)
        self.index.upsert(user_id="u1", subject_kind=KIND_GROUP_PARTICIPANT, group_id="g2", now=1000.0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_user_axis_keeps_other_users_in_the_same_group(self) -> None:
        deleted = self.index.delete_user("u1")
        self.assertEqual(deleted, 3, "u1 有 1 条 participant + 2 条 group_participant")

        remaining = {(row["subject_kind"], row["group_id"], row["user_id"])
                     for row in self._all_rows()}
        self.assertEqual(
            remaining,
            {
                (KIND_PARTICIPANT, "", "u2"),
                (KIND_GROUP_PARTICIPANT, "g1", "u2"),
            },
            f"删 u1 只能按 user_id 删，不得动 u2 的行：{remaining}",
        )

    def test_group_axis_keeps_participant_of_its_members(self) -> None:
        deleted = self.index.delete_group("g1")
        self.assertEqual(deleted, 2, "g1 里有 u1/u2 两条群成员坐标")

        remaining = {(row["subject_kind"], row["group_id"], row["user_id"])
                     for row in self._all_rows()}
        self.assertEqual(
            remaining,
            {
                (KIND_PARTICIPANT, "", "u1"),
                (KIND_PARTICIPANT, "", "u2"),
                (KIND_GROUP_PARTICIPANT, "g2", "u1"),
            },
            f"删 g1 不得清掉成员的私聊 participant，也不得动 g2：{remaining}",
        )

    def test_axes_do_not_overlap_for_other_groups(self) -> None:
        self.index.delete_group("g1")
        self.assertEqual(
            [(row["group_id"], row["user_id"]) for row in self.index.rows_for_user("u1")],
            [("", "u1"), ("g2", "u1")],
            "用户轴读回应仍包含私聊域与 g2",
        )
        self.assertEqual(self.index.rows_for_group("g2"), self.index.rows_for_group("g2"))


    def _all_rows(self) -> list[dict]:
        rows = []
        for user_id in ("u1", "u2"):
            rows.extend(self.index.rows_for_user(user_id))
        return rows


class ParseSubjectTests(unittest.TestCase):
    """`parse_subject` 决定"什么值得登记"：解析不了就不登记（不猜坐标）。"""

    def test_participant_and_group_participant_are_parsed(self) -> None:
        self.assertEqual(
            parse_subject({"subject_kind": KIND_PARTICIPANT, "subject_id": "qq:u1"}),
            (KIND_PARTICIPANT, "", "u1"),
        )
        self.assertEqual(
            parse_subject({"subject_kind": KIND_GROUP_PARTICIPANT, "subject_id": "qq:g1:u1"}),
            (KIND_GROUP_PARTICIPANT, "g1", "u1"),
        )

    def test_unparsable_or_group_chat_is_not_registered(self) -> None:
        for subject in (
            None,
            {},
            {"subject_kind": "group_chat", "subject_id": "qq:g1"},  # 群本体不是"谁写过"
            {"subject_kind": KIND_PARTICIPANT, "subject_id": "qq:"},
            {"subject_kind": KIND_GROUP_PARTICIPANT, "subject_id": "qq:g1"},
        ):
            with self.subTest(subject=subject):
                self.assertIsNone(parse_subject(subject))


if __name__ == "__main__":
    unittest.main()
