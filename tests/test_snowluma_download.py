import asyncio
import hashlib

import httpx
import pytest
from plugin.plugins.qq_auto_reply import snowluma_install as installer


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["slow_official", "corrupt_mirror", "all_bad", "timeout", "cancel"])
async def test_verified_race_and_cleanup(tmp_path, monkeypatch, scenario):
    payload = b"verified release"
    monkeypatch.setattr(installer, "platform_key", lambda: "win-x64")
    monkeypatch.setattr(installer, "ASSETS", {
        "win-x64": ("zip", len(payload), hashlib.sha256(payload).hexdigest()),
    })
    monkeypatch.setattr(installer, "MIRROR_DELAY", 0)
    monkeypatch.setattr(installer, "DOWNLOAD_TIMEOUT", 0.05 if scenario == "timeout" else 2)
    started = asyncio.Event()
    mirror_done = asyncio.Event()
    closed = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, mirror):
            self.mirror = mirror

        async def __aiter__(self):
            started.set()
            if scenario in ("timeout", "cancel") or (scenario == "slow_official" and not self.mirror):
                await asyncio.Event().wait()
            if scenario == "corrupt_mirror" and not self.mirror:
                await mirror_done.wait()
            bad = scenario == "all_bad" or (scenario == "corrupt_mirror" and self.mirror)
            yield b"x" * len(payload) if bad else payload
            if self.mirror:
                mirror_done.set()

        async def aclose(self):
            closed.append(self.mirror)

    def handler(request):
        return httpx.Response(200, stream=Stream(request.url.host == "gh-proxy.com"))

    client_class = httpx.AsyncClient
    monkeypatch.setattr(installer.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(handler), **kw,
    ))
    progress = []
    task = asyncio.create_task(installer.download(tmp_path, lambda done, total: progress.append(done), lambda _: None))
    if scenario == "cancel":
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif scenario in ("all_bad", "timeout"):
        with pytest.raises(RuntimeError, match="下载失败"):
            await task
    else:
        result = await task
        assert result.read_bytes() == payload
        assert progress == sorted(progress)
        # Verified cache avoids even creating another HTTP client.
        monkeypatch.setattr(installer.httpx, "AsyncClient", lambda **kw: pytest.fail("cache missed"))
        assert await installer.download(tmp_path, lambda *a: None, lambda _: None) == result
    assert not list(tmp_path.glob("*.part"))
    assert closed
    if scenario in ("all_bad", "timeout", "cancel"):
        assert not list(tmp_path.glob("*.zip"))
