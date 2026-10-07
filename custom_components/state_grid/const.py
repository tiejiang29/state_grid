DOMAIN = "state_grid"
PACKAGE_NAME = "custom_components.state_grid"
VERSION = "0.9.6"   # 和 manifest.json 的 version 保持一致：store 里的 dataVersion 读的是这一份
VERSION_STORAGE = 21
STORAGE_KEY = "state_grid.config"

# 流控码：11401 = RK001「网络连接超时（RK001）,请重试！」。它是**按登录标识**计的日额度，
# 换客户端、换 IP 都没用；App 接口面回的是同一个字符串码，所以拿它当"密码没错但今天到顶了"的判据。
RATE_LIMIT_CODES = {"11401", "RK001"}   # 11401 是网关码，RK001 有时直接坐在 code 上
# 站点前端自己把这三个码当"要弹哪种验证码"用：
# complexSliderType={'RK1003':'clickImg','RK008':'clickWord','RK007':'blockPuzzle'}
# 出自我们存下的 chunk-83665648.js，同一段里 RK007 分支直接 new TencentCaptcha(...).show()
CAPTCHA_CODES = {"RK007", "RK008", "RK1003"}
# 服务端说"这份会话已经不算数了"的码。它会**在 expires_at 之前**就作废（上游
# hass-state-grid issue #2：装好第 3 天就没了），所以不能只信本地存的那个时间戳。
SESSION_DEAD_CODES = {"-200", "-201"}
