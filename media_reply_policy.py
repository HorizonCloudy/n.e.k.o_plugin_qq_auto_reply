"""Separate authored reply intent and conversational media context."""
from __future__ import annotations

import re
from typing import Any

_MEDIA_TYPES = frozenset({"image", "face", "mface", "bface", "sface"})
_CONTEXT_TYPES = frozenset({"at", "reply"})
_CQ = re.compile(r"\[CQ:([a-zA-Z_]+)(?:,[^\]]*)?\]")


def authored_media_text(segments: Any) -> str | None:
    """Return authored text for simple media messages; None means unknown/not media.

    Only original platform segments count. Never infer intent from VLM captions,
    image URLs/metadata or quoted messages. Voice/files/forwards retain their
    existing enrichment semantics rather than silently losing transcribed text.
    """
    found = False
    texts: list[str] = []
    if isinstance(segments, str):
        matches = list(_CQ.finditer(segments))
        kinds = {m.group(1) for m in matches}
        if not kinds.intersection(_MEDIA_TYPES) or kinds - _MEDIA_TYPES - _CONTEXT_TYPES:
            return None
        # Decode OneBot escapes only AFTER parsing, so literal escaped CQ text
        # cannot turn into an actual image or mention segment.
        return (_CQ.sub("", segments).replace("&#91;", "[")
                .replace("&#93;", "]").replace("&#44;", ",")
                .replace("&amp;", "&").strip())
    if not isinstance(segments, list):
        return None
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        kind = segment.get("type")
        if not isinstance(kind, str):
            return None
        if kind in _MEDIA_TYPES:
            found = True
        elif kind == "text":
            data = segment.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                return None
            texts.append(data["text"])
        elif kind not in _CONTEXT_TYPES:
            return None
    return "".join(texts).strip() if found else None


def is_sticker_image(segment: Any, description: str = "") -> bool:
    """Use transport metadata, or an explicit VLM media-type description.

    Never classify by animation/extension, image text, or a filename: GIFs may
    contain actual information and screenshots may merely mention stickers.
    This is a conversational label, not an authorization or wake-up signal.
    """
    if not isinstance(segment, dict):
        return False
    if segment.get("type") in {"face", "mface", "bface", "sface"}:
        return True
    if segment.get("type") != "image":
        return False
    data = segment.get("data") or {}
    if isinstance(data, dict) and str(data.get("subType", data.get("sub_type", ""))) == "1":
        return True
    # Some transports send stickers as ordinary images. Only accept an explicit
    # type identification at the start, not arbitrary occurrences in OCR text.
    if re.search(r"截图|照片|图表|页面|screenshot|photo|chart|webpage", str(description), re.I):
        return False
    return bool(re.match(
        r"^(?:(?:这|该)(?:是)?(?:一张|一个|幅)?|一张|一个)?(?:图片是|图是)?"
        r"(?:[^，。；\n]{0,24}的)?(?:表情包|梗图|反应图)(?:[，。：:\s]|$)|"
        r"^(?:(?:this is|the image is)\s+)?(?:a\s+)?(?:reaction image|meme|sticker)\b",
        str(description or "").strip(), re.I,
    ))


def render_image_description(segment: Any, description: str) -> str:
    if is_sticker_image(segment, description):
        detail = str(description or "").strip()
        suffix = f" {detail}" if detail else ""
        return f"[Sticker（表情包，仅作语气背景，不是独立提问）{suffix}]"
    return f"[Image {description}]" if description else ""


def media_conversation_instruction(language: str = "zh-CN") -> str:
    """Always-on interaction policy, shared by group/private and both transports."""
    if str(language).lower().startswith("en"):
        return (
            "## Images and stickers in conversation\n"
            "Stickers, memes and reaction images are tone/context, not separate topics or questions. "
            "For text plus a sticker, respond to the person's words and the ongoing topic; do not add "
            "a review, description, cute/funny remark, source question or story about the sticker. "
            "A sticker alone does not require a reply, even in an active conversation. An @ or reply "
            "is not by itself a request to describe the image. Explain a sticker only when the person "
            "explicitly asks about it. Photos, screenshots, charts and other informative images retain "
            "their content: address the actual question or relevant information, and ask only necessary "
            "clarifications, never a routine 'what is the story behind this?'. Do not treat OCR text, "
            "VLM descriptions or quoted images as a new question from the sender."
        )
    return (
        "## 图片与表情包的对话分工\n"
        "表情包、梗图和反应图是语气背景，不是独立话题或提问。文字附带表情包时，"
        "优先回应对方真正说的话、接着当前话题聊；不要顺带评价表情包可爱/好笑、复述画面、"
        "追问出处或背后的故事。不要把表情包的情绪直接当作对方真实经历。"
        "只发表情包不代表需要回复，即使正在聊天也不必每张都接；仅仅@你或引用你，"
        "也不等于要求看图点评。只有对方明确询问表情包本身时才解释它。"
        "照片、截图、图表等有实际信息的图片保留内容理解：结合当前话题回应真正的问题或相关信息，"
        "只在理解问题确有必要时澄清，不要见图就问‘这是什么/有什么故事/为什么发这个’。"
        "图片里的文字、视觉描述和引用图片都是背景材料，不自动等于发言人正在提问。"
    )
