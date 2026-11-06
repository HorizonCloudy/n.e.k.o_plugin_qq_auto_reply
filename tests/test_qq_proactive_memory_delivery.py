"""A proactive reply reaches memory only after confirmed delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply.pipeline_models import (
    QQDeliveryResult,
    QQMessageBlock,
    QQReplyOutcome,
    QQReplyRequest,
)
from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService
from plugin.plugins.qq_auto_reply.reply_pipeline import QQReplyPipelineRunner
from plugin.plugins.qq_auto_reply.session_memory_service import QQSessionMemoryService


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_proactive_memory_waits_for_delivery(delivered):
    prompt = SimpleNamespace(type="human", content="[system] initiate a conversation")
    answer = SimpleNamespace(type="ai", content="Hello again")
    data = dict(session=SimpleNamespace(_conversation_history=[prompt, answer]),
                memory_enabled=True, her_name="Neko", is_group=False)
    key = "private:123"
    plugin = SimpleNamespace(
        _user_sessions={key: data}, logger=Mock(), reply_buffer_service=None,
        _build_session_key=lambda **kw: key,
    )
    memory = plugin.session_memory_service = QQSessionMemoryService(plugin)
    memory.post_memory_history = AsyncMock(return_value={"status": "ok"})
    plugin._cache_session_delta = memory.cache_session_delta
    generation = plugin.reply_generation_service = QQReplyGenerationService(plugin)
    generation.record_scoped_mentions_on_delivery = AsyncMock()
    context = SimpleNamespace(source_kind="proactive_private", is_group=False)
    memory.record_synthetic_prompt_rows(key, 0)

    await generation._sync_memory_after_success(
        session_key=key, user_data=data, context=context,
    )
    memory.post_memory_history.assert_not_awaited()

    async def send(*args, **kwargs):
        memory.post_memory_history.assert_not_awaited()
        return QQDeliveryResult(delivered=delivered, target_type="private",
                                target_id="123", reply_text="Hello again")

    plugin.reply_delivery_node = SimpleNamespace(deliver=send)
    runner = QQReplyPipelineRunner(plugin)
    runner._handle_forward_marks = AsyncMock()
    runner._consent_revoked_before_send = lambda ctx: False
    request = QQReplyRequest(message_text=prompt.content, sender_id="123",
                             source_kind="proactive_private")
    outcome = QQReplyOutcome(action="reply", history_ai_row=answer, reply_text="Hello again")
    plan = SimpleNamespace(target_type="private", target_id="123",
                           blocks=[QQMessageBlock(text="Hello again")])
    await runner._run_delivery(plan, request, outcome, context)
    if delivered:
        memory.post_memory_history.assert_awaited_once()
        messages = memory.post_memory_history.call_args.args[2]
        assert [m["role"] for m in messages] == ["assistant"]
        assert messages[0]["content"][0]["text"] == "Hello again"
        assert data["last_synced_index"] == 2
    else:
        await memory.cache_session_delta(key, data, sync_tail_ai=True)
        memory.post_memory_history.assert_not_awaited()
