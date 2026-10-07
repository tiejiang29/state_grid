from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from aiohttp import web
import secrets
from .const import DOMAIN
from .utils.logger import LOGGER
from .utils.store import async_load_from_store
from .data_client import StateGridDataClient
from .config_flow import StateGridConfigFlow

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONF_PUSH_WEBHOOK = "push_webhook_id"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """当用户在 UI 里点击"添加集成"并完成配置时调用。"""
    config = await async_load_from_store(hass, "state_grid.config") or None
    data_client = StateGridDataClient(hass=hass, config=config)

    # 配置优先级：entry.options > entry.data > 存储中的 config
    # entry.options 是用户在"配置"按钮中修改的最新值
    merged = {**(entry.data or {}), **(entry.options or {})}
    # LLM 三项已经从本集成移除（网页验证码那套不发了）；旧 entry.data 里
    # 残留的那些键会被直接忽略，不读也不回写。备用邮箱重新回来了，但它只做一件事：
    # 网页登录被硬拒（RK001）时换一把标识，见 web_api.WebChannel
    data_client.email_account = str(merged.get("email_account") or "")
    data_client.web_channel = bool(merged.get("web_channel", True))
    # 网页通道开关：默认开。它是"缓存没命中才走网络"的补齐路径，不影响 App 供数节奏。
    data_client.web_channel = bool(merged.get("web_channel", True))
    LOGGER.warning("供数通道：App 优先 + 网页补齐=%s（网页只在推送缓存没命中时发请求；备用标识%s）",
                   "开" if data_client.web_channel else "关",
                   "已配" if data_client.email_account else "未配")
    if "refresh_interval" in merged:
        try:
            data_client.refresh_interval = max(12, int(merged["refresh_interval"]))
        except (ValueError, TypeError):
            pass

    hass.data[DOMAIN] = data_client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_setup_push_webhook(hass, entry, data_client)
    return True


async def _async_setup_push_webhook(hass: HomeAssistant, entry: ConfigEntry,
                                    data_client: StateGridDataClient) -> None:
    """注册网页那侧数据的推送入口：/api/webhook/<id>。

    id 是随机 128 位，首次 setup 生成后写进 entry.data 持久化，用户不用在任何界面里填。
    只允许内网推送（推送方和 HA 在同一台 NAS 局域网上），外网访问直接拒。
    没有推送过来时这条路完全不参与供数，正常安装的人不需要知道它的存在。
    """
    webhook_id = (entry.data or {}).get(CONF_PUSH_WEBHOOK)
    if not webhook_id:
        webhook_id = secrets.token_hex(16)
        hass.config_entries.async_update_entry(
            entry, data={**(entry.data or {}), CONF_PUSH_WEBHOOK: webhook_id})
    # 每次 setup 都打一遍，日志滚没了还能去 .storage/core.config_entries 里捞。
    # 用 warning 级：这台 HA 容器的控制台只放 WARNING 以上，info 级的地址等于没打。
    LOGGER.warning("网页兜底推送入口: /api/webhook/%s（只有 state_grid_docker 容器 POST 过来才供数，没收到推送时不参与）",
                   webhook_id)

    async def handle_push(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        # 打在解析之前：任何一次 HTTP 命中都留得下痕迹（方法+来源，不含钩子地址本身）。
        # 这样"到底有没有人在推"可以直接用一个空 body 的请求验出来，而不用真推一份
        # 载荷——ingest_push 是整包替换，试错会把 App 通道那份好数据顶掉。
        LOGGER.warning("收到推送请求：%s 来自 %s", request.method, request.remote)
        try:
            bundle = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "body 不是 JSON"}, status=400)
        count = await data_client.ingest_push(bundle)
        # 先回响应再刷新：解析 800 行数据要几秒，别让推送方干等一个可能超时的大请求
        coordinator = data_client.coordinator
        if coordinator is not None:
            hass.async_create_task(coordinator.async_refresh())
        else:
            LOGGER.warning("收到推送但 coordinator 还没就绪，数据留在缓存里等下一次轮询")
        return web.json_response({"ok": True, "stored": count, "meta": data_client.push_meta})

    webhook_register(hass, DOMAIN, "state_grid 网页兜底推送", webhook_id, handle_push, local_only=True)
    entry.async_on_unload(lambda: webhook_unregister(hass, webhook_id))


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
    target_version = StateGridConfigFlow.VERSION
    LOGGER.info("ConfigEntry 迁移: 版本 %s -> %s", entry.version, target_version)
    # 我们不需要做任何数据结构变换，直接升级版本号即可
    # 因为所有字段都是 Optional，旧版本数据能兼容新版本
    if entry.version < target_version:
        hass.config_entries.async_update_entry(entry, version=target_version)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """移除集成时不删除存储文件。"""
    return None
