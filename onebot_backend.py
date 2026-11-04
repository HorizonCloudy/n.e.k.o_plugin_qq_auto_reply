"""Explicit deployment backend selection; OneBot wire mode stays independent."""
from enum import StrEnum


class OneBotBackend(StrEnum):
    NAPCAT = "napcat"
    SNOWLUMA = "snowluma"


def selected_backend(settings) -> OneBotBackend:
    return OneBotBackend((settings or {}).get("onebot_backend") or "napcat")


class OneBotBackendService:
    """Route the historical NapCat service API to the selected implementation."""

    def __init__(self, *, get_settings, **kwargs):
        from .napcat_service import QQNapcatService
        from .snowluma_service import QQSnowLumaService

        self.get_settings = get_settings
        self.services = {
            OneBotBackend.NAPCAT: QQNapcatService(get_settings=get_settings, **kwargs),
            OneBotBackend.SNOWLUMA: QQSnowLumaService(get_settings=get_settings, **kwargs),
        }

    def __getattr__(self, name):
        return getattr(self.services[selected_backend(self.get_settings())], name)

    async def stop_managed_napcat(self):
        # Also clean up a previously selected backend on shutdown.
        for service in self.services.values():
            await service.stop_managed_napcat()
