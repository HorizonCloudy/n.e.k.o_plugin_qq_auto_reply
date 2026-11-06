"""P0b 回归：写前登记的顺序与遗忘路径的"不登记"。

`QQMemoryBridge` 不依赖宿主包（纯 stdlib + 延迟 import），所以这三条能在单测里做，
不需要 e2e 的包加载器。三条都是**顺序/副作用**类断言，最容易在重构时被改坏：

1. **写前登记失败 → fail-closed**：登记抛异常时不得发 HTTP（宁可少记一条，
   也不要"发出去但没人知道"）；
2. **HTTP 失败 → 索引行保留**：登记发生在 HTTP 之前，HTTP 失败后 bridge **不删**索引行
   （删早了重试就再也枚举不到该 subject）；
3. **遗忘路径不登记**：`post_scoped_forget` 不得触发 `on_subject_write`，否则删掉的坐标
   会被自己插回来。
"""

from __future__ import annotations

import asyncio
import unittest

from memory_bridge import QQMemoryBridge


class _FakeResponse:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def raise_for_status(self) -> None:
        if self._fail:
            raise RuntimeError("http boom")

    def json(self) -> dict:
        return {"ok": True}


class _FakeClient:
    """记录发出去的请求；可配置成失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail = fail

    async def post(self, url, *, json=None, timeout=None):
        self.calls.append((url, json or {}))
        return _FakeResponse(fail=self._fail)


class _Bridge(QQMemoryBridge):
    """把 `_client()` 换成假客户端（真实实现在纯 stdlib 环境下拿不到宿主客户端）。"""

    def __init__(self, client: _FakeClient) -> None:
        super().__init__(plugin=None)
        self._fake = client

    def _client(self):  # type: ignore[override]
        return self._fake

    def _base_url(self) -> str:  # type: ignore[override]
        # 真实实现会延迟 import 宿主的 `config.MEMORY_SERVER_PORT`；单测里不需要真地址
        return "http://127.0.0.1:0"


PARTICIPANT = {"subject_kind": "participant", "subject_id": "qq:u1"}


class WriteBeforeIndexOrderingTests(unittest.TestCase):
    def test_index_failure_means_zero_http_requests(self) -> None:
        """写前登记失败 → 一条 HTTP 都不许发（fail-closed）。"""
        client = _FakeClient()
        bridge = _Bridge(client)

        def _boom(_subject):
            raise RuntimeError("index unavailable")

        bridge.on_subject_write = _boom

        with self.assertRaises(RuntimeError):
            asyncio.run(
                bridge.post_scoped_mentions(
                    "兰兰", "回复正文", subjects=[PARTICIPANT]
                )
            )
        self.assertEqual(
            client.calls, [], f"登记失败仍发了请求：{client.calls}"
        )

    def test_http_failure_keeps_the_index_row(self) -> None:
        """HTTP 失败后，登记已经发生且**不得撤销**（删早了重试就枚举不到）。"""
        client = _FakeClient(fail=True)
        bridge = _Bridge(client)
        written: list[dict] = []
        bridge.on_subject_write = written.append

        with self.assertRaises(RuntimeError):
            asyncio.run(
                bridge.post_scoped_mentions(
                    "兰兰", "回复正文", subjects=[PARTICIPANT]
                )
            )

        self.assertEqual(written, [PARTICIPANT], "登记必须先发生（顺序：登记 → HTTP）")
        self.assertEqual(len(client.calls), 1, "HTTP 确实发出过一次（失败在响应阶段）")
        # bridge 没有"删索引"的调用面：遗忘路径才删，写入失败不删
        self.assertFalse(
            hasattr(bridge, "_unregister_write") or hasattr(bridge, "_delete_subject_index"),
            "写入路径不该出现删索引的方法（索引行要留给重试）",
        )

    def test_forget_path_does_not_register(self) -> None:
        """`post_scoped_forget` 绝不能触发登记（否则删掉的坐标被自己插回来）。"""
        client = _FakeClient()
        bridge = _Bridge(client)
        written: list[dict] = []
        bridge.on_subject_write = written.append

        asyncio.run(bridge.post_scoped_forget("兰兰", subject=PARTICIPANT))

        self.assertEqual(written, [], f"遗忘路径触发了登记：{written}")
        self.assertEqual(len(client.calls), 1, "遗忘应发出 1 次 HTTP")
        self.assertIn("scoped_forget", client.calls[0][0])


if __name__ == "__main__":
    unittest.main()
