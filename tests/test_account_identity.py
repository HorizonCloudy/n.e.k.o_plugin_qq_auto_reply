import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply.account_identity import account_identity


@pytest.mark.parametrize("needs_attention,login,expected", [
    (True, {"status": "online", "self_id": "10001"}, "10001"),
    (True, {"status": "offline", "self_id": "10001"}, ""),
    (True, {"status": "error", "self_id": "10001"}, ""),
    (True, {"status": "online", "self_id": ""}, ""),
    (True, {"status": "online", "self_id": "bot-open-id"}, ""),
    (True, {"status": "online", "self_id": "0"}, ""),
    (True, {"status": "online", "self_id": "１２３"}, ""),
    (False, {"status": "online", "self_id": "10001"}, ""),
])
def test_only_live_onebot_identity_is_shared(needs_attention, login, expected):
    plugin = SimpleNamespace(qq_client=SimpleNamespace(needs_attention=needs_attention),
        runtime_service=SimpleNamespace(fetch_login_status_payload=AsyncMock(return_value=login)))
    result = asyncio.run(account_identity(plugin))
    assert result["self_id"] == expected
    assert result["available"] == bool(expected)
    assert set(result) == {"schema_version", "provider", "platform", "account_scope", "available", "self_id"}
    if not needs_attention:
        plugin.runtime_service.fetch_login_status_payload.assert_not_awaited()


def test_missing_client_is_unknown():
    assert not asyncio.run(account_identity(SimpleNamespace()))["available"]
