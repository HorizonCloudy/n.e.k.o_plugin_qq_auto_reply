"""3E 客户端侧的三道闸与可观测性（mock 版）。

**当前不发真实 HTTP**：服务端的 `assistant` 抽取行为与原话守卫未确认，`_post_impression`
是占位实现。所以这里只钉**客户端能保证的部分**——闸的顺序、fail-closed、以及空结果的
告警分级；将来替换 `_post_impression` 时这些断言不用改。

三道闸（顺序即语义，顺序错了就漏）：
1. `private_only`：`group_id` 非空 → 直接拒，**零请求**（群成员域不走这里）；
2. 原话守卫：印象命中用户最近原话子串 → 拒，**零请求**（"提炼"退化成"抄写"就把
   Phase 0 修掉的原话副本请回来了，还带"已脱敏"的错觉）；
3. 授权闸：入口查一次 + **发 HTTP 前再查一次**（中间用户可能改设置）→ 第二次失败
   reason 前缀 `recheck_`。

可观测：首次空结果 → INFO + `no_new_fact`（单次空是正常现象，不许告警刷屏）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepchat.service import DeepChatService


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"identities": []}


class RememberMilestoneGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = _FakePlugin(self._tmp.name)
        self.service = DeepChatService(self.plugin)
        self.posted = _Recorder()
        self.service._post_impression = self.posted  # type: ignore[method-assign]
        self.warnings: list[str] = []
        self.service._warn = self.warnings.append  # type: ignore[method-assign]
        self.service._info = lambda *_a, **_k: None  # type: ignore[method-assign]
        # 授权闸默认放行；具体用例可覆盖
        self.service._remember_gate = lambda uid: {"allowed": True, "reason": ""}  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_group_scope_is_private_only_and_sends_nothing(self) -> None:
        result = self.service.remember_milestone(
            sender_id="u1", impression="他最近在准备搬家", group_id="g1"
        )
        self.assertEqual(result["reason"], "private_only")
        self.assertFalse(result["written"])
        self.assertEqual(self.posted.calls, [], "群成员域必须零请求")

    def test_verbatim_user_words_are_rejected_and_send_nothing(self) -> None:
        quote = "我昨天在城南那家面馆吃到一半停电了"
        self.plugin._user_sessions["private:u1"] = {
            "session": SimpleNamespace(
                _conversation_history=[{"role": "user", "content": quote}]
            )
        }
        result = self.service.remember_milestone(
            sender_id="u1", impression=f"他说：{quote}"
        )
        self.assertEqual(result["reason"], "verbatim_user_words")
        self.assertFalse(result["written"])
        self.assertEqual(self.posted.calls, [], "命中原话必须零请求")

    def test_recheck_blocks_when_settings_change_mid_flight(self) -> None:
        # 入口放行、发前复检拒绝 → reason 必须带 recheck_ 前缀，且零请求
        answers = iter([{"allowed": True, "reason": ""},
                        {"allowed": False, "reason": "memory_mode_legacy"}])
        self.service._remember_gate = lambda uid: next(answers)  # type: ignore[method-assign]

        result = self.service.remember_milestone(
            sender_id="u1", impression="他最近在准备搬家"
        )
        self.assertEqual(result["reason"], "recheck_memory_mode_legacy")
        self.assertFalse(result["written"])
        self.assertEqual(self.posted.calls, [], "复检失败必须零请求")

    def test_first_empty_result_warns_once_then_latches(self) -> None:
        """首次空结果（还没成功过）打**一次** WARNING，之后不再刷屏（闩锁）。"""
        first = self.service.remember_milestone(
            sender_id="u1", impression="他最近在准备搬家"
        )
        self.assertEqual(len(self.posted.calls), 1, "闸都过了就应该发一次")
        self.assertEqual(first.get("reason"), "never_succeeded")
        self.assertFalse(first["written"])
        self.assertEqual(
            len([w for w in self.warnings if "never_succeeded" in w]),
            1,
            f"首次空结果应恰好打一次 never_succeeded 警告：{self.warnings}",
        )

        before = len([w for w in self.warnings if "never_succeeded" in w])
        self.service.remember_milestone(sender_id="u1", impression="他最近在准备搬家")
        self.assertEqual(
            len([w for w in self.warnings if "never_succeeded" in w]),
            before,
            f"闩锁失效：第二次仍在刷同一条警告：{self.warnings}",
        )


class _FakePlugin:
    def __init__(self, data_dir: str) -> None:
        self._qq_settings: dict = {
            "deepchat_private_participant_memory_enabled": True,
            "private_memory_mode": "participant",
        }
        self._data_dir = data_dir
        self.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        self._user_sessions: dict = {}
        self.memory_bridge = None
        self.proactive_message_service = None

    def data_path(self) -> Path:
        return Path(self._data_dir)

    def _build_session_key(
        self, *, sender_id: str, is_group: bool = False, group_id: str | None = None
    ) -> str:
        return f"group:{group_id or ''}" if is_group else f"private:{sender_id}"


if __name__ == "__main__":
    unittest.main()
