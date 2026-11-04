"""Phase 0：DeepChat 的隐私开关必须真的可保存。

这些键控制"是否注入表达/关系/主动私聊"——如果它们不在 ``MISC`` 和
``SAVEABLE_KEYS`` 里，面板上看起来能关、实际保存后被丢弃，用户会以为
隐私已关闭而它还在跑。属于"配置面失败导致隐私承诺失效"，故单独成测。

注：仓库里 `tests/test_qq_settings_schema.py` 是 pytest 风格且需要宿主包路径，
本机没有 pytest，放那里等于写一条无法执行的测试；这里用 unittest 直连
``settings_schema`` 模块，保证同一命令下可被真实跑到。
"""

from __future__ import annotations

import pathlib
import unittest

import settings_schema
from deepchat import service as deepchat_service

_PLUGIN_INIT = pathlib.Path(__file__).resolve().parents[1] / "__init__.py"

#: 缺一不可的隐私/行为开关。
PRIVACY_KEYS: tuple[str, ...] = (
    "deepchat_enabled",
    "deepchat_fragments_enabled",
    "deepchat_autonomy_enabled",
    "deepchat_expression_learning_enabled",
)


class DeepChatPrivacySwitchesTests(unittest.TestCase):
    def test_privacy_switches_are_listed_and_saveable(self) -> None:
        # ``MISC`` 是 SettingSpec 元组（不是键名元组），要按 .key 取键名。
        misc_keys = {spec.key for spec in settings_schema.MISC}
        for key in PRIVACY_KEYS:
            with self.subTest(key=key):
                self.assertIn(
                    key,
                    misc_keys,
                    f"{key} 不在 MISC：面板渲染不到这个开关",
                )
                self.assertIn(
                    key,
                    settings_schema.SAVEABLE_KEYS,
                    f"{key} 不在 SAVEABLE_KEYS：保存时会被丢弃",
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


if __name__ == "__main__":
    unittest.main()
