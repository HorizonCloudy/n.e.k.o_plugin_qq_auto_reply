"""SnowLuma process adapter. Login belongs to the native QQ / SnowLuma UI."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import snowluma_install
from .napcat_service import QQNapcatService
from .snowluma_onebot_config import read_object


class QQSnowLumaService(QQNapcatService):
    # Keep the internal transport API shared; no NapCat config/QR/startup is used.
    TRANSIENT_TIMEOUT_ERROR = "SnowLuma 已启动，等待 QQ 登录并连接反向 WS"
    FORWARD_TRANSIENT_TIMEOUT_ERROR = "SnowLuma 已启动，等待 QQ 登录并连接正向 WS"

    def default_napcat_directory(self):
        return (self._data_dir or self._config_dir) / "SnowLuma"

    def get_configured_napcat_path(self):
        return str(self._get_settings().get("snowluma_directory") or "").strip()

    def get_napcat_directory(self):
        raw = self.get_configured_napcat_path()
        return Path(raw) if raw else self.default_napcat_directory()

    async def migrate_napcat_storage(self):
        return  # SnowLuma never lived in the plugin's NapCat.Shell folder.

    def get_napcat_launch_target(self):
        return self.get_napcat_directory() / "index.mjs"

    def find_napcat_launcher(self):
        path = self.get_napcat_launch_target()
        return path if path.is_file() else None

    def get_webui_url(self):
        root = self.get_napcat_directory()
        if not (root / "index.mjs").is_file():
            return ""
        try:
            runtime = read_object(root / "config" / "runtime.json")
            port = int(runtime.get("webuiPort") or 5099)
            host = runtime.get("webuiHost") or "127.0.0.1"
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1"
            scheme = "https" if runtime.get("webuiTls", {}).get("enabled") else "http"
            return f"{scheme}://{host}:{port}/"
        except (ValueError, OSError):
            return ""

    async def get_webui_login_url(self):
        # Credentials are resolved only on an explicit console-open request,
        # never included in periodic dashboard status or log exports.
        url = self.get_webui_url()
        if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return url
        password = await asyncio.to_thread(self._initial_webui_password)
        return url + "?" + urlencode({"token": password}) if password else url

    def _initial_webui_password(self):
        root = self.get_napcat_directory()
        try:
            auth = read_object(root / "config" / "webui.json")
            if auth.get("mustChangePassword") is not True:
                return ""  # Never reuse an initial password after the user changed it.
            salt = bytes.fromhex(auth["passwordSalt"])
            expected = bytes.fromhex(auth["passwordHash"])
            if len(salt) != 16 or len(expected) != 64:
                return ""
            # Startup logs append across restarts; use the newest credential only.
            with (root / "logs" / "neko-launch.log").open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 1024 * 1024))
                text = log.read().decode("utf-8", errors="replace")
            text = re.sub(r"\x1b\[[0-9;]*m", "", text)
            matches = re.findall(r"initial credentials: user=admin password=([0-9a-f]{16})(?=\s|$)", text)
            if not matches:
                return ""
            password = matches[-1]
            actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
            return password if hmac.compare_digest(actual, expected) else ""
        except (OSError, ValueError, KeyError, TypeError):
            return ""

    async def sync_napcat_qrcode_into_static(self):
        return False  # Never show a stale NapCat QR as a SnowLuma login QR.

    def get_napcat_qrcode_path(self):
        return self.get_napcat_directory() / "unused-login-qr.png"

    async def _read_napcat_webui_lines(self):
        return []  # Do not expose SnowLuma console logs containing initial passwords.

    async def ensure_napcat_started(self):
        if self.napcat_process and self.napcat_process.returncode is None:
            return
        # A manually managed/remote backend can already be connected.
        client = self._get_qq_client()
        if client and client.is_connected():
            self.clear_startup_error()
            return
        root = self.get_napcat_directory()
        if not (root / "index.mjs").is_file() and not self.get_configured_napcat_path():
            return
        ok, detail = await asyncio.to_thread(snowluma_install.check_installation, root)
        if not ok:
            self.set_startup_error(f"SnowLuma {detail}，请执行一键部署自动修复")
            return
        try:
            node = root / ("node.exe" if os.name == "nt" else "node")
            options = {}
            if os.name == "nt":
                options["creationflags"] = (subprocess.CREATE_NEW_CONSOLE if self._get_settings().get("show_snowluma_window")
                                            else subprocess.CREATE_NO_WINDOW)
            else:
                options["start_new_session"] = True
            (root / "logs").mkdir(exist_ok=True)
            with (root / "logs" / "neko-launch.log").open("ab") as output:
                self._napcat_process = await asyncio.create_subprocess_exec(
                    str(node.resolve()), "index.mjs", cwd=str(root),
                    stdout=output, stderr=output, **options,
                )
            self._manages_napcat_process = True
            self.clear_startup_error()
            await asyncio.sleep(0.5)
            if self._napcat_process.returncode is not None:
                self.set_startup_error("SnowLuma 启动后退出，请查看 SnowLuma/logs/neko-launch.log")
            else:
                self._emit_log("INFO", "SnowLuma 已启动，请在本机 QQ 登录；可在 SnowLuma WebUI 查看接入状态")
        except Exception as error:
            self.set_startup_error(f"启动 SnowLuma 失败: {error}")

    async def stop_managed_napcat(self):
        # SnowLuma injects existing QQ processes. Stop only our Node process,
        # never terminate the user's native QQ process tree.
        process = self._napcat_process
        self._napcat_process = None
        managed = self._manages_napcat_process
        self._manages_napcat_process = False
        if managed and process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
