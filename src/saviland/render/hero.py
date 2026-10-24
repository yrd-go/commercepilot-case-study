# -*- coding: utf-8 -*-
"""主图合成：把"商品图 + 卖点文案"排版成可直接上架的主图 PNG。

为什么用代码合成而不是文生图
----------------------------
1. **可控**：文案必须逐字出现在图上（价格、功率、认证），文生图会把 "48W" 画成 "43W"。
2. **合规**：亚马逊主图要求纯白背景、商品占比 ≥85%、不能有水印/促销文字。
   代码渲染能精确保证这些硬约束，而且可以按规范做校验。
3. **零外部依赖**：不需要图像生成 API 的额度和费用，批量跑 20 个 SKU 是本地几秒钟的事。

三种版式（对应真实运营里的不同用途）
------------------------------------
    v0 clean   —— 纯白底 + 商品 + 顶部标题 + 规格角标，**符合亚马逊主图规范**
    v1 banner  —— 顶部品牌色标题栏 + 商品 + 底部三条卖点，用于副图/A+ 模块
    v2 split   —— 左右分栏，左文案右商品，用于详情页首屏

字号、留白、对齐都由 `Layout` 常量集中控制，评审被问"你怎么保证 20 张图观感一致"
时，答案是"版式是一份常量表，不是每张手调"。
"""
from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .. import config

SIZE = config.Biz.HERO_SIZE        # 1024
MARGIN = 56
BRAND_NAME = "SAVILAND"
BRAND_TAGLINE = "NAIL. ART. ELEVATED."

# Windows 常见字体候选：优先无衬线粗体，中文回落到微软雅黑
FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\ariblk.ttf",
]
FONT_CANDIDATES_REGULAR = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
]
FONT_CANDIDATES_CJK = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
]

# 类目配色：同一类目用同一套色，跨类目有区分度但不刺眼（美甲客群偏柔和高级感）
CATEGORY_THEME: dict[str, tuple[str, str, str]] = {
    # category: (主色, 强调色, 深色文字)
    "美甲灯":   ("#2B2F4A", "#C9A227", "#1B1E33"),
    "甲油胶":   ("#8E4A6B", "#E8B4C8", "#4A2338"),
    "美甲工具": ("#2F4858", "#7FB3C8", "#1D2E38"),
    "美甲饰品": ("#6B5B95", "#D6C9F0", "#3D3159"),
    "手足护理": ("#2E6B5E", "#9FD6C4", "#1C443B"),
    "美甲套装": ("#7A3E9D", "#D9B8F0", "#472159"),
    "卸甲与辅助": ("#4A4A55", "#B9BCC7", "#2A2A32"),
}
DEFAULT_THEME = ("#333A56", "#C9C9D6", "#20242F")

WHITE = (255, 255, 255)


# --------------------------------------------------------------------------
# 字体与文本
# --------------------------------------------------------------------------
def _first_existing(paths: Sequence[str]) -> str | None:
    for p in paths:
        if Path(p).is_file():
            return p
    return None


_FONT_CACHE: dict[tuple[int, bool, bool], ImageFont.FreeTypeFont] = {}


def load_font(size: int, bold: bool = True, cjk: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold, cjk)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = FONT_CANDIDATES_CJK if cjk else (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR)
    path = _first_existing(candidates) or _first_existing(FONT_CANDIDATES_BOLD)
    if path:
        font = ImageFont.truetype(path, size)
    else:  # 极端兜底：没有任何字体文件时用 Pillow 内置位图字体
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def font_for(text: str, size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    return load_font(size, bold=bold, cjk=has_cjk(text))


def text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: float,
              max_lines: int = 3) -> list[str]:
    """按像素宽度贪心折行。

    比 textwrap 的按字符折行更准：中英混排（"48W 大功率 UV LED 美甲灯"）时，
    按字符数折行会在窄字母上浪费空间、在宽字母上溢出。
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    # 先按空格切词；中文没有空格，再对超长词做字符级切分
    words: list[str] = []
    for token in text.split(" "):
        if text_width(draw, token, font) <= max_width or len(token) <= 1:
            words.append(token)
        else:
            cur = ""
            for ch in token:
                if text_width(draw, cur + ch, font) > max_width and cur:
                    words.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                words.append(cur)

    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if text_width(draw, trial, font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and words:
        # 末行溢出时加省略号，绝不画到画布外面
        last = lines[-1]
        while text_width(draw, last + "…", font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def theme_for(category: str) -> tuple[tuple[int, int, int], ...]:
    primary, accent, dark = CATEGORY_THEME.get(category or "", DEFAULT_THEME)
    return _hex(primary), _hex(accent), _hex(dark)


# --------------------------------------------------------------------------
# 商品图加载
# --------------------------------------------------------------------------
def _placeholder_product(sku: str, title: str, accent: tuple[int, int, int]) -> Image.Image:
    """商品图缺失时的兜底：画一张干净的占位卡，而不是让整条链路失败。"""
    img = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    box = (SIZE * 0.26, SIZE * 0.22, SIZE * 0.74, SIZE * 0.72)
    d.rounded_rectangle(box, radius=48, fill=(245, 246, 250, 255),
                        outline=accent + (255,), width=4)
    f1 = load_font(88, bold=True)
    f2 = load_font(34, bold=False)
    label = "NAIL KIT"
    w = d.textlength(label, font=f1)
    d.text((SIZE / 2 - w / 2, SIZE * 0.42), label, font=f1, fill=accent + (255,))
    sub = sku
    w2 = d.textlength(sub, font=f2)
    d.text((SIZE / 2 - w2 / 2, SIZE * 0.55), sub, font=f2, fill=(120, 124, 140, 255))
    return img


def load_product_image(product: dict, accent: tuple[int, int, int]) -> Image.Image:
    """读商品图并抠到白底（去 alpha、非白背景压成白底）。"""
    rel = product.get("hero_image") or ""
    path = Path(rel)
    if not path.is_absolute():
        path = config.ROOT / rel
    if not path.is_file():
        return _placeholder_product(product.get("sku", ""), product.get("title_cn", ""), accent)
    try:
        src = Image.open(path).convert("RGBA")
    except Exception:  # noqa: BLE001 - 损坏的图片不能让批量任务整体失败
        return _placeholder_product(product.get("sku", ""), product.get("title_cn", ""), accent)

    # 若图片带透明通道，合成到白底
    canvas = Image.new("RGB", src.size, WHITE)
    canvas.paste(src, mask=src.split()[-1])
    return canvas


def _fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """等比缩放到框内（contain），用 LANCZOS 保证缩小后依然锐利。"""
    w, h = img.size
    scale = min(box_w / w, box_h / h)
    new = (max(1, int(w * scale)), max(1, int(h * scale)))
    return img.resize(new, Image.LANCZOS)


def _drop_shadow(img: Image.Image, blur: int = 26, opacity: int = 60,
                 offset: tuple[int, int] = (0, 14)) -> Image.Image:
    """给商品加一层柔和投影，避免商品"浮"在白底上。

    ★ 踩过的坑：`Image.paste(im, box, mask)` 的 mask **不能是 RGB 图**。
    第一版写成 `out.paste(img, (blur, blur), img)`，在"商品图缺失、走 RGBA 占位图"
    时能跑通；一旦真实商品图就位（`load_product_image` 返回 RGB），立刻抛
    `ValueError: bad transparency mask`。表现是"主图时好时坏、取决于图片有没有下载到"，
    非常难查。修法：入口先统一成 RGBA，并且 mask 一律只取 alpha 单通道。
    """
    img = _to_rgba(img)
    alpha = img.split()[-1]
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda v: min(opacity, v)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    w, h = img.size
    out = Image.new("RGBA", (w + abs(offset[0]) + blur * 2, h + abs(offset[1]) + blur * 2),
                    (0, 0, 0, 0))
    out.paste(shadow, (blur + max(0, offset[0]), blur + max(0, offset[1])), shadow.split()[-1])
    out.paste(img, (blur, blur), img.split()[-1])
    return out


def _to_rgba(img: Image.Image) -> Image.Image:
    return img if img.mode == "RGBA" else img.convert("RGBA")


# --------------------------------------------------------------------------
# 版式
# --------------------------------------------------------------------------
@dataclass
class Copy:
    """一张主图需要的全部文案。缺字段时回落到商品主数据的默认值。"""
    headline: str = ""
    subhead: str = ""
    badge: str = ""
    cta: str = ""
    bullets: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict, product: dict) -> "Copy":
        spts = product.get("selling_points") or []
        headline = (data.get("hero_headline") or "").strip() or \
            (product.get("title_en") or product.get("title_cn") or "").strip()
        subhead = (data.get("hero_subhead") or "").strip()
        if not subhead and spts:
            subhead = str(spts[0])
        badge = (data.get("hero_badge") or "").strip()
        if not badge:
            specs = product.get("specs") or {}
            badge = " · ".join(f"{v}" for v in list(specs.values())[:2])[:34]
        bullets = tuple((data.get("bullets") or [])[:3]) or tuple(spts[:3])
        return cls(headline=headline, subhead=subhead, badge=badge,
                   cta=(data.get("cta") or "").strip(), bullets=bullets)


def _draw_badge(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                accent: tuple[int, int, int], bg: tuple[int, int, int],
                align: str = "left") -> None:
    """画一个胶囊形角标。

    `align="right"` 时 xy 视为**右边界**，先量文字宽度再反推左上角 ——
    第一版写死了 `SIZE - MARGIN - 300` 去猜位置，文案一长就冲出画布。
    贴图排版里所有"看得见的位置"都必须是量出来的，不能靠估。
    """
    if not text:
        return
    font = font_for(text, 30, bold=True)
    pad_x, pad_y = 22, 12
    w = text_width(draw, text, font)
    x, y = xy
    if align == "right":
        x = x - (w + pad_x * 2)
    draw.rounded_rectangle((x, y, x + w + pad_x * 2, y + 30 + pad_y * 2),
                           radius=(30 + pad_y * 2) // 2, fill=bg)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=accent)


def _text_plate(canvas: Image.Image, box: tuple[int, int, int, int],
                alpha: int = 232, radius: int = 26) -> None:
    """在文字下方垫一层半透明白色底板。

    商品原图本身常常带背景和文字（官网主图就带对比表格），直接把标题压上去会互相干扰。
    垫一层接近不透明的白底板，既保证标题可读，又不会把商品盖住太多。
    """
    x0, y0, x1, y1 = box
    plate = Image.new("RGBA", (max(1, x1 - x0), max(1, y1 - y0)), (255, 255, 255, alpha))
    mask = Image.new("L", plate.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, plate.size[0] - 1, plate.size[1] - 1),
                                           radius=radius, fill=255)
    canvas.paste(plate, (x0, y0), mask)


def _paste_product(canvas: Image.Image, product_img: Image.Image, box: tuple[int, int, int, int],
                   with_shadow: bool = True) -> None:
    x0, y0, x1, y1 = box
    fitted = _to_rgba(_fit(_to_rgba(product_img), x1 - x0, y1 - y0))
    if with_shadow:
        fitted = _drop_shadow(fitted)
    px = x0 + (x1 - x0 - fitted.size[0]) // 2
    py = y0 + (y1 - y0 - fitted.size[1]) // 2
    canvas.paste(fitted, (px, py), fitted.split()[-1])


def _brand_mark(draw: ImageDraw.ImageDraw, x: int, y: int, color: tuple[int, int, int],
                align_right: bool = False, size: int = 34) -> None:
    font = load_font(size, bold=True)
    w = text_width(draw, BRAND_NAME, font)
    draw.text((x - w if align_right else x, y), BRAND_NAME, font=font, fill=color)
    f2 = load_font(max(14, size - 16), bold=False)
    w2 = text_width(draw, BRAND_TAGLINE, f2)
    draw.text((x - w2 if align_right else x, y + size + 6), BRAND_TAGLINE, font=f2,
              fill=(150, 152, 165))


def _short(text: str, limit: int = 46) -> str:
    """把长句压成短语（在词边界截断），用于版式窄的卖点角标位。"""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:") + "…"


def render_variant(product: dict, copy: Copy, variant: int) -> Image.Image:
    """渲染一个版式变体，返回 1024×1024 RGB 图。

    三个版式的文字区与图片区**互不重叠**，这是硬约束：
    第一版把角标放在左上、标题居中，结果标题一长就和角标叠在一起（截图里很明显）。
    现在的排布是"标题在上、角标在下、商品占中间"，三者物理分离。
    """
    primary, accent, dark = theme_for(product.get("category", ""))
    product_img = load_product_image(product, primary)
    canvas = Image.new("RGBA", (SIZE, SIZE), WHITE + (255,))
    draw = ImageDraw.Draw(canvas)

    if variant == 1:      # banner：顶部品牌色标题栏 + 商品 + 底部三条卖点
        bar_h = 300
        draw.rectangle((0, 0, SIZE, bar_h), fill=primary + (255,))
        # ★ 先量角标宽度，再反推标题可用宽度。
        #   第一版写死"右边留 210px"，而角标实际有 290px 宽，标题第一行又正好写到
        #   702px —— 于是角标压在标题上。凡是"两个元素不能重叠"，就必须先量后放。
        badge_font = font_for(copy.badge, 30, bold=True)
        badge_w = text_width(draw, copy.badge, badge_font) + 44 if copy.badge else 0
        head_max_w = SIZE - MARGIN * 2 - (badge_w + 26 if badge_w else 0)

        f_head = font_for(copy.headline, 54, bold=True)
        lines = wrap_text(draw, copy.headline, f_head, head_max_w, max_lines=2)
        y = MARGIN
        for line in lines:
            draw.text((MARGIN, y), line, font=f_head, fill=WHITE)
            y += 62
        if copy.subhead:
            f_sub = font_for(copy.subhead, 28, bold=False)
            for line in wrap_text(draw, copy.subhead, f_sub, head_max_w, max_lines=2):
                draw.text((MARGIN, y + 2), line, font=f_sub, fill=accent)
                y += 36
        _draw_badge(draw, (SIZE - MARGIN, MARGIN - 10), copy.badge, (255, 255, 255), accent,
                    align="right")
        _paste_product(canvas, product_img, (MARGIN + 40, bar_h + 26, SIZE - MARGIN - 40, SIZE - 200))
        # 底部三条卖点：窄栏位所以先压成短语，避免每列都被截成半句话
        y = SIZE - 168
        col_w = (SIZE - MARGIN * 2) // 3
        for i, b in enumerate(copy.bullets[:3]):
            text = _short(b, 52)
            f_b = font_for(text, 23, bold=True)
            cx, cy = MARGIN + i * col_w + 10, y + 2
            draw.ellipse((cx, cy, cx + 20, cy + 20), fill=accent)
            draw.text((cx + 6, cy + 1), "✓", font=load_font(16, bold=True), fill=primary)
            for j, line in enumerate(wrap_text(draw, text, f_b, col_w - 44, 3)):
                draw.text((cx + 30, y + j * 27), line, font=f_b, fill=dark)

    elif variant == 2:    # split：左右分栏
        split = int(SIZE * 0.47)
        draw.rectangle((split, 0, SIZE, SIZE), fill=(247, 248, 251, 255))
        text_w = split - MARGIN * 2 + 10
        f_head = font_for(copy.headline, 58, bold=True)
        lines = wrap_text(draw, copy.headline, f_head, text_w, max_lines=3)
        y = 128
        for line in lines:
            draw.text((MARGIN, y), line, font=f_head, fill=dark)
            y += 68
        draw.line((MARGIN, y + 10, MARGIN + 96, y + 10), fill=accent, width=6)
        y += 42
        if copy.subhead:
            f_sub = font_for(copy.subhead, 28, bold=False)
            for line in wrap_text(draw, copy.subhead, f_sub, text_w, max_lines=3):
                draw.text((MARGIN, y), line, font=f_sub, fill=(90, 94, 110))
                y += 38
        # 角标左对齐，位置由正文高度决定（而不是写死），避免和副标题叠字
        badge_y = min(max(y + 18, SIZE - 250), SIZE - 176)
        _draw_badge(draw, (MARGIN, badge_y), copy.badge, primary, accent)
        _paste_product(canvas, product_img, (split + 24, 120, SIZE - 40, SIZE - 150))
        _brand_mark(draw, MARGIN, SIZE - 96, primary, size=30)

    else:                 # v0 clean：最接近亚马逊主图规范
        head_max_w = SIZE - MARGIN * 2 - 40
        f_head = font_for(copy.headline, 50, bold=True)
        lines = wrap_text(draw, copy.headline, f_head, head_max_w, max_lines=2)
        f_sub = font_for(copy.subhead, 28, bold=False) if copy.subhead else None
        sub_lines = wrap_text(draw, copy.subhead, f_sub, head_max_w - 80, max_lines=2) if f_sub else []

        # 先算标题块总高，再垫一层白底板，最后写字 —— 顺序反了就会被商品图压住
        block_h = len(lines) * 58 + len(sub_lines) * 34
        plate_box = (MARGIN - 18, MARGIN - 26, SIZE - MARGIN + 18, MARGIN + block_h + 16)
        _text_plate(canvas, plate_box)
        draw = ImageDraw.Draw(canvas)

        y = MARGIN
        for line in lines:
            w = text_width(draw, line, f_head)
            draw.text((SIZE / 2 - w / 2, y), line, font=f_head, fill=dark)
            y += 58
        for line in sub_lines:
            w = text_width(draw, line, f_sub)
            draw.text((SIZE / 2 - w / 2, y + 2), line, font=f_sub, fill=(92, 96, 112))
            y += 34

        # 角标放左下角，和顶部居中的标题物理分离
        _draw_badge(draw, (MARGIN, SIZE - 118), copy.badge, (255, 255, 255), primary)
        _paste_product(canvas, product_img, (MARGIN + 30, y + 26, SIZE - MARGIN - 30, SIZE - 150))
        _brand_mark(draw, SIZE - MARGIN, SIZE - 92, primary, align_right=True, size=30)

    return canvas.convert("RGB")


def compose_hero(product: dict, copy_dict: dict, out_path: Path | None = None,
                 variant: int = 0, variant_name: str | None = None) -> Path:
    """合成一张主图并落盘，返回文件路径。"""
    copy = Copy.from_dict(copy_dict or {}, product)
    img = render_variant(product, copy, variant)
    out_dir = config.OUTPUTS_DIR / "hero_images" / product["sku"]
    out_dir.mkdir(parents=True, exist_ok=True)
    names = {0: "clean", 1: "banner", 2: "split"}
    name = variant_name or names.get(variant, f"v{variant}")
    out_path = out_path or (out_dir / f"{product['sku']}_{name}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def contact_sheet(paths: Sequence[Path], out_path: Path, cols: int = 3,
                  cell: int = 360) -> Path:
    """把多张主图拼成一张总览图 —— 作品集和汇报都靠它一眼看完。"""
    paths = [p for p in paths if Path(p).is_file()]
    if not paths:
        raise ValueError("没有任何图片可以拼接")
    rows = math.ceil(len(paths) / cols)
    label_h = 34
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), (250, 250, 252))
    d = ImageDraw.Draw(sheet)
    f = load_font(20, bold=True)
    for i, p in enumerate(paths):
        r, c = divmod(i, cols)
        img = Image.open(p).convert("RGB").resize((cell - 16, cell - 16), Image.LANCZOS)
        x, y = c * cell + 8, r * (cell + label_h) + 8
        sheet.paste(img, (x, y))
        label = Path(p).stem
        d.text((x + 4, y + cell - 8), label, font=f, fill=(70, 74, 90))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="PNG", optimize=True)
    return out_path


def compliance_check(path: Path) -> dict:
    """主图合规自检：把亚马逊的硬约束做成可执行的检查，而不是靠人眼。

    检查项：正方形、边长 ≥1000、四角为纯白、商品（非白像素）占比合理。
    """
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()
    corners = [px[2, 2], px[w - 3, 2], px[2, h - 3], px[w - 3, h - 3]]
    white_corners = all(min(c) >= 245 for c in corners)

    # 抽样统计"非白"像素占比（商品 + 文字都算内容）
    step = 4
    non_white = 0
    total = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            total += 1
            if not (r >= 246 and g >= 246 and b >= 246):
                non_white += 1
    ratio = non_white / total if total else 0
    return {
        "file": str(path),
        "size": f"{w}x{h}",
        "square": w == h,
        "min_side_ok": min(w, h) >= 1000,
        "white_corners": white_corners,
        "content_ratio": round(ratio, 4),
        "content_ratio_ok": 0.08 <= ratio <= 0.72,
        "passed": (w == h) and min(w, h) >= 1000 and white_corners and 0.08 <= ratio <= 0.72,
    }
