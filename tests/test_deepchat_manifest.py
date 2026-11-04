from __future__ import annotations

import ast
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PLUGIN_ROOT / "__init__.py"

EXPECTED_ENTRIES = {
    "dc_view_state",
    "dc_reset_user",
    "dc_set_mood",
    "dc_tune",
    "dc_recall",
    # P0a（8577d21）新增的遗忘入口：本地 sqlite + 宿主 participant，边界显式。
    # 加进入口集合即等于声明"它要遵守 dc_* 的 async / **kw 契约"，所以必须登记而不是放过。
    "dc_forget_user",
}


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    return str(getattr(target, "attr", ""))


class DeepChatEntryContractTests(unittest.TestCase):
    def test_deepchat_entries_are_async_and_accept_kwargs(self) -> None:
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if _decorator_name(decorator) != "plugin_entry":
                    continue
                entry_id = ""
                call = decorator if isinstance(decorator, ast.Call) else None
                if call is not None:
                    for keyword in call.keywords:
                        if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                            entry_id = str(keyword.value.value)
                if entry_id.startswith("dc_"):
                    found[entry_id] = node

        self.assertEqual(set(found), EXPECTED_ENTRIES)
        for entry_id, node in found.items():
            self.assertIsInstance(node, ast.AsyncFunctionDef, f"{entry_id} 必须是 async def")
            self.assertIsNotNone(node.args.kwarg, f"{entry_id} 必须接受 **kw")


if __name__ == "__main__":
    unittest.main()
