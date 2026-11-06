import io
import json
import tarfile
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import snowluma_install as installer
from plugin.plugins.qq_auto_reply.deploy_service import QQDeployService
from plugin.plugins.qq_auto_reply.onebot_backend import OneBotBackendService
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService
from plugin.plugins.qq_auto_reply.snowluma_deploy import QQSnowLumaDeployService
from plugin.plugins.qq_auto_reply.snowluma_onebot_config import apply, read_object
from plugin.plugins.qq_auto_reply.snowluma_service import QQSnowLumaService


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_config_merges_and_reuses_endpoints_without_losing_other_consumers(tmp_path):
    path = tmp_path / "config/onebot_123.json"
    other = {"name": "other", "url": "ws://localhost:7000", "accessToken": "other-secret"}
    write_json(path, {"custom": 42, "networks": {
        "wsClients": [other], "httpServers": [{"name": "http"}],
        "wsServers": [{"name": "existing", "host": "0.0.0.0", "port": 3001}],
    }})
    write_json(tmp_path / "config/runtime.json", {"webuiPort": 6000, "custom": True})
    for token in ("first", "second"):
        apply(tmp_path, "123", reverse_url="ws://127.0.0.1:6199", forward_host="127.0.0.1", forward_port=3001,
              token=token, mode="napcat_forward")
    config = read_object(path)
    assert config["custom"] == 42
    assert config["networks"]["wsClients"][0] == other
    assert len(config["networks"]["wsClients"]) == 1
    assert len(config["networks"]["wsServers"]) == 1
    row = config["networks"]["wsServers"][0]
    assert row["name"] == "neko-qq-wsServers" and row["accessToken"] == "second"
    assert row["role"] == "Universal" and row["enabled"] is True
    assert config["networks"]["httpServers"] == [{"name": "http"}]
    assert read_object(tmp_path / "config/runtime.json") == {"webuiPort": 6000, "custom": True, "hookAutoLoad": True, "webuiHost": "127.0.0.1"}
    assert not (tmp_path / "config/onebot11_123.json").exists()


def test_stock_cleanup_across_accounts_and_direction_switch(tmp_path):
    stock = {"name": "ws-default", "host": "127.0.0.1", "port": 3001}
    custom = {"name": "custom", "host": "127.0.0.1", "port": 3010}
    changed_default = {"name": "ws-default", "host": "127.0.0.1", "port": 3011}
    for uin in ("123", "456"):
        write_json(tmp_path / f"config/onebot_{uin}.json", {"custom": True, "networks": {
            "wsServers": [stock, custom, changed_default],
            "httpServers": [{"name": "http-default", "host": "127.0.0.1", "port": 3000}],
        }})
    kwargs = dict(reverse_url="ws://127.0.0.1:6199/ws", forward_host="127.0.0.1", forward_port=3001, token="x")
    apply(tmp_path, "123", **kwargs)
    for uin in ("123", "456"):
        d = read_object(tmp_path / f"config/onebot_{uin}.json")
        assert d["custom"] is True
        assert d["networks"]["wsServers"] == [custom, changed_default]
        assert d["networks"]["httpServers"] == []
        assert list((tmp_path / "config").glob(f"onebot_{uin}.json.neko-backup-*"))
    assert read_object(tmp_path / "config/onebot.json")["networks"]["wsServers"] == []
    apply(tmp_path, "123", mode="napcat_forward", **kwargs)
    networks = read_object(tmp_path / "config/onebot_123.json")["networks"]
    assert networks["wsClients"][0]["enabled"] is False
    assert networks["wsServers"][-1]["port"] == 3001
    assert networks["wsServers"][-1]["enabled"] is True
    apply(tmp_path, "123", **kwargs)
    networks = read_object(tmp_path / "config/onebot_123.json")["networks"]
    assert networks["wsClients"][0]["enabled"] is True
    assert networks["wsServers"][-1]["enabled"] is False
    backups = list((tmp_path / "config").glob("*.neko-backup-*"))
    apply(tmp_path, "123", **kwargs)
    assert list((tmp_path / "config").glob("*.neko-backup-*")) == backups


def test_invalid_json_is_not_overwritten(tmp_path):
    path = tmp_path / "config/onebot_123.json"
    path.parent.mkdir()
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        apply(tmp_path, "123", reverse_url="ws://localhost:6199", forward_host="127.0.0.1", forward_port=3001, token="x")
    assert path.read_text() == "broken"


@pytest.mark.asyncio
async def test_facade_routes_paths_and_stops_both_owned_backends(tmp_path):
    settings = {}
    service = OneBotBackendService(get_settings=lambda: settings, data_dir=tmp_path)
    assert service.default_napcat_directory() == tmp_path / "NapCat.Shell"
    settings["onebot_backend"] = "snowluma"
    assert service.get_napcat_directory() == tmp_path / "SnowLuma"
    assert isinstance(service.services["snowluma"], QQSnowLumaService)
    for backend in service.services.values():
        backend.stop_managed_napcat = AsyncMock()
    await service.stop_managed_napcat()
    for backend in service.services.values():
        backend.stop_managed_napcat.assert_awaited_once()
    assert await service.sync_napcat_qrcode_into_static() is False


@pytest.mark.asyncio
async def test_invalid_backend_rejected_before_stopping_or_mutating():
    plugin = SimpleNamespace(_qq_settings={}, _stop_auto_reply_runtime=AsyncMock())
    with pytest.raises(ValueError):
        await QQSettingsService(plugin).save_settings(onebot_backend="invalid")
    assert plugin._qq_settings == {}
    plugin._stop_auto_reply_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_stop_cannot_switch_backend():
    plugin = SimpleNamespace(_qq_settings={"onebot_backend": "napcat"},
                             _stop_auto_reply_runtime=AsyncMock(side_effect=RuntimeError("cannot stop")))
    with pytest.raises(RuntimeError, match="cannot stop"):
        await QQSettingsService(plugin).save_settings(onebot_backend="snowluma")
    assert plugin._qq_settings["onebot_backend"] == "napcat"


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [True, False])
async def test_save_backend_keeps_independent_paths_and_rolls_back_failed_save(persisted):
    settings = {"onebot_backend": "napcat", "napcat_directory": "C:/nc", "snowluma_directory": "C:/sl"}

    async def stop(**kwargs):
        assert settings["onebot_backend"] == "napcat"

    plugin = SimpleNamespace(_qq_settings=settings, _stop_auto_reply_runtime=AsyncMock(side_effect=stop),
                             _emit_log=Mock(), _ensure_qq_client_initialized=Mock(),
                             attention_service=None, qq_client=None, _running=False,
                             napcat_service=SimpleNamespace(clear_startup_error=Mock()))
    service = QQSettingsService(plugin)
    service._persist_with_consent_rollback = AsyncMock(return_value=persisted)
    result = await service.save_settings(onebot_backend="snowluma", snowluma_directory="C:/new-sl")
    assert result["persisted"] is persisted
    assert settings["napcat_directory"] == "C:/nc"
    assert settings["onebot_backend"] == ("snowluma" if persisted else "napcat")
    assert settings["snowluma_directory"] == ("C:/new-sl" if persisted else "C:/sl")
    plugin._stop_auto_reply_runtime.assert_awaited_once_with(stop_napcat=True)


@pytest.mark.asyncio
async def test_cannot_switch_backend_while_deployment_is_active():
    plugin = SimpleNamespace(_qq_settings={"onebot_backend": "napcat"}, _onebot_deploy_in_progress=True,
                             _stop_auto_reply_runtime=AsyncMock())
    with pytest.raises(RuntimeError, match="部署正在进行"):
        await QQSettingsService(plugin).save_settings(onebot_backend="snowluma")
    plugin._stop_auto_reply_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_dispatch_and_open_platform_rejection(monkeypatch):
    deploy = AsyncMock(return_value={"backend": "snowluma"})
    monkeypatch.setattr(QQSnowLumaDeployService, "deploy", deploy)
    plugin = SimpleNamespace(_qq_settings={"onebot_backend": "snowluma"})
    assert (await QQDeployService(plugin).deploy(uin="123"))["backend"] == "snowluma"
    deploy.assert_awaited_once_with(uin="123", force=False, auto_start=True, emit=None)
    plugin._qq_settings["qq_connection_mode"] = "open_platform"
    with pytest.raises(RuntimeError, match="开放平台"):
        await QQDeployService(plugin).deploy(uin="123")
    assert deploy.await_count == 1


@pytest.mark.asyncio
async def test_snowluma_deploy_config_before_listener_before_process(tmp_path, monkeypatch):
    events = []
    settings = {"onebot_backend": "snowluma", "token": "token", "onebot_url": "ws://0.0.0.0:6199"}
    service = QQSnowLumaService(get_settings=lambda: settings, data_dir=tmp_path)
    monkeypatch.setattr(installer, "check_installation", lambda root: (True, "ok"))

    async def start_runtime(_):
        assert (tmp_path / "SnowLuma/config/onebot_123.json").is_file()
        events.append("runtime")
        return {"ok": True}

    async def start_process():
        events.append("process")

    service.ensure_napcat_started = start_process
    plugin = SimpleNamespace(_qq_settings=settings, napcat_service=service, _emit_log=Mock(),
                             settings_service=SimpleNamespace(persist_business_config=AsyncMock()),
                             _restart_auto_reply_runtime=start_runtime, _auto_start_fields=lambda a: {},
                             runtime_service=SimpleNamespace(fetch_login_status_payload=AsyncMock(return_value={"status": "offline"})))
    async def mutate_settings(mutation):
        mutation(settings)
        return True

    plugin.settings_service.mutate_business_config = mutate_settings
    result = await QQDeployService(plugin).deploy(uin="123")
    assert settings["backend_deployments"]["snowluma"] == str((tmp_path / "SnowLuma").resolve())
    assert events == ["runtime", "process"]
    assert result["qrcode_ready"] is False and result["logged_in"] is False
    assert "napcat_directory" not in settings
    assert (await QQDeployService(plugin).poll_login())["status"] == "pending"
    plugin.runtime_service.fetch_login_status_payload.return_value = {"status": "online", "self_id": "123"}
    assert (await QQDeployService(plugin).poll_login())["status"] == "completed"


def fake_archive(path, *, linux=False, malicious=False):
    files = {"index.mjs": b"code", "check-node-version.cjs": b"check",
             "node" if linux else "node.exe": b"node",
             f"native/snowluma-{'linux-x64' if linux else 'win32-x64'}.node": b"native",
             f"native/snowluma-{'linux-x64.so' if linux else 'win32-x64.dll'}": b"hook",
             "config/runtime.json": b"must not overwrite existing config"}
    if malicious:
        files["../escaped.txt"] = b"bad"
    if linux:
        with tarfile.open(path, "w:gz") as bundle:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            bundle.addfile(root)
            for name, data in files.items():
                entry = tarfile.TarInfo("./" + name)
                entry.size, entry.mode = len(data), 0o755
                bundle.addfile(entry, io.BytesIO(data))
    else:
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("native/", b"")
            for name, data in files.items():
                bundle.writestr(name, data)


@pytest.mark.parametrize("linux", [False, True])
def test_install_repair_preserves_config_and_detects_modified_program(tmp_path, monkeypatch, linux):
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-x64" if linux else "win-x64")
    archive, target = tmp_path / "package", tmp_path / "installed"
    fake_archive(archive, linux=linux)
    write_json(target / "config/runtime.json", {"webuiPort": 5432})
    installer.install(archive, target)
    assert installer.check_installation(target)[0]
    (target / "index.mjs").write_text("corrupt")
    assert not installer.check_installation(target)[0]
    installer.install(archive, target)
    assert installer.check_installation(target)[0]
    assert read_object(target / "config/runtime.json") == {"webuiPort": 5432}


@pytest.mark.parametrize("linux", [False, True])
def test_archive_traversal_rejected_without_touching_install(tmp_path, monkeypatch, linux):
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-x64" if linux else "win-x64")
    archive, target = tmp_path / "package", tmp_path / "installed"
    fake_archive(archive, linux=linux, malicious=True)
    with pytest.raises(RuntimeError):
        installer.install(archive, target)
    assert not target.exists()
    assert not (tmp_path / "escaped.txt").exists()
