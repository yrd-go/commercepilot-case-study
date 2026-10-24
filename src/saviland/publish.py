# -*- coding: utf-8 -*-
"""把运行产物发布到 `docs/images/`，供 README 与作品集文档引用。

为什么需要单独一步
------------------
`outputs/` 是**每次运行都会被覆盖**的工作目录，而文档里引用的图片路径必须稳定。
所以要有一次显式的"发布"：把选定的产物复制成固定文件名放进 `docs/images/`。

放在包里（而不是留在 tools/ 脚本里）的原因：`run.py portfolio` 需要调用它 ——
一键流程的最后一步就是"把这次跑出来的东西发布成文档配图"，
不能让用户再手动跑第二个脚本。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config
from .domain import ads as ads_domain

DOC_IMAGES = config.DOCS_DIR / "images"


def _copy(src: Path, dst_name: str, lines: list[str]) -> str | None:
    if not src.is_file():
        lines.append(f"  [跳过] 找不到 {src.name}")
        return None
    dst = DOC_IMAGES / dst_name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    lines.append(f"  [发布] {src.name:34} → docs/images/{dst_name}  "
                 f"({dst.stat().st_size // 1024} KB)")
    return str(dst.relative_to(config.ROOT)).replace("\\", "/")


def publish_docs_images(echo: bool = True) -> dict:
    """发布主图总览、周报图表、样例报告与样例文案。返回发布结果摘要。"""
    lines: list[str] = ["发布主图与图表到 docs/images …"]
    out: dict[str, str | None] = {}

    hero_dir = config.OUTPUTS_DIR / "hero_images"
    chart_dir = config.OUTPUTS_DIR / "weekly_charts"

    out["hero_contact_sheet"] = _copy(hero_dir / "contact_sheet_v0.png",
                                      "hero_contact_sheet.png", lines)
    out["hero_variant_banner"] = _copy(hero_dir / "contact_sheet_v1.png",
                                       "hero_variant_banner.png", lines)
    out["hero_variant_split"] = _copy(hero_dir / "contact_sheet_v2.png",
                                      "hero_variant_split.png", lines)

    # 单张 split 版式（README 正文里展示版式质量）
    for sku in ("SV-UVL-48W", "SV-GEL-CAT", "SV-TOOL-DRILL"):
        p = hero_dir / sku / f"{sku}_split.png"
        if p.is_file():
            out["hero_single_split"] = _copy(p, f"hero_{sku}_split.png", lines)
            break

    for name in ("daily_trend", "acos_by_campaign", "wow_compare",
                 "spend_structure", "ctr_cvr_quadrant"):
        out[f"chart_{name}"] = _copy(chart_dir / f"{name}.png",
                                     f"weekly_{name}.png", lines)

    out["product_contact_sheet"] = _copy(config.ASSETS_DIR / "_CONTACT_SHEET.png",
                                         "product_contact_sheet.png", lines)

    # 真实报告样例（HTML 自包含，双击即看）+ 文案样例
    reports = sorted((config.OUTPUTS_DIR / "weekly_reports").glob("weekly_*.html"))
    if reports:
        dst = DOC_IMAGES / "sample_weekly_report.html"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(reports[-1], dst)
        lines.append(f"  [发布] {reports[-1].name} → docs/images/sample_weekly_report.html")
        out["sample_report"] = str(dst.relative_to(config.ROOT)).replace("\\", "/")

    # 文案样例：**优先用"全部 SKU 最新版"**，而不是"最近一次批量"。
    #   批量通常只覆盖几个 SKU，拿它当作品集样例会让人以为只做了 4 个。
    csv_path = config.OUTPUTS_DIR / "listing" / "all_copies.csv"
    if not csv_path.is_file():
        csv_path = config.OUTPUTS_DIR / "listing" / "batch_latest.csv"
    if csv_path.is_file():
        dst = DOC_IMAGES / "sample_listing_copy.csv"
        shutil.copyfile(csv_path, dst)
        lines.append(f"  [发布] {csv_path.name} → docs/images/sample_listing_copy.csv")
        out["sample_csv"] = str(dst.relative_to(config.ROOT)).replace("\\", "/")

    lines.append(f"\n共发布到 {DOC_IMAGES}")
    if echo:
        print("\n".join(lines))
    return {"lines": lines, "files": out}


def hero_sheets_for_all(variants: int = 3, echo: bool = True) -> list[Path]:
    """为**全部已有主图的 SKU** 重出总览图。

    ★ 这里有个容易踩的坑：一键流程里只对"需要重写的 SKU"跑了链路 1，
    如果总览图也只拿这几个 SKU 去拼，就会把之前 20 个 SKU 的完整总览**覆盖成 5 个**——
    产物看起来"生成成功"，实际是回退。
    总览图必须覆盖所有已有主图的 SKU，而不是这一轮新生成的那几个。
    """
    from .pipelines import listing as listing_pipe
    from . import db

    all_skus = [r["sku"] for r in db.query(
        "SELECT DISTINCT sku FROM listing_copies ORDER BY sku")]
    paths: list[Path] = []
    for v in range(max(1, min(3, variants))):
        p = listing_pipe.contact_sheet_for(all_skus, variant=v,
                                          out_name=f"contact_sheet_v{v}.png")
        if p:
            paths.append(p)
            if echo:
                print(f"  总览图 v{v}（{len(all_skus)} 个 SKU）：{p}")
    return paths
