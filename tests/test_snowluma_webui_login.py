import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from plugin.plugins.qq_auto_reply.snowluma_service import QQSnowLumaService


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "rotated", "stale", "missing", "remote", "malformed"])
async def test_initial_password_only_when_current_and_local(tmp_path, case):
    service = QQSnowLumaService(get_settings=lambda: {}, data_dir=tmp_path)
    root = service.get_napcat_directory()
    (root / "config").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "index.mjs").write_text("placeholder")
    password, salt = "0123456789abcdef", b"a" * 16
    auth = {"passwordSalt": salt.hex(), "passwordHash": hashlib.scrypt(
        password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64,
    ).hex(), "mustChangePassword": case != "rotated"}
    (root / "config/webui.json").write_text(json.dumps(auth) if case != "malformed" else "bad")
    (root / "config/runtime.json").write_text(json.dumps({
        "webuiHost": "example.com" if case == "remote" else "0.0.0.0", "webuiPort": 5099,
    }))
    if case != "missing":
        logged = "fedcba9876543210" if case == "stale" else password
        (root / "logs/neko-launch.log").write_text(
            "[WebUI] initial credentials: user=admin password=1111111111111111\n"
            f"\x1b[32m[WebUI] initial credentials: user=admin password={logged}\x1b[0m\n",
        )
    assert "token=" not in service.get_webui_url()
    url = await service.get_webui_login_url()
    assert parse_qs(urlparse(url).query) == ({"token": [password]} if case == "valid" else {})
    assert await service._read_napcat_webui_lines() == []
