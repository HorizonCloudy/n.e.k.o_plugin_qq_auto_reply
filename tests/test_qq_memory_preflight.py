"""Regression: a model making zero tool calls must still receive authorized recall."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from test_qq_memory_directionality import plugin, context as memory_context
from plugin.plugins.qq_auto_reply.memory_bridge import QQMemoryQueryResult
from plugin.plugins.qq_auto_reply.memory_tool_service import QQMemoryToolService
from plugin.plugins.qq_auto_reply.memory_recall_preflight import (
    explicit_memory_query, memory_promise_only_reply,
)
from plugin.plugins.qq_auto_reply.pipeline_models import SYNTHETIC_SOURCE_KINDS
from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService, SystemMessage, AIMessage

QUESTION = '我之前写的《雨衣》，你记得哪两句？'
PROMISE = '我得先查一下之前的记录哦，等我一下~'


def ctx(p, **kw):
    defaults = dict(message=QUESTION, prompt_message=QUESTION, attachments=None,
                    recalled_memory_used=False, traces=[])
    defaults.update(kw)
    return memory_context(p, **defaults)


def generation(p):
    p.memory_tool_service = QQMemoryToolService(p)
    p._queue_attachment_images = AsyncMock(return_value=0)
    p._wait_session_response_complete = AsyncMock(return_value=True)
    p._ai_turn_timeout_seconds = 1
    service = QQReplyGenerationService(p)
    service._arm_turn_tools = AsyncMock(return_value=(False, False, False))
    return service


@pytest.mark.parametrize('text', [QUESTION, '知道我刚写了什么吗', '我之前答应过你什么？',
    '你还记得我叫什么吗？', '帮我查一下之前的聊天记录', 'Do you remember what I wrote?',
    'What did we promise last time?'])
def test_explicit_questions(text):
    assert explicit_memory_query(text)


@pytest.mark.parametrize('text', ['记得关窗', '记得明天提醒我买菜', '你记得光速是多少吗',
    '雨衣怎么洗？', '今天天气如何', '之前下过雨', '我之前写了一首诗，今天又写了一首', '',
    '我想写一首诗', '什么是长期记忆？'])
def test_not_every_turn_is_a_recall(text):
    assert not explicit_memory_query(text)


@pytest.mark.asyncio
async def test_zero_tool_model_gets_result_and_next_turn_has_no_stale_prompt(plugin):
    service = generation(plugin)
    context = ctx(plugin)
    session = NS(_conversation_history=[SystemMessage(content='original')], _instructions='original')
    chunks = []
    async def stream(message):
        assert plugin.memory_bridge.query_relevant_memory.await_count == 2
        assert 'PRIVATE_FRIEND' in session._instructions
        assert 'GROUP_FACTS' in session._instructions
        assert '本轮已实际检索' in session._instructions
        chunks.append('根据已查到的记录回答')
        session._conversation_history.append(AIMessage(content=chunks[-1]))
    session.stream_text = stream
    assert await service._run_session_generation(context=context, session_key='group:30003',
        user_data={'lock': asyncio.Lock()}, user_session=session, reply_chunks=chunks) == '根据已查到的记录回答'
    assert context.memory_preflight_status == 'hit'
    assert context.consent_snapshot[context.group_private_memory_token]
    assert context.traces[-1].stage == 'memory_recall_preflight'
    assert session._instructions == session._conversation_history[0].content == 'original'
    # Next speaker, no retrospective question, no retrieval or lingering raw text.
    other = ctx(plugin, message='早上好', prompt_message='早上好', sender_id='other', group_private_memory_token='')
    async def next_stream(message):
        assert 'PRIVATE_FRIEND' not in session._instructions
        assert 'GROUP_FACTS' not in session._instructions
        chunks.append('早上好')
    session.stream_text = next_stream
    await service._run_session_generation(context=other, session_key='group:30003',
        user_data={'lock': asyncio.Lock()}, user_session=session, reply_chunks=chunks)
    assert plugin.memory_bridge.query_relevant_memory.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('permission', ['admin', 'trusted', 'normal'])
async def test_preflight_uses_existing_scopes_and_one_call_budget(plugin, permission):
    plugin.permission_mgr.get_permission_level.return_value = permission
    service = generation(plugin); context = ctx(plugin, permission=permission)
    await service._ensure_explicit_memory_recall(context)
    calls = plugin.memory_bridge.query_relevant_memory.call_args_list
    assert len(calls) == 2
    private_scope = calls[1].kwargs['subjects']
    if permission == 'admin':
        assert private_scope is None
    else:
        assert private_scope == [plugin.memory_bridge.participant_subject(context.sender_id)]
    # A fallback retry and a model tool call cannot spend another retrieval budget.
    await service._ensure_explicit_memory_recall(context)
    handler = service._build_recall_tool_handler(context=context, consent_before={})
    await handler(NS(name='recall_memory', call_id='again', arguments={'query': 'another'}))
    assert len(plugin.memory_bridge.query_relevant_memory.call_args_list) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', sorted(SYNTHETIC_SOURCE_KINDS))
async def test_synthetic_never_preflights(plugin, kind):
    service = generation(plugin)
    await service._ensure_explicit_memory_recall(ctx(plugin, source_kind=kind))
    plugin.memory_bridge.query_relevant_memory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('disabled', ['turn', 'live'])
async def test_disabled_memory_never_reads(plugin, disabled):
    service = generation(plugin); context = ctx(plugin)
    if disabled == 'turn': context.use_memory_context = False
    else: plugin._qq_settings['group_memory_enabled'] = False
    await service._ensure_explicit_memory_recall(context)
    plugin.memory_bridge.query_relevant_memory.assert_not_called()


@pytest.mark.asyncio
async def test_read_revocation_drops_result(plugin):
    service = generation(plugin); context = ctx(plugin)
    async def query(*a, **kw):
        plugin._qq_settings['group_member_memory_enabled'] = False
        return QQMemoryQueryResult(text='SECRET', hit_count=1)
    plugin.memory_bridge.query_relevant_memory.side_effect = query
    await service._ensure_explicit_memory_recall(context)
    assert context.memory_preflight_status == 'empty'
    assert not context.recalled_memory_text
    assert not context.consent_snapshot


@pytest.mark.asyncio
async def test_generation_revocation_drops_answer_and_history(plugin):
    service = generation(plugin); context = ctx(plugin)
    session = NS(_conversation_history=[SystemMessage(content='original')], _instructions='original')
    chunks = []
    async def stream(message):
        assert 'PRIVATE_FRIEND' in session._instructions
        plugin._qq_settings['private_participant_memory_enabled'] = False
        chunks.append('SECRET')
        session._conversation_history.append(AIMessage(content='SECRET'))
    session.stream_text = stream
    assert await service._run_session_generation(context=context, session_key='group:30003',
        user_data={'lock': asyncio.Lock()}, user_session=session, reply_chunks=chunks) == ''
    assert len(session._conversation_history) == 1
    assert service._consent_dependency_revoked(context, context.consent_snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['empty', 'error', 'timeout'])
async def test_no_hit_or_failure_is_bounded_and_recorded(plugin, monkeypatch, result):
    import plugin.plugins.qq_auto_reply.reply_generation_service as g
    service = generation(plugin); context = ctx(plugin)
    if result == 'empty':
        plugin.memory_bridge.query_relevant_memory.return_value = QQMemoryQueryResult()
        plugin.memory_bridge.query_relevant_memory.side_effect = None
    elif result == 'error':
        plugin.memory_bridge.query_relevant_memory.side_effect = OSError('offline')
    else:
        cancelled = asyncio.Event()
        async def slow(**kw):
            try: await asyncio.sleep(30)
            finally: cancelled.set()
        plugin.memory_tool_service.execute_recall = slow
        monkeypatch.setattr(g, 'RECALL_TOOL_HTTP_TIMEOUT_SECONDS', -0.99)
    await service._ensure_explicit_memory_recall(context)
    assert context.memory_preflight_status == ('empty' if result == 'empty' else 'failed')
    assert not context.recalled_memory_text
    if result == 'timeout': assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_first', [False, True])
async def test_direct_fallback_uses_same_recall_and_does_not_repeat(plugin, monkeypatch, primary_first):
    import plugin.plugins.qq_auto_reply.reply_generation_service as g
    import utils.config_manager as config
    service = generation(plugin); context = ctx(plugin)
    plugin._should_skip_direct_llm_fallback_for_images = lambda **kw: False
    llm = NS(ainvoke=AsyncMock(return_value=NS(content='原文回答')), aclose=AsyncMock())
    monkeypatch.setattr(config, 'get_config_manager', lambda: NS(get_model_api_config=lambda _: {
        'model': 'test', 'base_url': 'http://unused.invalid', 'api_key': ''}))
    monkeypatch.setattr(g, 'create_chat_llm_async', AsyncMock(return_value=llm))
    if primary_first: await service._ensure_explicit_memory_recall(context)
    assert await service.generate_reply_fallback_direct_llm(context=context) == '原文回答'
    assert 'PRIVATE_FRIEND' in llm.ainvoke.call_args.args[0][0]['content']
    assert plugin.memory_bridge.query_relevant_memory.await_count == 2


@pytest.mark.parametrize('status', ['hit', 'empty', 'failed'])
def test_promise_only_backstop(status):
    assert memory_promise_only_reply(PROMISE, status) != PROMISE
    real = '我记得你写的是“身附雨衣”“以雨为衣”。'
    assert memory_promise_only_reply(real, status) == real
    assert memory_promise_only_reply(PROMISE, '') == PROMISE


@pytest.mark.asyncio
async def test_promise_guard_updates_history_too(plugin):
    service = generation(plugin); context = ctx(plugin)
    session = NS(_conversation_history=[SystemMessage(content='original')], _instructions='original')
    chunks = []
    async def stream(message):
        chunks.append(PROMISE)
        session._conversation_history.append(AIMessage(content=PROMISE))
    session.stream_text = stream
    text = await service._run_session_generation(context=context, session_key='group:30003',
        user_data={'lock': asyncio.Lock()}, user_session=session, reply_chunks=chunks)
    assert text != PROMISE
    assert session._conversation_history[-1].content == text
