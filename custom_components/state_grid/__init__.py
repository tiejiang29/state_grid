from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from .const import DOMAIN
from .utils.logger import LOGGER
from .utils.store import async_load_from_store
from .data_client import StateGridDataClient
from . import click_captcha_solver
from .config_flow import StateGridOnnxConfigFlow

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """当用户在 UI 里点击"添加集成"并完成配置时调用。"""
    config = await async_load_from_store(hass, "state_grid.config") or None
    data_client = StateGridDataClient(hass=hass, config=config)

    # 配置优先级：entry.options > entry.data > 存储中的 config
    # entry.options 是用户在"配置"按钮中修改的最新值
    merged = {**(entry.data or {}), **(entry.options or {})}

    if merged:
        llm_key = merged.get("llm_api_key", "")
        if llm_key:
            data_client.llm_api_key = llm_key
        if "llm_base_url" in merged:
            data_client.llm_base_url = merged["llm_base_url"]
        if "llm_model" in merged:
            data_client.llm_model = merged["llm_model"]
        if "email_account" in merged:
            data_client.email_account = merged["email_account"]
        if "refresh_interval" in merged:
            try:
                data_client.refresh_interval = max(12, int(merged["refresh_interval"]))
            except (ValueError, TypeError):
                pass

    # 确保至少有 LLM 配置（从 entry.data 或 config 中获取）
    if data_client.llm_api_key:
        click_captcha_solver.configure_llm(
            data_client.llm_api_key,
            data_client.llm_base_url,
            data_client.llm_model,
        )

    hass.data[DOMAIN] = data_client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成时调用。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options 更新时触发，重新加载集成使配置立即生效。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """ConfigEntry 版本迁移。

    HA 在打开 options 配置页时，如果 entry.version < config_flow.VERSION，
    会调用此方法。如果不实现，HA 会报错 500。
    """
    target_version = StateGridOnnxConfigFlow.VERSION
    LOGGER.info("ConfigEntry 迁移: 版本 %s -> %s", entry.version, target_version)
    # 我们不需要做任何数据结构变换，直接升级版本号即可
    # 因为所有字段都是 Optional，旧版本数据能兼容新版本
    if entry.version < target_version:
        hass.config_entries.async_update_entry(entry, version=target_version)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """移除集成时不删除存储文件。"""
    return None
