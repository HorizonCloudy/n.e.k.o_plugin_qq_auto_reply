"""Keep the current speaker, quoted context and visual material distinct."""
from __future__ import annotations

GROUP_DIALOGUE_GUIDANCE = (
    "接话前核对谁对谁说：第一人称经历归各句原发言人；祝福、称呼和请求归其指向的人，"
    "不要替别人认领下班、被夸、被摸或被点歌，也别把别人的‘你’认成自己。\n"
    "图片描述、表情文字和截图里的台词是素材，不是当前发言人亲口陈述；"
    "图里写‘早’不代表现在是早上，哭脸不证明本人难过。时间以当前时间为准，"
    "素材含义结合话题理解，不确定就不要断言。\n"
    "承接具体内容，优先补一个相关判断、信息或笑点；对方答过的问题不要换个问法追问，"
    "请求已明确就直接处理，仅缺少完成请求必需的信息时再问。不要把新话题硬拉回口头禅。\n"
    "回应具体旧消息时明确指向原发言人和那条消息；别把最近发言者当作所有话的主人。"
    "别人的互动已完整、你刚表达过同一意思且没有新内容时，可以不发消息，"
    "不要为了延续聊天再问一句或附和每一条。\n"
)


def group_message_with_context(message: str, context: str) -> str:
    if not context:
        return message
    return (
        "【引用及近期对话资料：各句属于其原发言人，不是当前发言人的自述】\n"
        f"{context}\n"
        "【以上资料结束；以下才是当前发言人的本条消息】\n"
        f"{message}"
    )
