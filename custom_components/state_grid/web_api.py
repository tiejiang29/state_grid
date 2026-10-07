"""网页通道：纯 HTTP 登录 + 会话维持 + 通用信封收发。

2026-10-05 实测通的三步，缺一样就退回 RK001/RK1003：
  1. 登录体**只发 `params`**——旧版多带的 `loginKey`/`code`/`Channels` 现在整站都不发了，
     带着它们服务端直接硬拒（这就是 9 月底以来网页登录坏掉的真正原因，不是风控升级）；
     `optSys` 用 `android`、`pushId` 用 `000000`（`ios`/`00000` 是短信那支的值）。
  2. **先空手发一次**拿 `RK1003`——那是服务端给这个客户端挂的一道待答题；
  3. 再发一次补 `complexSliderRet:0` + `complexSliderType:'clickImg'`。
     服务端只认"我刚下发过题 + 提交里带了 ret"，**从不回头向腾讯核对**，所以零浏览器、零验证码。

另外三条也对齐了：**不发 `sessionId`**（浏览器在 f06 上就不发它）、`Authorization`/`t` 各取
令牌的前一半（站点自己就这么截）、f02 的信封带第四个字段 `client_id`（漏了直接 `GB007`）。
`retryCount` 浏览器发、今天跑通的那两发没发——它到底该不该带**还没测出来**（该标识随后进入
硬拒态，两种写法都只回 RK001 了），所以这里先跟成功那两发保持一致：不发。

登录被硬拒（RK001）是**按登录标识**判的，所以这里备了两把：主标识（手机号）先上，
吃到 RK001 才换备用标识（登记邮箱）再上一轮；两把都被拒就冷却到当天 24 点，期间不发登录。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import random
import time
import urllib.parse
from typing import Any

from .utils.crypt import a, b, c, d, e
from .utils.logger import LOGGER

# 网页会话单独存一份：App 那份在 state_grid.app_device 里，两边互不覆盖
STORAGE_KEY_WEB_SESSION = "state_grid.web_session"
BASE_API = "https://www.95598.cn/api"
KEY_API = "/oauth2/outer/c02/f02"
AUTHORIZE_API = "/oauth2/oauth/authorize"
WEB_TOKEN_API = "/oauth2/outer/getWebToken"
LOGIN_API = "/osg-web0004/open/c44/f06"

APP_KEY = "0329843199564c55809c77959792b558"
APP_SECRET = "4c1974786ee54d3bb4fb82c1ec5cd1a8"
SERVER_PUBLIC_KEY = ("04461932EDC916BFEF2EA324056296214E8281FDF9F962C82E28D59C7B98BB5ED"
                     "479801B8AB8F86E933B73A136A431D40E0FF769A7209E63E67C8B9326F277A058")
LOGIN_STATE = "state_grid"
LOGIN_MEMBER = "0902"
# 服务端发的 access_token 实测 expiresIn≈1799（约 30 分钟），提前 5 分钟就换新的
BEARER_LIFE_SAFETY_S = 300
# 登录令牌按服务端给的时间算，兜底当 15 天
TOKEN_FALLBACK_S = 15 * 86400

# 只有这几个业务接口带 Authorization / t（照旧版的清单）
AUTH_APIS = ("/osg-open-uc0001/member/c9/f02", "/osg-open-bc0001/member/c05/f01",
             "/osg-open-bc0001/member/c01/f02", "/osg-open-bc0001/member/c04/f03",
             "/osg-web0004/member/c24/f01")
JSON_TYPE = "application/json;charset=UTF-8"

# 每发之间隔这一截再发下一发。首轮回补阶梯一轮能连发 20 多下，之前全挤在两秒内，
# 那个形态本身就像机器；稳态只有 3-5 发，隔不隔都看不出差别。
GAP_MIN_S = 0.3
GAP_MAX_S = 0.5


def _end_of_day_ts() -> float:
    """当天 23:59:59（北京时间）的 unix 秒。RK001 是按天翻脸的，冷却就按天平。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def _payload(plain: dict[str, Any]) -> dict[str, Any]:
    """解开的响应是 `{code, data, message}`：握手/bearer 的字段直接在 `data` 里，
    只有登录和报错才再套一层 `bizrt` / `srvrt`。所以这里两层都摸一下。"""
    if not isinstance(plain, dict):
        return {}
    data = plain.get("data")
    if isinstance(data, dict):
        biz = data.get("bizrt")
        return biz if isinstance(biz, dict) else data
    return plain


def _srvrt(plain: dict[str, Any]) -> dict[str, Any]:
    """错误详情住在 `data.srvrt` 里（resultCode / resultMessage），两层都可能是 dict 或没有。"""
    data = plain.get("data") if isinstance(plain, dict) else None
    srv = data.get("srvrt") if isinstance(data, dict) else None
    return srv if isinstance(srv, dict) else {}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _decrypt(plain: dict[str, Any], key: str) -> dict[str, Any]:
    """回包有两种形状：`{"encryptData":...}` 和 `{"data":"<密文>"}`（authorize 用后者）。"""
    if not isinstance(plain, dict):
        return {}
    blob = plain.get("encryptData")
    if not blob and isinstance(plain.get("data"), str):
        blob = plain["data"]
    if blob and key:
        try:
            inner = json.loads(b(str(blob), key))
            if isinstance(inner, dict):
                return inner
        except Exception:
            pass          # 解不开就按明文那份判码，别在这里编造成功
    return plain


class WebChannel:
    """一份网页会话：登录、续期 bearer、发业务请求。不碰解析层，只回原始响应。"""

    def __init__(self, hass, session, account: str, password_digest: str, store,
                 alt_account: str = "") -> None:
        self.hass = hass
        self.session = session            # HA 的 aiohttp clientsession（带 cookie jar）
        self.account = account
        # 配置里那把主标识的原样备份：轮换到备用标识时，日志要说清用的是哪一把
        self.primary_account = account
        # 备用标识（一般是登记在国网那边的邮箱）：只在主标识吃到 RK001 时才顶上，
        # 因为 RK001 是**按登录标识**判的，换一把当天没碰过的就能登进去
        self.alt_account = alt_account
        # 两把标识都吃到 RK001 之后填上"当天结束"，期间一发登录都不再发
        self.cooldown_until = 0.0
        # 集成里存的是 32 位 MD5 摘要（App 通道也用同一份），f06 要的就是它的大写形式。
        # 别再 hash 一遍——那等于拿摘要当密码去登录，服务端只会回密码错误。
        self.password_digest = password_digest
        self.store = store
        self.key_code = ""                # 服务端发的会话密钥
        self.public_key = ""              # 服务端发的公钥
        self.token = ""                   # 登录令牌（f06 的 bizrt.token，旧代码里叫 rsi）
        self.user_info: dict[str, Any] = {}
        self.access_token = ""
        self.bearer_deadline = 0.0
        self.expires_at = 0
        self.last_error = ""
        self._lock = asyncio.Lock()

    # ---------- 会话存取 ----------
    async def async_load(self) -> bool:
        data = await self.store.async_load() or {}
        self.key_code = str(data.get("keyCode") or "")
        # store 里没带公钥就用内置那个兜底（握手那步也是这么兜的）：信封加密少了它当场 TypeError
        self.public_key = str(data.get("publicKey") or "") or SERVER_PUBLIC_KEY
        self.token = str(data.get("token") or "")
        info = data.get("userInfo")
        self.user_info = info if isinstance(info, dict) else {}
        self.access_token = str(data.get("accessToken") or "")
        self.bearer_deadline = float(data.get("bearerDeadline") or 0)
        self.expires_at = int(data.get("expiresAt") or 0)
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # 上次是哪把标识登进去的就继续沿用（省一次换标识的摸索），但只认配置里
        # 现在还有这两把之一，别让一条早就改掉的旧标识变成第三个来源。
        # `used` 必须先判非空：没配备用标识时 `alt_account` 就是空串，而 store 里
        # 没有 account 时取出来也是空串——两者一比就相等，会把主标识抹成空，
        # 于是整个网页通道一声不响地不发请求（本机假测试没这格，10-07 真机打出来才发现）
        used = str(data.get("account") or "")
        if used and used not in (self.account, self.alt_account):
            # 落盘那把已经不在配置里了（改过手机号或改过备用标识）：这份会话是**另一个账号**的，
            # 继续用就是把旧账号的数据喂给新配置，直接当不可用重登
            return False
        if used:
            self.account = used
        return bool(self.token) and self.expires_at > time.time() + 86400

    async def async_save(self) -> None:
        await self.store.async_save({"keyCode": self.key_code, "publicKey": self.public_key,
                                     "token": self.token, "userInfo": self.user_info,
                                     "accessToken": self.access_token,
                                     "bearerDeadline": self.bearer_deadline,
                                     "expiresAt": self.expires_at,
                                     "account": self.account,
                                     "cooldownUntil": self.cooldown_until,
                                     "savedAt": int(time.time())})

    def _in_cooldown(self) -> bool:
        if self.cooldown_until <= 0:
            return False
        if time.time() >= self.cooldown_until:
            self.cooldown_until = 0.0
            return False
        return True

    def _identifiers(self) -> list[str]:
        out: list[str] = []
        for acct in (self.account, self.alt_account):
            acct = str(acct or "").strip()
            if acct and acct not in out:
                out.append(acct)
        return out

    # ---------- 信封 ----------
    def _envelope(self, payload: Any, ts: int, wrap: bool = True) -> dict[str, str]:
        """密文里还套一层：`{_access_token, _t, _data, _s}`。

        这层是 10-05 离线逐字节比对时才补上的——少了它，形状全对的登录照旧被硬拒。
        两个令牌在这里取**后半段**（`accessToken[len//2:]`），而请求头里的
        `Authorization`/`t` 取的是**前半段**——站点自己就这么不对称，照抄。
        """
        # wrap=False 给 f02/getWebToken：它们在旧那版里各走自己的分支，**不套这层**
        inner = payload if not wrap else {
            "_access_token": self.access_token[len(self.access_token) // 2:]
            if self.access_token else "",
            "_t": self.token[len(self.token) // 2:] if self.token else "",
            "_data": payload, "_s": ts}
        cipher = a(_dump(inner), self.key_code)
        return {"data": cipher + c(cipher + str(ts)), "skey": d(self.key_code, self.public_key),
                "timestamp": str(ts)}

    def _headers(self, api: str, ts: int, with_key: bool = True) -> dict[str, str]:
        # 跑通那条路没带 Origin/Referer，值也照它写（`charset` 前没有空格）
        headers = {"Accept": JSON_TYPE, "Content-Type": JSON_TYPE, "version": "1.0",
                   "source": "0901", "timestamp": str(ts), "wsgwType": "web", "appKey": APP_KEY}
        # retryCount 浏览器发，但纯 HTTP 跟着发反而被判硬拒（10-05 实测），所以默认不发
        if with_key:
            headers["keyCode"] = self.key_code
        if api in AUTH_APIS and self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token[:len(self.access_token) // 2]
            headers["t"] = self.token[:len(self.token) // 2]
        return headers

    async def _post(self, api: str, payload: Any, ts: int = 0,
                    wrap: bool = True) -> dict[str, Any]:
        """加密信封发出去，回包用会话 keyCode 解。解不开就原样回（调用方看 code）。

        `ts` 必须由调用方传进来并**一路用到底**：内层的 `_s`、信封的 `timestamp`、请求头的
        `timestamp`，以及载荷里自己带的 `timestamp`/`sign` 得是同一个值——旧那版全程用
        `self.timestamp`，我第一版在这里各算各的，等于自己制造不一致。
        """
        ts = ts or int(time.time() * 1000)
        plain = await self._raw(BASE_API + api, self._envelope(payload, ts, wrap),
                                 self._headers(api, ts))
        return _decrypt(plain, self.key_code)

    async def _post_form(self, api: str, payload: dict[str, Any], key: str,
                         ts: int = 0) -> dict[str, Any]:
        """authorize 那一发是**明文 form**，而且回包用登录令牌解，不是 keyCode。"""
        # authorize 不在旧那份 keyCode 清单里，浏览器也不在它上面带它
        headers = self._headers(api, ts or int(time.time() * 1000), with_key=False)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        plain = await self._raw(BASE_API + api, urllib.parse.urlencode(payload), headers)
        return _decrypt(plain, key)

    async def _raw(self, url: str, body: Any, headers: dict[str, str]) -> dict[str, Any]:
        await asyncio.sleep(random.uniform(GAP_MIN_S, GAP_MAX_S))
        try:
            if isinstance(body, dict):
                async with self.session.post(url, json=body, headers=headers,
                                             timeout=30) as resp:
                    text = await resp.text()
            else:
                async with self.session.post(url, data=body, headers=headers,
                                             timeout=30) as resp:
                    text = await resp.text()
        except Exception as exc:
            self.last_error = "cannot_connect"
            LOGGER.warning("网页通道请求失败 %s：%s", url.split("/api/")[-1][:30], type(exc).__name__)
            return {}
        if not text.startswith("{"):
            self.last_error = "cannot_connect"
            LOGGER.warning("网页通道 %s 回的不是 JSON（%d 字节）", url.split("/api/")[-1][:30], len(text))
            return {}
        try:
            plain = json.loads(text)
        except Exception:
            return {}
        return plain if isinstance(plain, dict) else {}

    # ---------- 登录 ----------
    async def async_handshake(self) -> bool:
        """c02/f02：自己造一把会话密钥、用服务端公钥封给它，换回它那份 keyCode/publicKey。"""
        local_key = e(32, 16, 2)
        ts = int(time.time() * 1000)
        cipher = a(_dump({"client_id": APP_KEY, "client_secret": APP_SECRET}), local_key)
        # f02 的信封比别处多一个顶层 client_id（值就是 appKey），漏了它网关直接回 GB007
        body = {"data": cipher + c(cipher + str(ts)), "skey": d(local_key, SERVER_PUBLIC_KEY),
                "client_id": APP_KEY, "timestamp": str(ts)}
        headers = {"Accept": JSON_TYPE, "Content-Type": JSON_TYPE, "version": "1.0",
                   "source": "0901", "timestamp": str(ts), "wsgwType": "web", "appKey": APP_KEY}
        biz = _payload(_decrypt(await self._raw(BASE_API + KEY_API, body, headers), local_key))
        self.key_code = str(biz.get("keyCode") or "")
        self.public_key = str(biz.get("publicKey") or SERVER_PUBLIC_KEY)
        if not self.key_code:
            self.last_error = "unknown"
            LOGGER.warning("网页通道握手没拿到会话密钥（载荷键=%s）", sorted(biz)[:6])
            return False
        return True

    def _login_body(self, with_answer: bool) -> dict[str, Any]:
        body = {"params": {
            "uscInfo": {"devciceIp": "", "tenant": LOGIN_STATE, "member": LOGIN_MEMBER,
                        "devciceId": ""},
            "quInfo": {"optSys": "android", "pushId": "000000", "addressProvince": "110100",
                       "password": self.password_digest.upper(),
                       "addressRegion": "110101", "account": self.account,
                       "addressCity": "330100"}}}
        if with_answer:
            body["complexSliderRet"] = 0
            body["complexSliderType"] = "clickImg"
        return body

    async def _attempt_login(self) -> bool:
        """用 `self.account` 这一把标识走两步登录。成功时填好 token/userInfo。"""
        if not await self.async_handshake():
            return False
        ts = int(time.time() * 1000)
        plain = await self._post(LOGIN_API, self._login_body(False), ts)
        code = str(plain.get("code") or "")
        biz = _payload(plain)
        if not biz.get("token"):
            # 空手那一发要拿到的就是 RK1003：它等于"这道题我发给你了"
            if code not in ("RK1003", "RK007", "RK008"):
                self.last_error = _classify(code, _srvrt(plain), plain)
                LOGGER.warning("网页通道登录未取得题目分支 code=%s message=%r",
                               code, str(plain.get("message") or "")[:60])
                return False
            plain = await self._post(LOGIN_API, self._login_body(True), ts + 1)
            code = str(plain.get("code") or "")
            biz = _payload(plain)
        if not biz.get("token"):
            self.last_error = _classify(code, _srvrt(plain), plain)
            LOGGER.warning("网页通道登录未完成 code=%s message=%r", code,
                           str(plain.get("message") or "")[:60])
            return False
        self.token = str(biz["token"])
        info = biz.get("userInfo")
        self.user_info = info[0] if isinstance(info, list) and info else (info or {})
        self.expires_at = int(time.time()) + TOKEN_FALLBACK_S
        LOGGER.warning("网页通道登录成功（%s）：令牌 %d 字符、户号信息 %d 项",
                       "备用标识" if self.account != self.primary_account else "主标识",
                       len(self.token), len(self.user_info))
        return True

    async def async_login(self, force: bool = False) -> bool:
        async with self._lock:
            # store 先读一次（本地读，不发网络）：冷却到哪一天、上次用的是哪把标识，
            # 都只存在那里——不读就会在重启后把"今天已经别试了"忘掉
            usable = await self.async_load()
            if self._in_cooldown():
                LOGGER.warning("网页通道今天不再尝试登录（RK001 冷却到当天 24 点）")
                return False
            if usable and not force:
                if await self.async_ensure_bearer():
                    return True
                # 落盘的登录令牌续不动 bearer（NAS 10:31 实测 `20103 请求异常【O10011】`）：
                # 不重登就等于整轮没有网页通道。这里只补一次登录，`async_call` 里那条
                # "bearer 换不动不许顺手重登"的护栏照旧留着，同一轮不会滚成两次登录。
                LOGGER.warning("网页通道续 bearer 失败，本轮改用新会话重登一次")
            ids = self._identifiers()
            for acct in ids:
                self.account = acct
                if await self._attempt_login():
                    # 先落盘再换 bearer：服务端那条 `操作过于频繁` 是按**账号**算的，
                    # 刚拿到的登录令牌一旦丢掉就得重新走两发 f06，等于自己把限频喂大。
                    await self.async_save()
                    await self.async_ensure_bearer()
                    return True
                if self.last_error != "rate_limited":
                    # 密码错、连不上、要人工过题——换一把标识解决不了，别再多花两发
                    break
            if self.last_error == "rate_limited":
                self.cooldown_until = _end_of_day_ts()
                await self.async_save()
                LOGGER.warning("网页通道登录被硬拒（RK001，试过 %d 把标识），冷却到当天结束，"
                               "这期间只吃 App 与推送缓存", len(ids))
            return False

    # ---------- bearer ----------
    async def async_ensure_bearer(self) -> bool:
        if self.access_token and time.time() < self.bearer_deadline:
            return True
        ts = int(time.time() * 1000)
        auth = await self._post_form(AUTHORIZE_API, {
            "client_id": APP_KEY, "response_type": "code", "redirect_url": "/test",
            "timestamp": ts, "rsi": self.token}, self.token, ts)
        url = str(_payload(auth).get("redirect_url") or "")
        at = url.find("code=")
        if at < 0:
            # 只打错误码和站点文案：光打"顶层有哪些键"判不出是解不开还是服务端直接回错
            self.last_error = _classify(str(auth.get("code") or ""), _srvrt(auth), auth)
            LOGGER.warning("网页通道 authorize 没回授权码：code=%s message=%r 载荷键=%s",
                           auth.get("code"), str(auth.get("message") or "")[:40],
                           sorted(_payload(auth))[:6])
            return False
        code32 = url[at + 5:at + 37]
        tok = await self._post(WEB_TOKEN_API, {
            "grant_type": "authorization_code", "sign": c(APP_KEY + str(ts)),
            "client_secret": APP_SECRET, "state": "464606a4-184c-4beb-b442-2ab7761d0796",
            "key_code": self.key_code, "client_id": APP_KEY, "timestamp": ts,
            "code": code32}, ts, wrap=False)
        biz = _payload(tok)
        self.access_token = str(biz.get("access_token") or "")
        life = int(biz.get("expiresIn") or 1800)
        self.bearer_deadline = time.time() + max(60, life - BEARER_LIFE_SAFETY_S)
        if not self.access_token:
            self.last_error = "unknown"
            LOGGER.warning("网页通道 getWebToken 没回 access_token：code=%s message=%r 载荷键=%s",
                           tok.get("code"), str(tok.get("message") or "")[:40],
                           sorted(biz)[:6])
            return False
        LOGGER.warning("网页通道 bearer 已换：access_token %d 字符、约 %d 分钟",
                       len(self.access_token), int(self.bearer_deadline - time.time()) // 60)
        # 成功就把上一次的失败原因清掉：这个字段只该表示"最近一次为什么没成"
        self.last_error = ""
        await self.async_save()
        return True

    # ---------- 业务请求 ----------
    async def async_call(self, api: str, payload: Any) -> dict[str, Any]:
        """取一个业务接口；会话过期就重登一次再试，仍然失败只回空 dict（调用方按缺数处理）。"""
        if not await self.async_ensure_bearer():
            # bearer 换不动就不要顺手重登：那会再吃两发 f06，而限频是按账号算的
            return {}
        plain = await self._post(api, payload)
        if _session_dead(plain) and await self.async_login(force=True):
            plain = await self._post(api, payload)
        return plain


def _session_dead(plain: dict[str, Any]) -> bool:
    code = str(plain.get("code") or "")
    text = str(_srvrt(plain).get("resultMessage") or plain.get("message") or "")
    return code in ("-200", "-201") or "登录状态已失效" in text


def _classify(code: str, srvrt: dict[str, Any], plain: dict[str, Any]) -> str:
    """把网页那边的拒绝分成一小组稳定的键，好让配置页说人话。"""
    text = str(srvrt.get("resultMessage") or plain.get("message") or "")
    result_code = str(srvrt.get("resultCode") or "")
    if code in ("11401", "RK001") or "RK001" in text:
        return "rate_limited"
    if code in ("RK007", "RK008", "RK1003"):
        return "captcha_required"
    if "验证码" in text:
        return "invalid_code"
    if result_code in ("0100", "0101") or "密码" in text or "账号" in text:
        return "invalid_auth"
    if not plain:
        return "cannot_connect"
    return "unknown"


async def async_attach(client) -> "WebChannel | None":
    """给 data_client 惰性挂一个网页会话：能挂上返回通道，挂不上返回 None。

    只在"推送缓存没命中、本轮真的要发网络"那一刻被调用一次，所以 App 通道供得上的日子
    这里一次 f06 都不会发。返回 None 也照样写回 `client.web`，用来记住"这轮试过了"，
    免得同一轮里每个空格子都去登录一次。
    """
    if not getattr(client, "web_channel", False):
        return None
    account = str(getattr(client, "account", "") or "")
    digest = str(getattr(client, "password", "") or "")
    if not account or len(digest) != 32:
        LOGGER.warning("网页通道没挂上：缺账号或密码摘要（store 里的摘要长度 %d）", len(digest))
        return None
    alt = str(getattr(client, "email_account", "") or "").strip()
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store
    web = WebChannel(client.hass, async_get_clientsession(client.hass), account, digest,
                     Store(client.hass, 1, STORAGE_KEY_WEB_SESSION), alt)
    if not alt:
        LOGGER.warning("网页通道没配备用标识：一旦被硬拒（RK001）就只能等明天")
    if not await web.async_login():
        LOGGER.warning("网页通道本轮不可用（%s），这轮继续只吃缓存", web.last_error)
        return None
    LOGGER.warning("网页通道已挂上：户号信息 %d 项、bearer %d 字符",
                   len(web.user_info), len(web.access_token))
    return web
