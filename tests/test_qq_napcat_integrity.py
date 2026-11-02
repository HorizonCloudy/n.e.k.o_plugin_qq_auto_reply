import asyncio
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply import napcat_install
from plugin.plugins.qq_auto_reply import napcat_integrity as integrity
from plugin.plugins.qq_auto_reply.deploy_service import QQDeployService
from plugin.plugins.qq_auto_reply.napcat_service import QQNapcatService


@pytest.fixture
def installed(tmp_path):
    archive = tmp_path / "shell.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name in (
            "launcher-user.bat", "launcher.bat", "napcat.mjs", "package.json",
            "loadNapCat.js", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll",
            "native/example.node", "node_modules/example/index.js", "static/index.html",
            "config/webui.json", "qqnt.json",
        ):
            z.writestr(name, "original")
    return napcat_install.extract_zip(archive, tmp_path / "installed")


def test_fresh_install_and_mutable_user_data(installed):
    assert integrity.check_installation(installed)[0]
    for name in ("config/webui.json", "launcher-user.bat", "qqnt.json"):
        (installed / name).write_text("user changes")
    assert integrity.check_installation(installed)[0]


def test_generated_loader_is_excluded_from_new_manifest(installed):
    files = json.loads((installed / integrity.MANIFEST).read_text(encoding="utf-8"))["files"]
    assert "loadNapCat.js" not in files
    assert {"napcat.mjs", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll"} <= files.keys()


@pytest.mark.parametrize("manifest_kind", ["new", "old", "absent"])
@pytest.mark.parametrize("state", ["rewritten", "missing", "empty"])
def test_runtime_loader_does_not_block_installation(installed, monkeypatch, manifest_kind, state):
    monkeypatch.setattr(integrity.napcat_platform, "is_windows", lambda: True)
    manifest = installed / integrity.MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    checked_files = len(data["files"])
    loader = installed / "loadNapCat.js"
    if manifest_kind == "old":
        data["files"]["loadNapCat.js"] = {"size": loader.stat().st_size, "sha256": integrity.digest(loader)}
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif manifest_kind == "absent":
        manifest.unlink()
    if state == "missing":
        loader.unlink()
    else:
        loader.write_text("" if state == "empty" else 'import("file:///new/location/napcat.mjs")')
    ok, detail = integrity.check_installation(installed)
    assert ok, detail
    if manifest_kind != "absent":
        assert f"{checked_files} 个程序文件" in detail


@pytest.mark.parametrize("name", ["napcat.mjs", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll"])
@pytest.mark.parametrize("state", ["missing", "modified", "empty"])
def test_core_program_files_are_not_exempt(installed, monkeypatch, name, state):
    monkeypatch.setattr(integrity.napcat_platform, "is_windows", lambda: True)
    if state == "missing":
        (installed / name).unlink()
    else:
        (installed / name).write_text("" if state == "empty" else "modified")
    ok, detail = integrity.check_installation(installed)
    assert not ok and name in detail


def test_loader_exemption_only_applies_to_installation_root(installed):
    nested = installed / "native/loadNapCat.js"
    nested.write_text("original")
    manifest = installed / integrity.MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"]["native/loadNapCat.js"] = {"size": nested.stat().st_size, "sha256": integrity.digest(nested)}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    nested.unlink()
    assert not integrity.check_installation(installed)[0]


@pytest.mark.parametrize("name", ["native/example.node", "node_modules/example/index.js", "static/index.html"])
@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_missing_and_same_size_corruption(installed, name, damage):
    if damage == "missing":
        (installed / name).unlink()
    else:
        (installed / name).write_text("modified")
    ok, detail = integrity.check_installation(installed)
    assert not ok
    assert name in detail


def test_legacy_install_reports_limited_check(installed):
    (installed / integrity.MANIFEST).unlink()
    ok, detail = integrity.check_installation(installed)
    assert ok and "未完成哈希校验" in detail
    (installed / "napcat.mjs").write_text("")
    ok, detail = integrity.check_installation(installed)
    assert not ok and "napcat.mjs" in detail


@pytest.mark.parametrize("content", ["broken json", json.dumps({"schema": 1, "files": {"../outside": {}}})])
def test_invalid_manifest_is_not_treated_as_legacy(installed, content):
    (installed / integrity.MANIFEST).write_text(content)
    assert not integrity.check_installation(installed)[0]


def test_corrupt_install_cannot_start(installed, monkeypatch):
    monkeypatch.setattr(integrity.napcat_platform, "is_windows", lambda: True)
    (installed / "napcat.mjs").unlink()
    service = QQNapcatService(get_settings=lambda: {"napcat_directory": str(installed)})
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    asyncio.run(service.ensure_napcat_started())
    spawn.assert_not_called()
    assert service.has_hard_startup_error()
    assert "napcat.mjs" in service.get_startup_error()


def test_deploy_failed_repair_keeps_settings_and_existing_install(installed, monkeypatch):
    (installed / "static/index.html").write_text("modified")
    settings = {"napcat_directory": str(installed)}
    service = QQNapcatService(get_settings=lambda: settings)
    save = AsyncMock()
    plugin = SimpleNamespace(
        napcat_service=service, _qq_settings=settings,
        settings_service=SimpleNamespace(save_settings=save),
        _emit_log=lambda *args: None,
    )
    deploy = QQDeployService(plugin)
    fetch = AsyncMock(side_effect=RuntimeError("download failed"))
    monkeypatch.setattr(deploy, "_fetch", fetch)
    with pytest.raises(RuntimeError, match="download failed"):
        asyncio.run(deploy.deploy(uin="12345678"))
    assert fetch.call_args.kwargs["repair_target"] == installed
    save.assert_not_called()
    assert (installed / "config/webui.json").read_text() == "original"
    assert (installed / "static/index.html").read_text() == "modified"


def test_repair_rejects_incomplete_package_without_replacing_old_install(installed):
    archive = installed.parent / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("napcat.mjs", "updated")
    with pytest.raises(RuntimeError, match="自动修复包完整性检查未通过"):
        napcat_install.extract_zip(archive, installed, validate_installation=True)
    assert (installed / "napcat.mjs").read_text() == "original"
    assert (installed / "config/webui.json").read_text() == "original"
    assert not list(installed.parent.glob("*.staging-*"))


@pytest.mark.parametrize("damage", ["program", "launchers"])
def test_deploy_repairs_custom_path_before_config_and_start(installed, monkeypatch, damage):
    (installed / "static/index.html").write_text("damaged")
    if damage == "launchers":
        for name in ("launcher.bat", "launcher-user.bat", "napcat.mjs"):
            (installed / name).unlink()
    settings = {"napcat_directory": str(installed)}
    service = QQNapcatService(get_settings=lambda: settings)
    stop = AsyncMock()
    monkeypatch.setattr(service, "stop_managed_napcat", stop)
    plugin = SimpleNamespace(
        napcat_service=service, _qq_settings=settings,
        _emit_log=lambda *args: None, data_path=lambda name: installed.parent / name,
    )
    deploy = QQDeployService(plugin)
    monkeypatch.setattr(deploy, "_check_node", lambda step: None)
    monkeypatch.setattr(napcat_install, "download_asset", AsyncMock(return_value=installed.parent / "shell.zip"))

    async def check_repaired(*args):
        assert integrity.check_installation(installed)[0]
        assert (installed / "static/index.html").read_text() == "original"
        assert (installed / "config/webui.json").read_text() == "original"
        stop.assert_awaited_once()
        raise RuntimeError("reached configuration after repair")

    monkeypatch.setattr(deploy, "_persist_connection_settings", check_repaired)
    with pytest.raises(RuntimeError, match="reached configuration after repair"):
        asyncio.run(deploy.deploy(uin="12345678"))


def test_failed_directory_promotion_restores_previous_install(installed, monkeypatch):
    archive = installed.parent / "update.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("napcat.mjs", "updated")
    real_replace = napcat_install.os.replace

    def fail_promotion(src, dst):
        if ".staging-" in str(src):
            raise OSError("simulated directory promotion failure")
        return real_replace(src, dst)

    monkeypatch.setattr(napcat_install.os, "replace", fail_promotion)
    with pytest.raises(OSError, match="promotion failure"):
        napcat_install.extract_zip(archive, installed)
    assert integrity.check_installation(installed)[0]
    assert (installed / "config/webui.json").read_text() == "original"
    assert not list(installed.parent.glob("*.old-*"))


def test_reinstall_preserves_user_data_and_refreshes_program_baseline(installed):
    (installed / "config/webui.json").write_text("user settings")
    (installed / "launcher-user.bat").write_text("user arguments")
    (installed / "cache").mkdir()
    (installed / "cache/session").write_text("session cache")
    (installed / "static/index.html").write_text("damaged")
    napcat_install.extract_zip(installed.parent / "shell.zip", installed)
    assert (installed / "config/webui.json").read_text() == "user settings"
    assert (installed / "launcher-user.bat").read_text() == "user arguments"
    assert (installed / "cache/session").read_text() == "session cache"
    assert (installed / "static/index.html").read_text() == "original"
    assert integrity.check_installation(installed)[0]
