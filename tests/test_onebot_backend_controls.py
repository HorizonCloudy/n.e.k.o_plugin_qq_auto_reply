from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply.runtime_ops_service import QQRuntimeOpsService


def setup(backend="napcat", alive=False, connected=False):
    process = SimpleNamespace(returncode=None if alive else 0, pid=42)
    service = SimpleNamespace(
        napcat_process=process, manages_napcat_process=alive,
        clear_startup_error=lambda: None, get_startup_error=lambda: "",
    )

    async def start():
        process.returncode = None
        service.manages_napcat_process = True

    async def stop():
        process.returncode = 0

    service.ensure_napcat_started = AsyncMock(side_effect=start)
    service.stop_managed_napcat = AsyncMock(side_effect=stop)
    other = SimpleNamespace(stop_managed_napcat=AsyncMock())
    plugin = SimpleNamespace(
        _qq_settings={"onebot_backend": backend, "qq_connection_mode": "napcat"},
        qq_client=SimpleNamespace(is_connected=lambda: connected),
        napcat_service=SimpleNamespace(services={backend: service, "other": other}),
    )
    ops = QQRuntimeOpsService(plugin)
    ops._stop_runtime_locked = AsyncMock()
    return plugin, service, other, ops


@pytest.mark.parametrize("backend", ["napcat", "snowluma"])
async def test_start_and_stop_selected_backend(backend):
    plugin, service, other, ops = setup(backend)
    assert (await ops.control_backend(start=True, backend=backend)).is_ok()
    service.ensure_napcat_started.assert_awaited_once()
    ops._stop_runtime_locked.assert_not_awaited()
    assert (await ops.control_backend(start=False, backend=backend)).is_ok()
    ops._stop_runtime_locked.assert_awaited_once_with(stop_napcat=False)
    service.stop_managed_napcat.assert_awaited_once()
    other.stop_managed_napcat.assert_not_awaited()


@pytest.mark.parametrize("condition", ["open_platform", "deploy", "selection", "external"])
async def test_reject_unsafe_stop(condition):
    plugin, service, _, ops = setup(alive=True)
    backend = "napcat"
    if condition == "open_platform":
        plugin._qq_settings["qq_connection_mode"] = "open_platform"
    elif condition == "deploy":
        plugin._onebot_deploy_in_progress = True
    elif condition == "selection":
        backend = "snowluma"
    else:
        service.manages_napcat_process = False
    assert (await ops.control_backend(start=False, backend=backend)).is_err()
    ops._stop_runtime_locked.assert_not_awaited()
    service.stop_managed_napcat.assert_not_awaited()


async def test_connected_external_backend_is_not_launched_again():
    _, service, _, ops = setup(connected=True)
    assert (await ops.control_backend(start=True, backend="napcat")).is_ok()
    service.ensure_napcat_started.assert_not_awaited()


async def test_start_failure_is_not_reported_as_success():
    _, service, _, ops = setup()
    service.ensure_napcat_started.side_effect = None
    assert (await ops.control_backend(start=True, backend="napcat")).is_err()


async def test_stop_cancels_pipeline_before_terminating_backend():
    _, service, _, ops = setup(alive=True)
    order = []

    async def pipeline(**kwargs):
        order.append("pipeline")

    async def process():
        order.append("process")
        service.napcat_process.returncode = 0

    ops._stop_runtime_locked.side_effect = pipeline
    service.stop_managed_napcat.side_effect = process
    assert (await ops.control_backend(start=False, backend="napcat")).is_ok()
    assert order == ["pipeline", "process"]
