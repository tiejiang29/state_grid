"""
LLM 验证码解算器 - 支持点选和滑块两种验证码类型

参考 sgcc_electricity_new 的 click_captcha_solver.py，适配纯 API 方式（无 Selenium）。
使用 OpenAI 兼容 API 调用视觉大模型识别验证码。

策略:
1. 点选验证码：下载参考图标条+主图，LLM 识别图标位置返回坐标
2. 滑块验证码：下载背景图，LLM 识别缺口位置返回滑块距离
"""

import base64
import io
import json
import logging
import re
from typing import List, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# 延迟导入 openai，避免在 HA 启动时报错
_OPENAI_CLIENT = None
_LLM_CONFIG = {}


def configure_llm(api_key: str, base_url: str, model: str):
    """配置 LLM 参数，在 config_flow 中调用。"""
    global _LLM_CONFIG
    _LLM_CONFIG = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    # 重置客户端以便下次重新创建
    global _OPENAI_CLIENT
    _OPENAI_CLIENT = None


def get_llm_client():
    """获取 OpenAI 客户端（懒加载）。"""
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        try:
            from openai import OpenAI
            _OPENAI_CLIENT = OpenAI(
                base_url=_LLM_CONFIG.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
                api_key=_LLM_CONFIG.get("api_key", ""),
            )
        except ImportError:
            logger.error("openai 包未安装，请运行: pip install openai")
            raise
        except Exception as e:
            logger.error(f"创建 OpenAI 客户端失败: {e}")
            raise
    return _OPENAI_CLIENT


def get_llm_model() -> str:
    """获取当前配置的模型名称（无默认值，必须由用户配置时填入）。"""
    return _LLM_CONFIG.get("model", "")


def base64_to_bytes(base64_data: str) -> bytes:
    """将 base64 图片数据转为 bytes。"""
    if base64_data.startswith("data:image"):
        base64_data = base64_data.split(",", 1)[1]
    return base64.b64decode(base64_data)


def base64_to_image(base64_data: str) -> Image.Image:
    """将 base64 图片数据转为 PIL Image。"""
    raw = base64_to_bytes(base64_data)
    return Image.open(io.BytesIO(raw))


def image_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    """将 PIL Image 转为 data URI。"""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/{fmt.lower()};base64,{b64}"


# ═══════════════════════════════════════════════════════════
# 点选验证码解算
# ═══════════════════════════════════════════════════════════

def solve_click_captcha(
    ref_base64: str,
    main_base64: str,
    main_width: int,
    main_height: int,
) -> List[Tuple[int, int]]:
    """
    解算点选验证码。

    参数:
        ref_base64: 参考图标条的 base64 数据
        main_base64: 主图（图标网格）的 base64 数据
        main_width: 主图宽度（像素）
        main_height: 主图高度（像素）

    返回:
        坐标列表 [(x1,y1), (x2,y2), (x3,y3)]，按参考图标顺序
    """
    try:
        # 1. 解码参考图标条并拆分为3个独立图标
        ref_img = base64_to_image(ref_base64)
        icon_uris = _split_strip(ref_img)
        if len(icon_uris) < 3:
            logger.error(f"参考图标条拆分失败，仅得到 {len(icon_uris)} 个图标")
            return []

        # 2. 解码主图并转为 data URI
        main_img = base64_to_image(main_base64)
        main_uri = image_to_data_uri(main_img)

        # 3. 调用 LLM 识别所有图标位置
        coords = _find_all_icons(icon_uris, main_uri, main_width, main_height)
        if len(coords) < 2:
            logger.warning(f"LLM 仅返回 {len(coords)} 个坐标点")
            return []

        # 钳制到主图范围内
        result = [
            (max(0, min(x, main_width - 1)), max(0, min(y, main_height - 1)))
            for x, y in coords
        ]
        logger.info(f"点选验证码解算结果: {result}")
        return result

    except Exception as e:
        logger.error(f"点选验证码解算失败: {e}")
        return []


def _split_strip(strip_img: Image.Image) -> List[str]:
    """将参考图标条三等分为独立图标的 data URI 列表。"""
    try:
        w, h = strip_img.size
        part_w = w // 3
        uris = []
        for i in range(3):
            left = i * part_w
            right = (i + 1) * part_w if i < 2 else w
            icon = strip_img.crop((left, 0, right, h))
            # 放大图标以便 LLM 看清细节
            icon = icon.resize((icon.width * 3, icon.height * 3), Image.LANCZOS)
            uris.append(image_to_data_uri(icon))
            logger.info(f"参考图标 #{i + 1}: {icon.width}x{icon.height}")
        return uris
    except Exception as e:
        logger.error(f"参考图标条拆分错误: {e}")
        return []


def _find_all_icons(
    icon_uris: List[str],
    main_uri: str,
    main_width: int,
    main_height: int,
) -> List[Tuple[int, int]]:
    """单次 LLM API 调用，找到所有3个图标位置。"""
    prompt = (
        f"大图（{main_width}×{main_height}像素）是一个图标网格。\n"
        "找到3个参考图标(A, B, C)各自在大图网格中的位置。\n"
        "匹配规则：形状和颜色必须一致，空心/实心、线条粗细是关键区分点，允许旋转。\n\n"
        '输出JSON：{"coords":[[xA,yA],[xB,yB],[xC,yC]]}\n'
        "其中x、y为图标中心的比例坐标（0~1）。"
    )

    content = []
    labels = ["A", "B", "C"]
    for i, uri in enumerate(icon_uris[:3]):
        content.append({"type": "image_url", "image_url": {"url": uri}})
        content.append({"type": "text", "text": f"参考图标{labels[i]}"})

    content.append({"type": "image_url", "image_url": {"url": main_uri}})
    content.append({"type": "text", "text": prompt})

    try:
        client = get_llm_client()
        response = client.chat.completions.create(
            model=get_llm_model(),
            messages=[
                {
                    "role": "system",
                    "content": "Output valid JSON only. No markdown, no explanation.",
                },
                {"role": "user", "content": content},
            ],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        output = response.choices[0].message.content or ""
        logger.info(f"点选验证码 LLM 响应: {output[:400]}")
        return _parse_click_coordinates(output, main_width, main_height)
    except Exception as e:
        logger.error(f"点选验证码 LLM 调用失败: {e}")
        return []


def _parse_click_coordinates(
    text: str, main_width: int, main_height: int
) -> List[Tuple[int, int]]:
    """从 LLM 返回文本中提取 JSON 坐标并转为像素。"""
    # 优先尝试 JSON 解析
    match = re.search(r'\{.*"coords"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            result = []
            for x, y in data["coords"]:
                x, y = float(x), float(y)
                if max(x, y) <= 1.5:
                    result.append((round(x * main_width), round(y * main_height)))
                else:
                    result.append((round(x), round(y)))
            return result
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"JSON 坐标解析失败: {e}")

    # 回退：正则解析
    coords = []
    paren_pairs = re.findall(r'\(\s*(\d+\.?\d*)\s*[,，]\s*(\d+\.?\d*)\s*\)', text)
    for x_str, y_str in paren_pairs:
        coords.append((float(x_str), float(y_str)))

    if not coords:
        nums = re.findall(r'(\d+\.?\d+)', text)
        for i in range(0, len(nums) - 1, 2):
            coords.append((float(nums[i]), float(nums[i + 1])))

    result = []
    for x, y in coords[:3]:
        if max(x, y) <= 1.5:
            result.append((round(x * main_width), round(y * main_height)))
        else:
            result.append((round(x), round(y)))
    return result


# ═══════════════════════════════════════════════════════════
# 滑块验证码解算（LLM 方式）
# ═══════════════════════════════════════════════════════════

def solve_slider_captcha_llm(
    canvas_base64: str,
    canvas_width: int = 310,
    canvas_height: int = 200,
) -> int:
    """
    使用 LLM 解算滑块验证码，返回滑块距离（像素）。

    参数:
        canvas_base64: 背景图（含缺口）的 base64 数据
        canvas_width: 背景图宽度
        canvas_height: 背景图高度

    返回:
        滑块需要移动的像素距离
    """
    try:
        canvas_img = base64_to_image(canvas_base64)
        bg_w, bg_h = canvas_img.size
        canvas_uri = image_to_data_uri(canvas_img)

        client = get_llm_client()
        response = client.chat.completions.create(
            model=get_llm_model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": canvas_uri}},
                        {
                            "type": "text",
                            "text": (
                                f"这是一个滑块拼图验证码的背景图（{bg_w}x{bg_h}像素）。\n"
                                "图中有一个矩形缺口（拼图块被挖掉的位置），缺口边缘有轻微阴影或颜色差异。\n"
                                "请找到这个缺口，返回缺口左侧边缘的X坐标比例（0~1之间）。\n"
                                "输出格式（仅一个数字）：0.XX"
                            ),
                        },
                    ],
                }
            ],
            max_tokens=50,
        )

        output = response.choices[0].message.content or ""
        logger.info(f"滑块验证码 LLM 响应: {output[:100]}")

        # 解析比例
        nums = re.findall(r'(\d+\.?\d*)', output)
        if not nums:
            logger.warning("无法从 LLM 响应中解析滑块位置")
            return 0
        ratio = float(nums[0])
        if ratio > 1.5:
            ratio = ratio / bg_w
        ratio = max(0.0, min(1.0, ratio))

        distance = int(ratio * canvas_width)
        logger.info(f"滑块缺口比例: {ratio:.3f}, 距离: {distance}px")
        return distance

    except Exception as e:
        logger.error(f"滑块验证码 LLM 解算失败: {e}")
        return 0


# ═══════════════════════════════════════════════════════════
# 验证码类型检测
# ═══════════════════════════════════════════════════════════

def detect_captcha_type(captcha_data: dict) -> str:
    """
    根据 API 返回的验证码数据检测验证码类型。

    滑块验证码返回: canvasSrc(背景) + blockSrc(滑块块) + blockY(Y坐标)
    点选验证码返回: iconSrc/wordSrc(参考图标条) + canvasSrc(主图)

    返回:
        "click" 或 "slider"
    """
    # 如果有参考图标条相关字段，说明是点选
    if "iconSrc" in captcha_data or "wordSrc" in captcha_data or "iconSrcs" in captcha_data:
        return "click"

    # 如果有 blockSrc（滑块拼图块），说明是滑块
    if "blockSrc" in captcha_data:
        return "slider"

    # 如果有 canvasSrc 但没有 blockSrc，可能是点选
    if "canvasSrc" in captcha_data and "blockSrc" not in captcha_data:
        return "click"

    # 默认尝试滑块（兼容旧逻辑）
    return "slider"
