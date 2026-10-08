"""国家电网集成的配置向导：添加与改密码都走 App 通道。

取数以 App 通道为主；网页那条纯 HTTP 通道（`web_api.py`）只在某个格子缓存没货时才发请求，
它不需要验证码，也不发浏览器。这一路的备用登录标识（邮箱）就在选项里填：手机号那把
被硬拒（RK001）时网页登录自动改用邮箱，两把都被拒才等明天。

服务端把这台合成设备当成"新设备"时会要求一次短信验证（`resultCode=4006`）：那时多发一个
请求让国网把验证码发到手机号，拿回短时效的 codeKey，配置向导多出一页填 6 位数字，再把
验证码连同 codeKey 塞回**同一个登录请求**。只在被要求时发生，日常取数碰不到它。
"""
import hashlib

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .app_api import AppChannel
from .const import DOMAIN
from .data_client import StateGridDataClient
from .utils.logger import LOGGER

USER_HINT = (
    "取数默认由网页那条纯 HTTP 通道主讲（不开浏览器、不过验证码），App 接口灌进来的那份兜底；"
    "两个选项都能在「配置」里改回 App 优先。\n\n"
    "添加与改密码都走 App 通道：配好之后按刷新间隔取数，默认 12 小时一次（一天两次）。"
    "网页被硬拒（RK001）时会自动改用备用标识，两把都不行就等第二天，不需要你动手。\n\n"
    "「上个月抄表」这一格站点给回的就是 0，所以它会显示 0；其余实体都有值。"
)

SMS_HINT = (
    "国网把这次登录当成「新设备」，需要一次短信验证。验证码已经发到你登记的那个手机号上，"
    "请把收到的 6 位数字填进来。\n\n"
    "这只需要做一次，之后的自动登录不再问；验证码过期或填错可以重来。"
)

# App 登录失败的原因 → 配置页的错误键。"密码错了"和"今天被限流"要让用户做的事完全不同，
# 都回一句"登录失败"只会让人反复改密码
APP_ERROR_KEYS = {
    "invalid_auth": "invalid_auth",
    "invalid_code": "invalid_verification_code",
    "rate_limited": "rk001_rate_limit",
    "cannot_connect": "cannot_connect",
    "new_device": "new_device_required",
    "captcha_required": "captcha_required",
    "unknown": "app_login_failed",
}


async def app_sign_in(hass, account: str, password: str, code: str = "",
                      code_key: str = "") -> tuple[bool, str, str]:
    """用明文密码做一次 App 登录，返回 (是否成功, 配置页错误键, 短信 codeKey)。

    App 接口收的是密码的 md5 摘要，HA 里存的也正是摘要；明文只在这次调用里用完就丢，
    不进配置项、不进 store、不进日志。

    第三项非空表示服务端要"新设备验证"、而且短信已经发出去了，调用方该去要验证码。
    已经带着验证码回来过一次就**不再重发**：不然用户每填错一次就多一条短信。
    """
    channel = AppChannel(hass, account, hashlib.md5(password.encode()).hexdigest())
    if await channel.async_login(force=True, code=code, code_key=code_key):
        return True, "", ""
    err_key = APP_ERROR_KEYS.get(channel.last_error, "app_login_failed")
    if channel.needs_device_sms and not code:
        sent = await channel.async_send_device_sms()
        return False, err_key, sent
    return False, err_key, ""


class StateGridConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """配置步骤：手机号 + 密码，App 通道登录成功就建条目（被要求时多一步短信）。"""

    VERSION = 12

    def __init__(self) -> None:
        # 短信那一步要跨步骤留住这几样：手机号、明文密码（只在内存里，用完就清）、codeKey
        self._pending: dict[str, str] = {}

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if self.hass.data.get(DOMAIN):
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        phone = ""
        password = ""

        if user_input is not None:
            phone = str(user_input.get("phone", "")).strip()
            password = str(user_input.get("password", ""))

            if not phone or not password:
                errors["base"] = "invalid_auth"
            elif not phone.isdigit():
                errors["base"] = "invalid_phone"
            else:
                ok, err_key, code_key = await app_sign_in(self.hass, phone, password)
                if ok:
                    return await self._async_finish(phone, password)
                if code_key:
                    self._pending = {"phone": phone, "password": password,
                                     "code_key": code_key}
                    return await self.async_step_device_verification()
                errors["base"] = err_key

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("phone", default=phone): selector(
                        {"text": {"type": "text"}}
                    ),
                    vol.Required("password", default=password): selector(
                        {"text": {"type": "password"}}
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"how_it_works": USER_HINT},
        )

    async def async_step_device_verification(self, user_input=None):
        """填手机收到的 6 位验证码：带着它和 codeKey 再登一次同一个接口。"""
        errors: dict[str, str] = {}
        phone = str(self._pending.get("phone") or "")
        if user_input is not None:
            code = str(user_input.get("verification_code", "")).strip()
            if len(code) != 6 or not code.isdigit():
                errors["base"] = "invalid_verification_code"
            else:
                ok, err_key, _ = await app_sign_in(
                    self.hass, phone, str(self._pending.get("password") or ""),
                    code=code, code_key=str(self._pending.get("code_key") or ""))
                if ok:
                    return await self._async_finish(
                        phone, str(self._pending.get("password") or ""))
                # codeKey 是一次性的，用错一次就作废：让用户回去重提一次密码，
                # 那里会重新发一条短信，而不是在这一页上悄悄重发
                self._pending = {}
                errors["base"] = err_key or "app_login_failed"
        return self.async_show_form(
            step_id="device_verification",
            data_schema=vol.Schema({
                vol.Required("verification_code"): selector({"text": {"type": "text"}})
            }),
            errors=errors,
            description_placeholders={"sms_hint": SMS_HINT},
        )

    async def _async_finish(self, phone: str, password: str):
        """登录成功之后建条目：凭证只落 store，明文密码不留在这个流程对象里。"""
        self._pending = {}
        dc = StateGridDataClient(hass=self.hass, config=None)
        # 凭证只落 store：App 通道每轮取数读的就是这两项。网页那套会话字段
        # （keyCode/accessToken/userInfo…）已经没有生产者，不再往 store 里写
        dc.account = phone
        dc.password = hashlib.md5(password.encode()).hexdigest().upper()
        try:
            await dc.save_data()
        except Exception:
            LOGGER.exception("保存 state_grid.config 失败，但登录已成功。")
        self.hass.data[DOMAIN] = dc
        LOGGER.warning("[配置] App 通道登录成功，接下来按刷新间隔供数")
        return self.async_create_entry(title=f"国家电网 - {phone}", data={})

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        """返回选项流程。"""
        return OptionsFlowHandler(entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """集成选项：改国网登录密码、改刷新间隔。"""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # 新版 HA（Python 3.14 + 最新 HA）的 OptionsFlow 基类把 config_entry
        # 设为只读 property（无 setter），同时基类没有定义接受参数的 __init__，
        # 所以：
        #   - super().__init__(config_entry) 会触发 object.__init__() 报 TypeError
        #   - self.config_entry = config_entry 会触发 AttributeError
        # 解决方案：不碰 config_entry 这个名字，用自己的私有属性 _entry 保存。
        self._entry = config_entry
        self._pending: dict[str, str] = {}

    async def async_step_init(self, user_input=None):
        current = {**(self._entry.data or {}), **(self._entry.options or {})}
        errors: dict[str, str] = {}
        new_data: dict[str, object] = {}
        interval = str(current.get("refresh_interval", 12))
        web_on = bool(current.get("web_channel", True))
        prio_on = bool(current.get("web_priority", True))
        email = str(current.get("email_account") or "")

        if user_input is not None:
            raw_interval = user_input.get("refresh_interval")
            interval = str(raw_interval or interval)
            if raw_interval:
                try:
                    new_data["refresh_interval"] = max(12, min(48, int(str(raw_interval).strip())))
                except (ValueError, TypeError):
                    errors["refresh_interval"] = "invalid_interval"

            # 这几项要在改密码那一段**之前**收：那条路成功时会带着 new_data 直接 return，
            # 放它后面就等于"改了密码顺手把开关和备用标识丢掉"
            if "web_channel" in user_input:
                new_data["web_channel"] = bool(user_input.get("web_channel"))
            if "web_priority" in user_input:
                new_data["web_priority"] = bool(user_input.get("web_priority"))
            if "email_account" in user_input:
                new_email = str(user_input.get("email_account") or "").strip()
                if new_email and "@" not in new_email:
                    errors["email_account"] = "invalid_email"
                else:
                    new_data["email_account"] = new_email

            new_password = str(user_input.get("new_password") or "").strip()
            if new_password and not errors:
                dc = self.hass.data.get(DOMAIN)
                account = str(getattr(dc, "account", "") or "")
                if dc is None or not account:
                    # 运行中没有实例就没人能报出账号，也不该拿空账号去试密码
                    errors["new_password"] = "no_account"
                else:
                    ok, err_key, code_key = await app_sign_in(self.hass, account, new_password)
                    if ok:
                        return await self._async_save_password(new_password, new_data)
                    if code_key:
                        self._pending = {"account": account, "password": new_password,
                                         "code_key": code_key}
                        self._pending_data = new_data
                        return await self.async_step_device_verification()
                    errors["new_password"] = err_key

            if not errors:
                if new_data:
                    dc = self.hass.data.get(DOMAIN)
                    if dc is not None and "refresh_interval" in new_data:
                        dc.refresh_interval = new_data["refresh_interval"]
                    if dc is not None and "web_channel" in new_data:
                        # 关掉时把已挂的会话一起摘掉，别让 __fetch 继续用旧实例发请求
                        dc.web_channel = new_data["web_channel"]
                        if not new_data["web_channel"]:
                            dc.web = None
                    if dc is not None and "web_priority" in new_data:
                        dc.web_priority = new_data["web_priority"]
                    if dc is not None and "email_account" in new_data:
                        # 换了备用标识就得重挂一次：实例里记的还是旧的那把
                        dc.email_account = new_data["email_account"]
                        dc.web = None
                    return self.async_create_entry(title="", data=new_data)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "refresh_interval", default=interval,
                        description="刷新间隔（小时，填 12-48 之间的整数）",
                    ): selector({"text": {"type": "text"}}),
                    vol.Optional(
                        "web_channel", default=web_on,
                        description="网页通道（关掉则完全不发网页请求，只吃 App 与推送）",
                    ): selector({"boolean": {}}),
                    vol.Optional(
                        "web_priority", default=prio_on,
                        description="网页优先取数（每格先问网页，App 灌进来的那份兜底）；"
                                    "关掉则回到 App 优先、网页只补没货的格子",
                    ): selector({"boolean": {}}),
                    vol.Optional(
                        "email_account", default=email,
                        description="备用登录标识（选填，一般填登记在国网的邮箱）：手机号那把被硬拒时，"
                                    "网页登录改用这一把，不用你动手",
                    ): selector({"text": {"type": "text"}}),
                    vol.Optional(
                        "new_password", default="",
                        description="修改国家电网密码时填写（留空不修改）；填写后会走 App 通道验证一次",
                    ): selector({"text": {"type": "password"}}),
                }
            ),
            errors=errors,
        )

    async def async_step_device_verification(self, user_input=None):
        """改密码时也可能会被要求一次短信验证，流程和添加时一样。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = str(user_input.get("verification_code", "")).strip()
            if len(code) != 6 or not code.isdigit():
                errors["base"] = "invalid_verification_code"
            else:
                ok, err_key, _ = await app_sign_in(
                    self.hass, str(self._pending.get("account") or ""),
                    str(self._pending.get("password") or ""),
                    code=code, code_key=str(self._pending.get("code_key") or ""))
                if ok:
                    return await self._async_save_password(
                        str(self._pending.get("password") or ""),
                        getattr(self, "_pending_data", {}) or {})
                self._pending = {}
                errors["new_password"] = err_key or "app_login_failed"
        return self.async_show_form(
            step_id="device_verification",
            data_schema=vol.Schema({
                vol.Required("verification_code"): selector({"text": {"type": "text"}})
            }),
            errors=errors,
            description_placeholders={"sms_hint": SMS_HINT},
        )

    async def _async_save_password(self, new_password: str, extra: dict[str, object]):
        """新密码已被 App 通道接受：摘要落 store，把刷新间隔一起结掉。"""
        self._pending = {}
        dc = self.hass.data.get(DOMAIN)
        # App 登录已经把新会话写进设备 store；这里再把新摘要落进
        # state_grid.config，下一轮取数就用它
        if dc is not None:
            dc.password = hashlib.md5(new_password.encode()).hexdigest().upper()
            try:
                await dc.save_data()
                LOGGER.warning("[改密码] 新密码经 App 通道验证通过，已写入 store")
            except Exception:
                LOGGER.exception("[改密码] App 登录成功但保存 store 失败")
            if "refresh_interval" in extra:
                dc.refresh_interval = extra["refresh_interval"]
        return self.async_create_entry(title="", data=extra)
