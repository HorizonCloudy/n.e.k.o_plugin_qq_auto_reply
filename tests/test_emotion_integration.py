from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plugin.plugins.qq_auto_reply import settings_schema
from plugin.plugins.qq_auto_reply.deepchat.emotion import (
    EMOTION_MULTIPLIER,
    MOOD_ANCHORS,
    MOOD_LABELS,
    Mood,
    emotion_fragment,
)
from plugin.plugins.qq_auto_reply.deepchat.service import DeepChatService
from plugin.plugins.qq_auto_reply.emotion_service import build_emotion_section


@pytest.fixture
def plugin(tmp_path):
    plugin = SimpleNamespace(_qq_settings={}, data_path=lambda: tmp_path)
    plugin.deepchat_service = DeepChatService(plugin)
    plugin.attention_service = SimpleNamespace(
        _enabled=lambda: True, get_state=Mock(return_value=SimpleNamespace(emotion="annoyed")),
    )
    return plugin


def test_shared_vocabulary_covers_all_consumers():
    values = {m.value for m in Mood}
    assert values == set(EMOTION_MULTIPLIER) == set(settings_schema.DEFAULT_EMOTION_MULTIPLIERS)
    assert set(Mood) == set(MOOD_LABELS) == set(MOOD_ANCHORS)
    for mood in Mood:
        assert mood.value in emotion_fragment(mood, is_group=False)


async def test_scope_selection_and_no_private_fallback(plugin):
    plugin.deepchat_service.set_mood("u", "happy")
    group = await build_emotion_section(plugin, is_group=True, group_id="g", sender_id="u")
    private = await build_emotion_section(plugin, is_group=False, group_id=None, sender_id="u")
    assert "annoyed" in group and "happy" not in group
    assert "happy" in private and "annoyed" not in private
    plugin.attention_service.get_state.return_value.emotion = "calm"
    assert await build_emotion_section(plugin, is_group=True, group_id="g", sender_id="u") == ""
    plugin.attention_service._enabled = lambda: False
    assert await build_emotion_section(plugin, is_group=True, group_id="g", sender_id="u") == ""


async def test_switches_and_synthetic_private_turns(plugin):
    kwargs = dict(is_group=False, group_id=None, sender_id="u")
    assert await build_emotion_section(plugin, **kwargs, synthetic=True) == ""
    for switch in ("deepchat_enabled", "deepchat_fragments_enabled"):
        plugin._qq_settings[switch] = False
        assert await build_emotion_section(plugin, **kwargs) == ""
        # Disabling DeepChat does not disable the existing group state.
        assert "annoyed" in await build_emotion_section(plugin, is_group=True, group_id="g", sender_id="u")
        plugin._qq_settings[switch] = True


def test_group_writeback_does_not_mutate_private_affect(plugin):
    service = plugin.deepchat_service
    service.mood.set_mood("user:u", "happy", now=1000)
    before = service.mood.get("user:u", now=1000)
    result = service.writeback(sender_id="u", text="讨厌你", is_group=True, group_id="g")
    after = service.mood.get("user:u", now=1000)
    assert after == before
    assert result["mood"] is None
    assert service.relationship.get("u").interactions == 1


async def test_private_read_runs_off_loop_under_writeback_lock(plugin):
    main_thread = threading.get_ident()
    original = plugin.deepchat_service.mood.get
    seen = []

    def get(*args, **kwargs):
        seen.append((threading.get_ident(), plugin.deepchat_service._writeback_lock.locked()))
        return original(*args, **kwargs)

    plugin.deepchat_service.mood.get = get
    await asyncio.wait_for(
        build_emotion_section(plugin, is_group=False, group_id=None, sender_id="u"), timeout=2,
    )
    assert len(seen) == 1 and seen[0][0] != main_thread and seen[0][1]
