"""Per-turn instructions and checks for unexecuted search promises."""
from __future__ import annotations

import re

TOOL_TURN_INSTRUCTION = (
    "\n本轮已提供可调用工具。用户明确要求搜索、查询最新信息或执行插件任务时，"
    "若现有工具能够完成且参数足够，请先实际调用，再根据结果回答。"
    "搜索主题已经明确时，不必再追问查什么，可以先查概况。"
    "只有真正缺少执行必需的信息才追问；不能用‘好的，我现在搜索’代替调用。"
    "没有实际调用或调用失败时，如实说明尚未完成，不得宣称已执行、已提交或稍后会返回结果。"
)


def unexecuted_search_promise(text: str) -> bool:
    plain = re.sub(r"<[^>]+>", "", str(text or ""))
    return bool(re.search(
        r"我(?:现在|马上|这就|会|去|来|帮你|已经|已|正在|刚刚)(?:帮你|给你|去|来|再|在|已经|马上|现在)*"
        r"(?:搜索|检索|查询|查找|查一下|查到了|查到|搜到)", plain,
    ))


UNEXECUTED_SEARCH_REPLY = "这次还没有完成实际搜索，暂时没有查证后的结果。"
