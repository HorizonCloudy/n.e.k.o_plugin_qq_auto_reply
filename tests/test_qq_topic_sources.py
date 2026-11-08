"""Public topic provenance survives tool cleanup, only after confirmed delivery."""
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock

import pytest
from plugin.plugins.qq_auto_reply import topic_source_service as t

URL = "https://example.com/cats"
TITLE = "猫咪的有趣生活"


def context(**kw):
    data = dict(sender_id="123", is_group=False, permission_level="admin",
                persist_memory=True, ephemeral_session=False, source_kind="private",
                message="发我看看", turn_uid="turn-1")
    data.update(kw)
    return NS(**data)


@pytest.fixture
def plugin(tmp_path):
    return NS(_admin_qq="123", data_path=lambda name: tmp_path / name, logger=Mock(),
              permission_mgr=NS(get_permission_level=lambda _: "admin"))


def capture(plugin, ctx, payload=None):
    t.capture_topic_sources(plugin, ctx, "better_web_search", "search",
                            payload if payload is not None else {"results": [{"title": TITLE, "url": URL}]})


@pytest.mark.parametrize("payload", [
    {"results": [{"title": TITLE, "url": URL, "snippet": "猫咪"}]},
    {"success": True, "data": {"result": [{"name": TITLE, "link": URL}]}},
    json.dumps({"results": [{"title": TITLE, "url": URL}]}),
    f"[{TITLE}]({URL})",
    {"content": [{"type": "text", "text": f"{TITLE}: {URL}"}]},
])
def test_extract_real_result_shapes(payload):
    sources = t.extract_sources(payload, "better_web_search:search")
    assert len(sources) == 1
    assert sources[0]["url"] == URL
    assert TITLE in sources[0]["title"]


def test_video_id_and_dedup():
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    sources = t.extract_sources({"results": [{"bvid": "BV1xx411c7mD", "title": TITLE},
                                            {"url": url}]}, "neko_video:search")
    assert [s["url"] for s in sources] == [url]
    assert t.extract_sources({"title": TITLE}, "neko_video:search") == []


@pytest.mark.parametrize("url", ["file:///C:/secret", "javascript:alert(1)",
    "http://localhost/x", "http://10.0.0.1/a", "http://[::1]/", "http://a.internal/",
    "https://user:pass@example.com/", "https://example.com/?api_key=secret",
    "https://example.com/#access_token=secret", "https://example.com/a b"])
def test_non_public_urls_rejected(url):
    assert t.public_url(url) == ""


@pytest.mark.parametrize("payload", [
    {"success": False, "results": [{"url": URL}]},
    {"ok": False, "url": URL}, {"status": "FAILED", "url": URL},
    {"status": "processing", "url": URL}, {"credentials": {"url": URL}}, None,
])
def test_no_sources_from_failure_or_unrecognized_fields(payload):
    assert t.extract_sources(payload, "web_search:search") == []


def test_bounds_and_no_secret_urls_in_snippets():
    rows = t.extract_sources({"results": [{"url": URL + str(i), "title": TITLE,
           "snippet": "secret https://example.com/?access_token=hidden"} for i in range(1000)]}, "search")
    assert len(rows) == t.MAX_SOURCES
    assert "hidden" not in json.dumps(rows)
    cyclic = {}; cyclic["data"] = cyclic
    assert t.extract_sources(cyclic, "search") == []


def test_commit_restart_and_original_link_followup(plugin):
    ctx = context(source_kind="proactive_private")
    capture(plugin, ctx)
    assert not plugin.data_path(t.STATE_FILE).exists()
    assert t.topic_source_prompt(plugin, context()).endswith('"omitted": false}')
    t.commit_topic_sources(plugin, ctx, "我看到了" + TITLE)
    service = t.QQTopicSourceService(plugin)
    rows = service.recent("123")
    assert rows[0]["selected"] is True
    assert rows[0]["sources"][0]["url"] == URL
    prompt = service.prompt(context())
    assert URL in prompt and "不要自行拼接或编造链接" in prompt
    assert service.prompt(context(message="晚安")) == ""
    assert service.prompt(ctx) == ""
    t.commit_topic_sources(plugin, ctx, TITLE)
    assert len(plugin._topic_source_service.recent("123")) == 1


def test_ambiguous_candidates_and_empty_new_topic(plugin):
    ctx = context()
    capture(plugin, ctx, {"results": [{"url": URL}, {"url": URL + "2"}]})
    t.commit_topic_sources(plugin, ctx, "今天找到两个好玩的东西")
    rows = plugin._topic_source_service.recent("123")
    assert not rows[0]["selected"] and len(rows[0]["sources"]) == 2
    failed = context(turn_uid="failed")
    capture(plugin, failed, {"success": False})
    t.commit_topic_sources(plugin, failed, "这次没查到有效结果")
    rows = plugin._topic_source_service.recent("123")
    assert rows[0]["sources"] == []
    assert "不能用更早的链接冒充" in t.topic_source_prompt(plugin, context())


@pytest.mark.parametrize("changes", [dict(sender_id="999"), dict(is_group=True),
    dict(permission_level="normal"), dict(persist_memory=False), dict(ephemeral_session=True)])
def test_other_contexts_never_load_or_capture(plugin, changes):
    ctx = context(**changes)
    capture(plugin, ctx)
    t.commit_topic_sources(plugin, ctx, TITLE)
    assert t.topic_source_prompt(plugin, ctx) == ""
    assert not hasattr(plugin, "_topic_source_service")
    assert not plugin.data_path(t.STATE_FILE).exists()


def test_revoked_permission_and_unrelated_tools(plugin):
    ctx = context()
    t.capture_topic_sources(plugin, ctx, "memory", "recall", {"url": URL})
    t.capture_topic_sources(plugin, ctx, "web_search", "login", {"url": URL})
    assert not hasattr(plugin, "_topic_source_service")
    capture(plugin, ctx)
    plugin.permission_mgr.get_permission_level = lambda _: "none"
    t.commit_topic_sources(plugin, ctx, TITLE)
    assert t.topic_source_prompt(plugin, ctx) == ""
    assert not plugin.data_path(t.STATE_FILE).exists()


def test_turn_local_candidates(plugin):
    a, b = context(), context(turn_uid="turn-2")
    capture(plugin, a)
    capture(plugin, b, {"url": URL + "other"})
    t.commit_topic_sources(plugin, b, "第二个")
    assert plugin._topic_source_service.recent("123")[0]["sources"][0]["url"] == URL + "other"
    assert a.topic_source_candidates[0]["url"] == URL


def test_corrupt_expired_and_oversized_state(plugin):
    path = plugin.data_path(t.STATE_FILE)
    for text in ["{broken", "x" * (t.MAX_STATE_BYTES + 1), json.dumps({"123": [{"at": 0}]})]:
        path.write_text(text, encoding="utf-8")
        assert t.QQTopicSourceService(plugin).recent("123") == []


def test_write_failure_retains_in_memory(plugin):
    ctx = context(); capture(plugin, ctx)
    plugin.data_path = lambda _: (_ for _ in ()).throw(OSError("read only"))
    t.commit_topic_sources(plugin, ctx, TITLE)
    assert URL in t.topic_source_prompt(plugin, context())
    plugin.logger.warning.assert_called()


def test_prompt_has_bounded_valid_json(plugin):
    service = t.QQTopicSourceService(plugin)
    service._topics = {"123": [dict(at=time.time(), message="x" * 800, turn=str(j),
        sources=[dict(url="https://example.com/" + str(i) + "x" * 1900,
                      title="x" * 160, summary="x" * 240) for i in range(8)]) for j in range(4)]}
    prompt = service.prompt(context())
    assert len(prompt) < t.MAX_PROMPT_DATA + 1500
    data = json.loads(prompt.splitlines()[-1])
    assert data["omitted"] and data["topics"][0]["truncated"]


def test_async_keeps_raw_sources_before_display_truncation(plugin):
    record = dict(key="task1", plugin_id="better_web_search", entry_id="search",
                  conversation=vars(context()), terminal="done", output="truncated")
    payload = {"data": {"text": "a" * 4000, "results": [{"url": URL, "title": TITLE}]}}
    t.prepare_async_topic_sources(plugin, record, payload)
    assert not plugin.data_path(t.STATE_FILE).exists()
    t.commit_async_topic_sources(plugin, record, NS(reply_text=TITLE))
    assert URL in t.topic_source_prompt(plugin, context())


@pytest.mark.asyncio
async def test_bridge_callback_gets_untruncated_payload_and_refuses_unmounted(plugin):
    from plugin.plugins.qq_auto_reply.plugin_tool_service import QQPluginToolService
    plugin._qq_settings = {"qq_open_plugin_tools": {"better_web_search": "admin"}}
    plugin.qq_client = NS(CHANNEL="open", mode="open_platform", needs_attention=False)
    service = QQPluginToolService(plugin)
    raw = {"data": "a" * 4000, "results": [{"url": URL}]}
    service.call_plugin_entry_with_payload = AsyncMock(return_value=("short", raw))
    service._register_followup = AsyncMock(return_value=False)
    cb = Mock()
    handler = service.build_handler([({"plugin_id": "better_web_search", "entries": ["search"]}, "admin")],
                 conversation=vars(context()), on_result=cb)
    await handler(NS(call_id="1", name="plugin_better_web_search", arguments={"entry_id": "search"}))
    assert cb.call_args.args == ("better_web_search", "search", raw)
    await handler(NS(call_id="2", name="plugin_better_web_search", arguments={"entry_id": "delete"}))
    assert cb.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_pipeline_saves_only_after_success(plugin, delivered):
    from plugin.plugins.qq_auto_reply.pipeline_models import QQDeliveryResult, QQMessageBlock, QQReplyOutcome
    from plugin.plugins.qq_auto_reply.reply_pipeline import QQReplyPipelineRunner
    ctx = context(); capture(plugin, ctx)
    plugin.reply_buffer_service = None
    plugin.reply_generation_service = NS(record_scoped_mentions_on_delivery=AsyncMock())
    async def send(*args, **kwargs):
        assert not plugin.data_path(t.STATE_FILE).exists()
        return QQDeliveryResult(delivered=delivered, target_type="private", target_id="123", reply_text=TITLE)
    plugin.reply_delivery_node = NS(deliver=send)
    runner = QQReplyPipelineRunner(plugin)
    runner._handle_forward_marks = AsyncMock()
    runner._consent_revoked_before_send = lambda _: False
    outcome = QQReplyOutcome(action="reply", reply_text=TITLE)
    plan = NS(target_type="private", target_id="123", blocks=[QQMessageBlock(text=TITLE)])
    await runner._run_delivery(plan, None, outcome, ctx)
    assert plugin.data_path(t.STATE_FILE).exists() is delivered

@pytest.mark.asyncio
async def test_primary_followup_injection_is_turn_scoped(plugin):
    import asyncio
    from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService, SystemMessage
    ctx = context(attachments=None, prompt_message="发我看看", system_prompt="猫娘", recalled_memory_text="")
    capture(plugin, ctx); t.commit_topic_sources(plugin, ctx, TITLE)
    plugin._queue_attachment_images = AsyncMock(return_value=0)
    plugin._wait_session_response_complete = AsyncMock(return_value=True)
    plugin._ai_turn_timeout_seconds = 1
    plugin._qq_settings = {}
    service = QQReplyGenerationService(plugin)
    service._sanitize_for_live_consent = lambda c, s, r: (s, r)
    service._consent_dependency_snapshot = lambda c: {}
    service._store_consent_snapshot = lambda *a: None
    service._consent_dependency_revoked = lambda *a: False
    service._stamp_nonconsent_boundary = lambda *a: None
    service._arm_turn_tools = AsyncMock(return_value=(False, False, False))
    session = NS(_conversation_history=[SystemMessage(content="original")], _instructions="original")
    chunks = []
    async def stream(message):
        assert message == "发我看看"
        assert URL in session._conversation_history[0].content
        assert URL in session._instructions
        chunks.append("原链接：" + URL)
    session.stream_text = stream
    result = await service._run_session_generation(context=ctx, session_key="private:123",
                 user_data={"lock": asyncio.Lock()}, user_session=session, reply_chunks=chunks)
    assert result == "原链接：" + URL
    assert session._instructions == "original"
    assert session._conversation_history[0].content == "original"


@pytest.mark.asyncio
async def test_fallback_also_receives_original_source(plugin, monkeypatch):
    from plugin.plugins.qq_auto_reply import reply_generation_service as g
    import utils.config_manager as config
    ctx = context(attachments=None, prompt_message="链接呢", system_prompt="猫娘", recalled_memory_text="")
    capture(plugin, ctx); t.commit_topic_sources(plugin, ctx, TITLE)
    plugin._should_skip_direct_llm_fallback_for_images = lambda **kw: False
    plugin._ai_turn_timeout_seconds = 1
    llm = NS(ainvoke=AsyncMock(return_value=NS(content=URL)), aclose=AsyncMock())
    monkeypatch.setattr(config, "get_config_manager", lambda: NS(get_model_api_config=lambda _: {
        "model": "test", "base_url": "http://unused.invalid", "api_key": ""}))
    monkeypatch.setattr(g, "create_chat_llm_async", AsyncMock(return_value=llm))
    service = g.QQReplyGenerationService(plugin)
    service._sanitize_for_live_consent = lambda c, s, r: (s, r)
    service._consent_dependency_snapshot = lambda c: {}
    service._store_consent_snapshot = lambda *a: None
    service._consent_dependency_revoked = lambda *a: False
    assert await service.generate_reply_fallback_direct_llm(context=ctx) == URL
    assert URL in llm.ainvoke.call_args.args[0][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_buffer_commits_after_real_delivery(plugin, delivered):
    import asyncio
    from plugin.plugins.qq_auto_reply.reply_buffer_service import QQReplyBufferService, PendingReply
    ctx = context(); capture(plugin, ctx)
    pending = PendingReply(first_text=TITLE, wait_seconds=0, sender_id="123", is_group=False, group_id="")
    pending.mention_context = ctx
    plugin._qq_settings = {}
    plugin._emit_log = Mock()
    plugin._user_sessions = {}
    plugin.reply_generation_service = NS(record_scoped_mentions_on_delivery=AsyncMock())
    async def send(*args, **kwargs):
        assert not plugin.data_path(t.STATE_FILE).exists()
        return NS(delivered=delivered)
    async def locked(key, fn):
        await fn()
    plugin.reply_delivery_node = NS(deliver=send)
    plugin._run_with_session_lock = locked
    plugin._spawn_memory_sync_task = lambda coro, **kw: asyncio.create_task(coro)
    service = QQReplyBufferService(plugin)
    service._consent_revoked_since = lambda _: False
    service._clear_undelivered_marks = lambda *a: None
    service._settle_provisional = lambda *a: None
    service._pending["private:123"] = pending
    await service._deliver_after_wait("private:123", pending)
    assert plugin.data_path(t.STATE_FILE).exists() is delivered
