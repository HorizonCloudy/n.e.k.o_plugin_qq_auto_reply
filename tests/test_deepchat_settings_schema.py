"""DeepChat 隐私开关当前只走 dc_tune（dashboard save chain 尚未接）。

dashboard_service.save_settings 与前端字段还没有这四个键；若贸然放进
``MISC``/``SAVEABLE_KEYS``，保存链会丢键、上游 test_qq_settings_* 会红。
本文件钉住「仍在 TUNING_KEYS、但暂不进 MISC/SAVEABLE」的 deferral；
恢复条件见测试方法 docstring。

注：仓库里 `tests/test_qq_settings_schema.py` 是 pytest 风格且需要宿主包路径，
本机没有 pytest，放那里等于写一条无法执行的测试；这里用 unittest 直连
``settings_schema`` 模块，保证同一命令下可被真实跑到。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import settings_schema
from deepchat import service as deepchat_service

_PLUGIN_INIT = pathlib.Path(__file__).resolve().parents[1] / "__init__.py"
_REPO_ROOT = _PLUGIN_INIT.parent

#: 缺一不可的隐私/行为开关。
PRIVACY_KEYS: tuple[str, ...] = (
    "deepchat_enabled",
    "deepchat_fragments_enabled",
    "deepchat_autonomy_enabled",
    "deepchat_expression_learning_enabled",
)


class DeepChatPrivacySwitchesTests(unittest.TestCase):
    def test_privacy_switches_are_dc_tune_only_for_now(self) -> None:
        """dashboard save_settings 与前端 UI 尚未接这四个键，暂只走 dc_tune。

        一旦补了 save_settings 具名参数 + 前端回显/提交，再把它们放进
        MISC/SAVEABLE_KEYS；在那之前放进 SAVEABLE_KEYS 会被 dashboard 保存链
        丢弃（上游 test_qq_settings_save_chain / test_qq_config_save_keys 会红）。
        """
        misc_keys = {spec.key for spec in settings_schema.MISC}
        for key in PRIVACY_KEYS:
            with self.subTest(key=key):
                self.assertIn(
                    key,
                    deepchat_service.TUNING_KEYS,
                    f"{key} 必须保留 dc_tune 入口，否则用户无法关闭该能力",
                )
                self.assertNotIn(
                    key,
                    misc_keys,
                    f"{key} 暂不进 MISC：dashboard save_settings / 前端字段未接",
                )
                self.assertNotIn(
                    key,
                    settings_schema.SAVEABLE_KEYS,
                    f"{key} 暂不进 SAVEABLE：保存链会丢键，UI 会报 TypeError",
                )


class RetiredTuningKeyTests(unittest.TestCase):
    """已停用的旋钮：既不能从 tune 那里"保存成功"，也不能在工具描述里被广告。"""

    RETIRED_KEY = "deepchat_reflection_enabled"

    def _dc_tune_description_block(self) -> str:
        source = _PLUGIN_INIT.read_text(encoding="utf-8")
        start = source.find('id="dc_tune"')
        self.assertGreater(start, -1, "找不到 dc_tune 入口定义")
        end = source.find("@plugin_entry", start + 1)
        return source[start : end if end > start else start + 2000]

    def test_retired_keys_are_marked_and_not_advertised(self) -> None:
        """停用键必须被登记，且不能出现在 dc_tune 的「param 取 …」里。

        描述是 `", ".join(DEEPCHAT_TUNING_KEYS)` 生成的，所以除了"键名别出现"，
        还要断言没有整表 join——否则任何 retired 键都会被自动广告出去。
        """
        retired = getattr(deepchat_service, "RETIRED_TUNING_KEYS", None)
        self.assertIsNotNone(retired, "缺少 RETIRED_TUNING_KEYS：retired 键无处标注")
        self.assertTrue(retired, "RETIRED_TUNING_KEYS 为空，等于没有登记任何停用键")
        self.assertTrue(
            set(retired) <= set(deepchat_service.TUNING_KEYS),
            "retired 键必须仍在 TUNING_KEYS 里——否则 tune 走 unknown 分支，给不出停用原因",
        )
        self.assertIn(self.RETIRED_KEY, set(retired))

        block = self._dc_tune_description_block()
        self.assertNotIn(self.RETIRED_KEY, block, "dc_tune 描述里仍出现停用键名")
        self.assertNotIn(
            '", ".join(DEEPCHAT_TUNING_KEYS)',
            block,
            "描述仍在整表 join(TUNING_KEYS)：retired 键会被当成可选项广告出去",
        )
        # 精确相等而不是"子集"：广告必须覆盖 TUNING_KEYS 里的**全部非停用键**，
        # 否则反过来会漏掉"能用但没广告"的隐藏旋钮——那是同一个缺陷换个方向复发。
        # 断言过滤谓词本身（而不是某个具体拼法），这样加 sorted()/换行都不误伤。
        self.assertIn(
            "if k not in DEEPCHAT_RETIRED_TUNING_KEYS",
            block,
            "广告生成器必须排除 RETIRED 键（等价于 advertised == TUNING_KEYS - RETIRED）",
        )


class EmotionVocabularyTests(unittest.TestCase):
    """P2-⑤：DeepChat 的 Mood 与 attention 情绪词表必须显式对齐。

    两侧词表**目前不共享**（DeepChat: neutral/happy/excited/...；attention:
    calm/playful/proud/...），交集只有 4 个。这里不硬编码"必须完全统一"，
    而是要求：**每个 Mood 要么出现在共享词表里，要么被显式登记为 deepchat 专有**。

    想加新 mood 的人，必须在这里做一次有意识的选择（登记，或先去统一词表）——
    仓库里 attention 侧已有同样惯例（`test_qq_emotion_vocabulary.py` 的白名单）。
    """

    #: 显式登记：这些 mood 只在 DeepChat 内部使用，不入 attention 词表。
    #: 空集合 = 目前一个都没登记（所以本用例现在是红的，见 docstring）。
    DEEPCHAT_ONLY_MOODS: frozenset[str] = frozenset()

    @staticmethod
    def _mood_values() -> set[str]:
        source = (_REPO_ROOT / "deepchat" / "models.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef) and node.name == "Mood":
                values = {
                    stmt.value.value
                    for stmt in node.body
                    if isinstance(stmt, ast.Assign)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                }
                if values:
                    return values
        raise AssertionError("解析不到 Mood 枚举取值")

    @staticmethod
    def _attention_emotions() -> set[str]:
        source = (_REPO_ROOT / "attention_service.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            # 带类型注解的写法是 AnnAssign（`_EMOTION_MULTIPLIER: dict[str, float] = {...}`），
            # 不带注解才是 Assign —— 只认 Assign 会误报"解析不到词表"，让 3D 的红变成假信号。
            if isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            elif isinstance(node, ast.Assign):
                target, value = (node.targets[0] if node.targets else None), node.value
            else:
                continue
            if not (
                isinstance(target, ast.Name)
                and target.id == "_EMOTION_MULTIPLIER"
                and isinstance(value, ast.Dict)
            ):
                continue
            return {
                key.value
                for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
        raise AssertionError("解析不到 attention 的 _EMOTION_MULTIPLIER 词表")

    def test_every_mood_is_shared_or_explicitly_registered(self) -> None:
        moods = self._mood_values()
        shared = self._attention_emotions()

        unregistered = sorted(
            mood for mood in moods if mood not in shared and mood not in self.DEEPCHAT_ONLY_MOODS
        )
        self.assertEqual(
            unregistered,
            [],
            "这些 Mood 既不在共享情绪词表里，也没登记为 deepchat 专有："
            f"{unregistered}（共享词表：{sorted(shared)}）",
        )


if __name__ == "__main__":
    unittest.main()
