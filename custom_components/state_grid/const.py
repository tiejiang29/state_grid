DOMAIN = "state_grid"
PACKAGE_NAME = "custom_components.state_grid"
VERSION = "0.7.10"
VERSION_STORAGE = 21
STORAGE_KEY = "state_grid.config"

# 流控相关错误码（密码登录日额度限制）
# 11401 = RK001 限流（"网络连接超时（RK001）,请重试！"）
# 注意: RK001是账号维度的限流，不是IP限流
# 手机号被限流后邮箱账号仍可正常登录（自动降级）
FLOW_CONTROL_CODES = {11401}

# LLM 验证码识别配置
LLM_API_KEY = ""
LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
LLM_MODEL = "doubao-seed-2-0-pro-260215"
