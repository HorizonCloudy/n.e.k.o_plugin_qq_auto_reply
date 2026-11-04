"""16b：用户可见文本里不得出现"已完成删除"式的过度承诺。

**为什么需要**：里程碑的存量清理是"逐条剔除、保留行"，`not_covered` 里还有
`group_participant` 等未覆盖面。若 runbook / 入口说明写成"已彻底删除"，用户会以为原话
已经没了 —— 这是比功能 bug 更难纠正的信任问题。

**面（surface）必须显式定义**，否则这条否断言会误伤合法文本：
- 扫：`docs/deepchat-architecture.html` 的 §10 切片（`<!-- deepchat-runbook:start/end -->`）
      + 各 `plugin_entry(id="dc_*")` 的 `description=` 块
- 不扫：源码注释/墓碑 docstring、`self._emit_log(...)` 日志、`tests/` 断言消息
  实测过：整文件扫会有 6 处误报，其中 5 处是"已清空 Token""已删除群自定义提示词"这类
  **真实操作日志**，1 处是墓碑（`⚠️ 已删除：_recent_summary()`）——它们是"面之外"，
  不是"已接受"。

**两类"命中"分开处理**（两种失败模式、两种机制，不能合并）：
- 面外文本：靠上面的"面"定义排除，绝不进语料；
- 面内但被**引用**（例：§10 的禁令句自己引用了一遍"无原话"）：靠引号区间 + 前缀否定放行；
- 面内但已接受（措辞约定）：进 `ACCEPTED_BLOCKED`。**这是措辞约定，不是正则能力上限**。
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUNBOOK = _REPO_ROOT / "docs" / "deepchat-architecture.html"
_PLUGIN_INIT = _REPO_ROOT / "__init__.py"

RUNBOOK_START = "<!-- deepchat-runbook:start -->"
RUNBOOK_END = "<!-- deepchat-runbook:end -->"

#: "已完成删除"式主张（正向动词）
CLAIM = re.compile(r"(?:已|全部|彻底|完全|均)[经被都]{0,3}\s*(?:清理|删除|清除|清空|移除)")
#: "没有原话残留"式主张（否定式不变量）
NOVERB = re.compile(r"(?:无|没有|不再有|不含|未含).{0,6}(?:原话|原始聊天|原始记录)")

#: 引用豁免：命中落在中文引号内，且同一行开引号之前有否定标记 → 视为"在谈论这个词组"
_QUOTE = re.compile(r"“[^”]*”")
NEGATION_MARKERS = ("不得", "禁止", "不要", "不可", "勿", "不应")

# ── 语料（下限写成断言：删样本会红，加样本不会）────────────────────────
BAD_CLAIM_SAMPLES = [
    "已清理完毕",
    "已经清理完毕",
    "彻底删除",
    "已经彻底删除",
    "全部清空",
    "完全移除",
    "全部都删除了",
    "均已清理",
    "已彻底清空该表",
    "已经全部清理掉了",
]
BAD_NOVERB_SAMPLES = [
    "已无原话",
    "不再有原话",
    "没有原始聊天记录",
    "无任何原话残留",
    "不含原话",
]
#: 逐字取自 §10 现文（不是复述）：这些句子含"删除/清空"字面，但都不构成主张。
GOOD_SAMPLES = [
    "不要删整行",
    "只删除匹配的元素",
    "保留行，不删除",
    "清空该字段后写回",
    "不做取证导出",
    "禁止明文备份",
    "删行会连关系数值一起丢",
    "B 类行若被应用自动清空，不可恢复",
    "只改 milestones 字段，不删整行",
    "审计只记数量、不记内容",
    "按行数确认会低估影响",
]
#: 面内、已接受（措辞约定）。真要清除可以改措辞，不要放宽正则。
ACCEPTED_BLOCKED = [
    "已删除的元素不可恢复",
    "已删除该行后写回",
]
#: 面外示例：只作注释，**不进任何断言**——它们是合法操作日志，不属"已接受"
SURFACE_EXCLUDED_SAMPLES = ["群 X 记忆已清除", "Token → (已清空)"]

EXEMPT_OK = ["这是显式接受的残留，不得据此宣称“库里已无原话”"]
EXEMPT_FAIL = [
    "不得据此宣称“库里已无原话”，实际已彻底删除全部原话",
    "本工具已彻底删除全部原话",
]


def _strip_accepted(text: str) -> str:
    for phrase in ACCEPTED_BLOCKED:
        text = text.replace(phrase, "")
    return text


def _is_quoted_under_negation(line: str, start: int) -> bool:
    for region in _QUOTE.finditer(line):
        if region.start() < start < region.end():
            return any(marker in line[: region.start()] for marker in NEGATION_MARKERS)
    return False


def _offenders(text: str, *, allow_quote_exemption: bool = True) -> list[str]:
    """返回违规的行（去掉已接受措辞后，逐个命中判定引用豁免）。"""
    found: list[str] = []
    for line in _strip_accepted(text).splitlines():
        for pattern in (CLAIM, NOVERB):
            for match in pattern.finditer(line):
                if allow_quote_exemption and _is_quoted_under_negation(line, match.start()):
                    continue
                found.append(line.strip())
                break
    return found


def _runbook_slice() -> str:
    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.find(RUNBOOK_START)
    end = text.find(RUNBOOK_END)
    assert start >= 0 and end > start, "缺 §10 切片标记（<!-- deepchat-runbook:start/end -->）"
    return text[start:end]


def _description_blocks() -> dict[str, str]:
    """只取各 `dc_*` 入口的 `description=` 表达式文本（不取函数体）。

    第一版按 `id="dc_x"` 到下一个 `@plugin_entry` 切片，结果把**函数体**也扫了进去
    （`self._emit_log("...记忆已清除")` 是一条合法的操作日志），于是误判成"description 过度承诺"。
    用 AST 取 `description=` 关键字的源码片段，面就干净了。
    """
    source = _PLUGIN_INIT.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            callee = getattr(decorator.func, "id", getattr(decorator.func, "attr", ""))
            if callee != "plugin_entry":
                continue
            entry_id: str | None = None
            description: str | None = None
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    entry_id = str(keyword.value.value)
                elif keyword.arg == "description":
                    description = ast.get_source_segment(source, keyword.value)
            if entry_id and entry_id.startswith("dc_") and description is not None:
                blocks[entry_id] = description
    return blocks


class OverPromiseWordingTests(unittest.TestCase):
    """A/B/C：语料自身的判别力（正则若失效，这四条会红）。"""

    def test_bad_claim_samples_are_all_caught(self) -> None:
        missed = [s for s in BAD_CLAIM_SAMPLES if not CLAIM.search(s)]
        self.assertEqual(missed, [], f"这些主张没被 CLAIM 抓住：{missed}")

    def test_bad_no_verbatim_samples_are_all_caught(self) -> None:
        missed = [s for s in BAD_NOVERB_SAMPLES if not NOVERB.search(s)]
        self.assertEqual(missed, [], f"这些主张没被 NOVERB 抓住：{missed}")

    def test_good_samples_are_not_false_positives(self) -> None:
        offenders = _offenders("\n".join(GOOD_SAMPLES))
        self.assertEqual(offenders, [], f"好样本被误伤：{offenders}")

    def test_corpus_is_not_gutted(self) -> None:
        """下限 = 当前实际条数：删样本会红，加样本不会（防"参数化空列表 = 零用例 = 绿"）。"""
        self.assertGreaterEqual(len(BAD_CLAIM_SAMPLES), 10, "坏样本被删过？新增措辞应加语料而不是减")
        self.assertGreaterEqual(len(BAD_NOVERB_SAMPLES), 5, "NOVERB 坏样本被删过")
        self.assertGreaterEqual(len(GOOD_SAMPLES), 11, "好样本被删过？误报清单必须保持覆盖")


class SurfaceClaimTests(unittest.TestCase):
    """D/E：真实文本面。"""

    def test_quote_exemption_is_not_a_loophole(self) -> None:
        """豁免本身要有牙：引用放行，但同一行后半句的违规、以及无引号的裸违规必须仍被抓。"""
        for line in EXEMPT_OK:
            self.assertEqual(_offenders(line), [], f"引用句被误判：{line}")

        for line in EXEMPT_FAIL:
            self.assertTrue(
                _offenders(line),
                f"豁免放过了违规：{line}（后半句/裸违规不得搭便车）",
            )

    def test_runbook_section_and_entry_descriptions_do_not_over_promise(self) -> None:
        runbook_offenders = _offenders(_runbook_slice())

        description_offenders = []
        for entry_id, block in _description_blocks().items():
            # description 里不给"引用豁免"：入口说明不该出现这些词组，出现即需改写
            for line in _offenders(block, allow_quote_exemption=False):
                description_offenders.append(f"{entry_id}: {line}")

        self.assertEqual(runbook_offenders, [], f"§10 runbook 出现过度承诺：{runbook_offenders}")
        self.assertEqual(
            description_offenders,
            [],
            "入口 description 出现过度承诺（或需登记到 ACCEPTED_BLOCKED）："
            f"{description_offenders}",
        )


if __name__ == "__main__":
    unittest.main()
