import shutil

import pytest
from plugin.plugins.qq_auto_reply import napcat_service as ns


@pytest.mark.parametrize("configured", ["", "directory", "launcher"])
@pytest.mark.parametrize("windows", [True, False])
async def test_migrated_install_survives_payload_replacement(tmp_path, monkeypatch, configured, windows):
    monkeypatch.setattr(ns.napcat_platform, "is_windows", lambda: windows)
    launcher = "launcher.bat" if windows else "napcat.mjs"
    package = tmp_path / "package"
    old = package / "NapCat.Shell"
    old.mkdir(parents=True)
    (old / launcher).write_text("launcher")
    (old / "config").mkdir()
    (old / "config/account.json").write_text("account data")
    monkeypatch.setattr(ns, "bundled_napcat_dir", lambda: old)
    settings = {"napcat_directory": str(old / launcher) if configured == "launcher" else str(old) if configured else ""}
    svc = ns.QQNapcatService(get_settings=lambda: settings, config_dir=package, data_dir=tmp_path / "persistent")
    await svc.migrate_napcat_storage()
    target = tmp_path / "persistent/NapCat.Shell"
    assert (target / "config/account.json").read_text() == "account data"
    assert (old / "config/account.json").exists()
    # Simulate the host replacing the entire plugin payload on update.
    shutil.rmtree(package)
    assert svc.get_napcat_directory() == target
    assert svc.find_napcat_launcher() == target / launcher
    await svc.migrate_napcat_storage()
    assert (target / "config/account.json").read_text() == "account data"


async def test_existing_persistent_install_is_never_overwritten(tmp_path):
    package = tmp_path / "package"
    old = package / "NapCat.Shell"
    old.mkdir(parents=True)
    (old / "account").write_text("old")
    target = tmp_path / "persistent/NapCat.Shell"
    target.mkdir(parents=True)
    (target / "account").write_text("current")
    svc = ns.QQNapcatService(config_dir=package, data_dir=tmp_path / "persistent")
    await svc.migrate_napcat_storage()
    assert (target / "account").read_text() == "current"


async def test_external_custom_path_is_unchanged(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    svc = ns.QQNapcatService(get_settings=lambda: {"napcat_directory": str(external)},
                            config_dir=tmp_path / "package", data_dir=tmp_path / "persistent")
    await svc.migrate_napcat_storage()
    assert svc.get_napcat_directory() == external
    assert not svc.default_napcat_directory().exists()


async def test_failed_migration_preserves_source_and_does_not_switch(tmp_path, monkeypatch):
    old = tmp_path / "package/NapCat.Shell"
    old.mkdir(parents=True)
    (old / "account").write_text("saved")
    svc = ns.QQNapcatService(get_settings=lambda: {"napcat_directory": str(old)},
                            config_dir=old.parent, data_dir=tmp_path / "persistent")

    def fail(*args):
        raise OSError("copy failed")

    monkeypatch.setattr(ns.shutil, "copytree", fail)
    with pytest.raises(OSError, match="copy failed"):
        await svc.migrate_napcat_storage()
    assert (old / "account").read_text() == "saved"
    assert svc.get_napcat_directory() == old
    assert not svc.default_napcat_directory().exists()
