"""Preset selection must survive the full settings save chain without resetting consent."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply import reply_presets as presets
from plugin.plugins.qq_auto_reply import settings_schema
from plugin.plugins.qq_auto_reply.config_store import QQAutoReplyConfigStore
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService


def service(settings=None):
    plugin = SimpleNamespace(
        _qq_settings=deepcopy(settings if settings is not None else settings_schema.defaults()),
        _emit_log=lambda *a, **k: None, _ensure_qq_client_initialized=lambda: None,
        attention_service=None, qq_client=None, _running=False, _user_sessions={},
        config_store=QQAutoReplyConfigStore,
    )
    svc = QQSettingsService(plugin)
    svc.persist_business_config = AsyncMock(return_value=True)
    return svc


@pytest.mark.parametrize('name', ['fast', 'balanced', 'quiet'])
async def test_full_save_applies_and_persists_named_bundle(name):
    svc = service()
    snapshots = []
    async def persist(overlay=None):
        snapshots.append(deepcopy(svc.plugin._qq_settings))
        return True
    svc.persist_business_config = persist
    result = await svc.save_settings(reply_behavior_preset=name, buffer_collect_window_seconds=59)
    assert result['persisted'] is True
    for key, value in presets.preset_values(name).items():
        assert svc.plugin._qq_settings[key] == value, key
        assert snapshots[-1][key] == value, key
    assert snapshots[-1][presets.PRESET_KEY] == name
    assert presets.effective_preset(snapshots[-1]) == name


def test_old_custom_settings_are_detected_without_mutation():
    old = settings_schema.defaults()
    old.pop(presets.PRESET_KEY)
    old['buffer_collect_window_seconds'] = 7
    before = deepcopy(old)
    assert presets.effective_preset(old) == 'custom'
    assert old == before
    assert presets.effective_preset({}) == 'balanced'


async def test_legacy_api_edit_marks_custom_but_unrelated_save_does_not_apply_defaults():
    svc = service()
    await svc.save_settings(buffer_collect_window_seconds=7)
    assert svc.plugin._qq_settings[presets.PRESET_KEY] == 'custom'
    await svc.save_settings(force_sentence_split=True)
    assert svc.plugin._qq_settings['buffer_collect_window_seconds'] == 7
    await svc.save_settings(reply_behavior_preset='custom')
    assert svc.plugin._qq_settings['buffer_collect_window_seconds'] == 7


@pytest.mark.parametrize('failure', ['false', 'exception', 'invalid'])
async def test_failure_rolls_back_all_managed_values_and_marker(failure):
    svc = service({})
    before = deepcopy(svc.plugin._qq_settings)
    if failure == 'false':
        svc.persist_business_config.return_value = False
        assert not (await svc.save_settings(reply_behavior_preset='fast'))['persisted']
    elif failure == 'exception':
        svc.persist_business_config.side_effect = OSError('disk full')
        with pytest.raises(OSError):
            await svc.save_settings(reply_behavior_preset='quiet')
    else:
        with pytest.raises(ValueError):
            await svc.save_settings(reply_behavior_preset='custom', attention_max_score=20,
                                    attention_fall_rate=float('nan'))
    for key in (*presets.MANAGED_KEYS, presets.PRESET_KEY):
        assert (key in svc.plugin._qq_settings) == (key in before)
        assert svc.plugin._qq_settings.get(key) == before.get(key)


async def test_invalid_preset_rejected_before_mutation():
    svc = service()
    before = deepcopy(svc.plugin._qq_settings)
    with pytest.raises(ValueError):
        await svc.save_settings(reply_behavior_preset='typo', group_memory_enabled=True)
    assert svc.plugin._qq_settings == before
    svc.persist_business_config.assert_not_called()


async def test_presets_never_enable_memory_or_initiative_or_disable_safety():
    svc = service()
    independent = {key: value for key, value in svc.plugin._qq_settings.items()
                   if key not in presets.MANAGED_KEYS and key != presets.PRESET_KEY}
    for name in ('fast', 'quiet', 'balanced'):
        await svc.save_settings(reply_behavior_preset=name)
        for key, value in independent.items():
            assert svc.plugin._qq_settings.get(key) == value, key


def test_presets_are_independent_copies_and_low_frequency_is_not_incoming_tempo():
    balanced = presets.preset_values('balanced')
    quiet = presets.preset_values('quiet')
    assert quiet['attention_frequency_target_gap'] == balanced['attention_frequency_target_gap']
    assert quiet['reply_burst_max_replies'] / quiet['reply_burst_window_seconds'] < (
        balanced['reply_burst_max_replies'] / balanced['reply_burst_window_seconds'])
    assert quiet['attention_fall_rate'] < balanced['attention_fall_rate']
    assert quiet['dormancy_idle_seconds'] > balanced['dormancy_idle_seconds']
    balanced['attention_emotion_multipliers'].clear()
    assert presets.preset_values('balanced')['attention_emotion_multipliers']

async def test_custom_inherits_internal_preset_knobs_across_save():
    svc = service()
    await svc.save_settings(reply_behavior_preset='fast')
    await svc.save_settings(reply_behavior_preset='custom', buffer_collect_window_seconds=4)
    assert svc.plugin._qq_settings[presets.PRESET_KEY] == 'custom'
    assert svc.plugin._qq_settings['buffer_collect_window_seconds'] == 4
    assert svc.plugin._qq_settings['attention_fall_rate'] == 0
    assert svc.plugin._qq_settings['dormancy_enabled'] is False


async def test_post_commit_cleanup_failure_does_not_roll_back_durable_values():
    svc = service()
    def fail_cleanup():
        raise RuntimeError('cleanup failed after write')
    svc.plugin.attention_service = SimpleNamespace(cleanup_stale_cache=fail_cleanup)
    with pytest.raises(RuntimeError, match='cleanup failed'):
        await svc.save_settings(reply_behavior_preset='quiet')
    assert presets.effective_preset(svc.plugin._qq_settings) == 'quiet'
    svc.persist_business_config.assert_awaited_once()


async def test_serialized_failure_cannot_roll_back_a_later_success():
    import asyncio
    svc = service()
    writes = 0
    async def persist(overlay=None):
        nonlocal writes
        writes += 1
        await asyncio.sleep(0)
        return writes > 1
    svc.persist_business_config = persist
    failed, succeeded = await asyncio.gather(
        svc.save_settings(reply_behavior_preset='fast'),
        svc.save_settings(reply_behavior_preset='quiet'),
    )
    assert not failed['persisted'] and succeeded['persisted']
    assert presets.effective_preset(svc.plugin._qq_settings) == 'quiet'
