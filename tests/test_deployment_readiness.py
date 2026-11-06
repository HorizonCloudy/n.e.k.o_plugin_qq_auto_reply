"""Deployment readiness is not QR availability or QQ login success."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import napcat_integrity, napcat_platform, snowluma_install
from plugin.plugins.qq_auto_reply.deploy_service import QQDeployService
from plugin.plugins.qq_auto_reply.runtime_service import QQRuntimeService
from plugin.plugins.qq_auto_reply.snowluma_deploy import QQSnowLumaDeployService


def make_plugin(root, backend="napcat"):
    settings = {"onebot_backend": backend, "qq_connection_mode": "napcat"}
    saved = []

    async def mutate(mutation):
        mutation(settings)
        saved.append(deepcopy(settings))
        return True

    service = SimpleNamespace(
        get_napcat_directory=lambda: root,
        get_configured_napcat_path=lambda: str(root),
        get_startup_error=lambda: "",
        manages_napcat_process=True,
        napcat_process=None,
        migrate_napcat_storage=AsyncMock(),
        ensure_napcat_started=AsyncMock(),
        get_webui_url=lambda: "http://localhost/",
    )
    plugin = SimpleNamespace(
        _qq_settings=settings, napcat_service=service, qq_client=None,
        settings_service=SimpleNamespace(mutate_business_config=mutate,
                                         persist_business_config=AsyncMock(return_value=True)),
        _emit_log=Mock(), _auto_start_fields=lambda auto: {},
    )
    return plugin, saved


@pytest.mark.parametrize("backend", ["napcat", "snowluma"])
@pytest.mark.parametrize("failure", [False, True])
async def test_real_deploy_records_completion_only_after_success(tmp_path, monkeypatch, backend, failure):
    plugin, saved = make_plugin(tmp_path, backend)
    plugin._qq_settings["onebot_url"] = "ws://localhost:6199"
    monkeypatch.setattr(napcat_platform, "find_launcher", lambda root: root / "launcher")
    monkeypatch.setattr(napcat_integrity, "check_installation", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(snowluma_install, "check_installation", lambda root: (True, "ok"))
    deploy = QQDeployService(plugin) if backend == "napcat" else QQSnowLumaDeployService(plugin)
    deploy._write_config = AsyncMock()
    deploy._persist_connection_settings = AsyncMock()
    deploy._wait_qrcode = AsyncMock(return_value=False)
    deploy._online_login_uin = AsyncMock(return_value="")
    runtime = QQRuntimeService(plugin)

    async def start():
        plugin.napcat_service.napcat_process = SimpleNamespace(returncode=None)
        assert not runtime._deployment_complete(backend), "Process startup alone is not deployment completion"
        if failure:
            raise RuntimeError("start failed")

    plugin.napcat_service.ensure_napcat_started = start
    if failure:
        with pytest.raises(RuntimeError, match="start failed"):
            await deploy.deploy(uin="123", auto_start=False)
        assert not runtime._deployment_complete(backend)
        assert saved[-1]["backend_deployments"][backend] == ""
    else:
        result = await deploy.deploy(uin="123", auto_start=False)
        assert result["ok"] and not result["logged_in"] and not result["qrcode_ready"]
        assert saved[-1]["backend_deployments"][backend] == str(tmp_path.resolve())
        # Reload while stopped: remembered completion still exposes the login/setup card.
        plugin._qq_settings = deepcopy(saved[-1])
        plugin.napcat_service.napcat_process = None
        assert runtime._deployment_complete(backend)
        plugin._onebot_deploy_in_progress = True
        assert not runtime._deployment_complete(backend)


async def test_completion_is_scoped_to_backend_and_install_path(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    deploy = QQDeployService(plugin)
    await deploy._record_deployment("napcat", tmp_path)
    runtime = QQRuntimeService(plugin)
    assert runtime._deployment_complete("napcat")
    assert not runtime._deployment_complete("snowluma")
    other = tmp_path / "other"
    other.mkdir()
    plugin.napcat_service.get_napcat_directory = lambda: other
    assert not runtime._deployment_complete("napcat")
    await deploy._record_deployment("snowluma", other)
    assert runtime._deployment_complete("snowluma")
    assert plugin._qq_settings["backend_deployments"]["napcat"] == str(tmp_path.resolve())
    other.rmdir()
    assert not runtime._deployment_complete("snowluma")


async def test_failed_completion_save_does_not_publish_ready(tmp_path):
    plugin, _ = make_plugin(tmp_path)

    async def fail(mutation):
        mutation(plugin._qq_settings)
        return False

    plugin.settings_service.mutate_business_config = fail
    with pytest.raises(RuntimeError, match="部署状态保存失败"):
        await QQDeployService(plugin)._record_deployment("napcat", tmp_path)
    assert not QQRuntimeService(plugin)._deployment_complete("napcat")


def test_legacy_readiness_requires_healthy_running_backend(tmp_path):
    plugin, _ = make_plugin(tmp_path)
    runtime = QQRuntimeService(plugin)
    assert not runtime._deployment_complete("napcat")
    plugin.napcat_service.napcat_process = SimpleNamespace(returncode=None)
    assert runtime._deployment_complete("napcat")
    plugin.napcat_service.get_startup_error = lambda: "failed"
    assert not runtime._deployment_complete("napcat")
    plugin.napcat_service.get_startup_error = lambda: ""
    plugin._qq_settings["qq_connection_mode"] = "open_platform"
    assert not runtime._deployment_complete("napcat")
