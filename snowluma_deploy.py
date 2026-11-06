"""SnowLuma deployment and login handling, separate from NapCat's file format."""
import asyncio
import secrets
from urllib.parse import urlparse

from . import napcat_onebot_config, snowluma_install, snowluma_onebot_config
from .deploy_service import QQDeployService


class QQSnowLumaDeployService(QQDeployService):
    async def _ensure_token(self, emit=None):
        token = str(self._settings().get("token") or "").strip()
        if not token:
            token = secrets.token_urlsafe(24)
            self._settings()["token"] = token
            if await self.plugin.settings_service.persist_business_config() is False:
                self._settings()["token"] = ""
                raise RuntimeError("Token 保存失败，已停止部署，请检查存档目录权限")
        return token

    def _emit(self, emit, step, message, **extra):
        labels = {"locate": "定位 SnowLuma", "node": "检查运行环境", "fetch": "下载并解包 SnowLuma",
                  "config": "写入 OneBot 配置", "start": "启动 SnowLuma", "qrcode": "等待 QQ 登录"}
        super()._emit(emit, step, message, label=labels.get(step, step), backend="snowluma", **extra)

    async def deploy(self, *, uin="", force=False, auto_start=True, emit=None):
        uin = str(uin or "").strip()
        if not uin.isascii() or not uin.isdigit() or uin.startswith("0"):
            raise RuntimeError("请先填写有效的机器人 QQ 号，并使用该账号登录本机 QQ")
        svc = self.plugin.napcat_service
        root = svc.get_napcat_directory()
        self._progress_last.clear()
        self._emit(emit, "locate", f"检查 SnowLuma: {root}")
        ok, detail = await asyncio.to_thread(snowluma_install.check_installation, root)
        if force or not ok:
            self._emit(emit, "fetch", f"{detail}；获取 {snowluma_install.VERSION} 完整包（内置 Node）")
            archive = await snowluma_install.download(
                self.plugin.data_path("downloads"),
                lambda done, total: self._progress(emit, "fetch", done, total),
                lambda message: self._emit(emit, "fetch", message),
            )
            await svc.stop_managed_napcat()
            await asyncio.to_thread(snowluma_install.install, archive, root)
            self._emit(emit, "fetch", "SnowLuma 安装完成，已有配置及数据保留")
        settings = self._settings()
        settings["snowluma_directory"] = str(root)
        mode = settings.get("qq_connection_mode") or "napcat"
        if not settings.get("onebot_url"):
            settings["onebot_url"] = self.plugin.settings_service._default_onebot_url(mode)
        if await self.plugin.settings_service.persist_business_config() is False:
            raise RuntimeError("连接配置保存失败，已停止部署，请检查存档目录权限")
        await self._write_config(root, uin, mode, None, emit)
        self.plugin._deploy_prewrote_onebot_uin = uin
        auto = {"ok": False, "status": "", "error": ""}
        if auto_start:
            auto = await self.plugin._restart_auto_reply_runtime(True)
        await svc.ensure_napcat_started()
        if svc.get_startup_error():
            raise RuntimeError(svc.get_startup_error())
        login_uin = await self._online_login_uin()
        self._emit(emit, "start", "SnowLuma 已启动；请打开本机 QQ 并登录填写的账号，页面将自动确认连接")
        return {"ok": True, "backend": "snowluma", "snowluma_directory": str(root), "uin": uin,
                "qrcode_ready": False, "logged_in": bool(login_uin), "login_uin": login_uin,
                "webui_url": svc.get_webui_url(), **self.plugin._auto_start_fields(auto)}

    async def _write_config(self, root, uin, mode, step, emit):
        if not uin.isascii() or not uin.isdigit() or uin.startswith("0"):
            raise RuntimeError("请先填写有效的机器人 QQ 号")
        token = await self._ensure_token(emit)
        url = str(self._settings().get("onebot_url") or "")
        if mode == "napcat_forward":
            host, port = napcat_onebot_config.host_port_of(url)
            reverse = napcat_onebot_config.DEFAULT_REVERSE_DIAL
            path = urlparse(url).path or "/"
            if path.rstrip("/") in ("/api", "/event"):
                raise RuntimeError("SnowLuma 正向连接请使用根路径 /，/api 与 /event 无法同时收发消息")
        else:
            host, port, path = "127.0.0.1", napcat_onebot_config.DEFAULT_FORWARD_PORT, "/"
            reverse = napcat_onebot_config.dial_url(url)
        result = snowluma_onebot_config.apply(root, uin, reverse_url=reverse, forward_host=host,
                                             forward_port=port, forward_path=path, token=token,
                                             mode=mode or "napcat")
        self._emit(emit, "config", f"已写入 SnowLuma 账号配置: {result.name}")
        return result

    async def poll_login(self, *, auto_start=True, emit=None):
        uin = await self._online_login_uin()
        if not uin:
            return {"status": "pending", "backend": "snowluma"}
        return {"status": "completed", "uin": uin, "backend": "snowluma"}

    async def apply_onebot_config(self, *, uin="", restart=False, emit=None):
        uin = str(uin or await self._online_login_uin()).strip()
        svc = self.plugin.napcat_service
        path = await self._write_config(svc.get_napcat_directory(), uin,
                                        self._settings().get("qq_connection_mode"), None, emit)
        if restart:
            await svc.stop_managed_napcat()
            await svc.ensure_napcat_started()
        return {"ok": True, "uin": uin, "config_path": str(path), "restarted": bool(restart), "backend": "snowluma"}
