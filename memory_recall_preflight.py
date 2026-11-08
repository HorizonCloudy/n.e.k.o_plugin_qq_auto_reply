"""Narrow explicit memory requests; routing/authorization stays in the memory tool."""
from __future__ import annotations

import re


def explicit_memory_query(message: str) -> str:
    text = re.sub(r"\[CQ:[^\]]*\]", "", str(message or "")).strip()
    # Do not treat reminders (记得关窗), general knowledge, or background chatter
    # as permission to perform a memory read. This only chooses WHEN, never WHERE.
    personal = bool(re.search(r"我|咱|我们|你(?:曾|之前|以前|上次|刚才)|咱们", text))
    past = bool(re.search(r"之前|以前|上次|刚才|刚刚|昨天|前天|曾经|过去|那天|当时|刚(?:写|说|聊|看|答应)", text))
    question = bool(re.search(r"[？?]|吗|么|哪|多少|记不记得|还记得|你记得|是否", text))
    recall = bool(re.search(r"记得|记不记得|记忆|记录|说过|聊过|答应过|写过|约定|承诺", text))
    lookup = bool(re.search(r"(?:查|找|翻|搜|回忆).{0,12}(?:记忆|记录|之前|以前|上次|聊过|说过)", text))
    english = bool(re.search(
        r"\b(?:do you remember|can you recall)\b.{0,100}\b(?:I|we|my|our)\b|"
        r"\b(?:what|which)\b.{0,80}\b(?:I|we)\b.{0,40}\b(?:said|wrote|promised|promise|told)\b", text, re.I))
    if english or (personal and ((past and question) or (recall and question) or lookup)):
        return text[:600]
    return ""


def preflight_instruction(status: str, *, has_memory: bool) -> str:
    if not status:
        return ""
    if status == "hit" and has_memory:
        result = "本轮已实际检索，结果已放入本轮长期记忆段。依据相关原文直接回答，不要编造缺失的引文。"
    elif status == "failed":
        result = "本轮记忆检索未能完成。请如实说明检索失败，不得声称已经查到或把失败说成没有这件事。"
    else:
        result = "本轮未获得可用于回答的相关记忆（也可能授权已撤销）。请如实说当前无法从可用记录确认，不要据此断言事情没有发生。"
    return "\n【本轮记忆检索状态】\n" + result + "本轮不要重复调用 recall_memory，也不要只回复‘我先查一下’或承诺稍后返回；没有后台查询会继续。\n"


def memory_promise_only_reply(text: str, status: str) -> str:
    """Last-resort guard for the observed promise-only response, not real answers."""
    if not status:
        return text
    plain = re.sub(r"<[^>]+>", "", str(text or "")).strip()
    if len(plain) > 90 or not re.fullmatch(
        r"(?:好[的呀啊]?[,，。！!\s]*)?我(?:得|需要|要|会|先|这就|现在|马上|去|再|帮你|来){1,5}"
        r"(?:查|找|翻|检索|查询|回忆)(?:一下|一查|查|找)?"
        r"(?:之前的|以前的|相关的|一下|的|记忆|记录|内容|哦|喔|哈|呀|啊|呢|[,，。！!~～\s]|等我一下|稍等|等一下|稍后告诉你)*", plain):
        return text
    if status == "hit":
        return "已经检索了本轮可用记忆，但还没能据此形成可靠的回答。"
    if status == "failed":
        return "这次记忆检索未能完成，暂时无法确认之前的记录。"
    return "这次没有从当前可用记忆中找到能确认的记录，不能确定原文。"
