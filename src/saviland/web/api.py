# -*- coding: utf-8 -*-
"""API 数据装配层：纯函数，输入参数、输出可直接 JSON 化的 dict。

三条设计约束
------------
1. **不碰 HTTP**。这里不出现 Request/Response，所以每条都能在测试里直接调，
   不用起服务。`server.py` 只负责把 URL 映射到这些函数。
2. **JSON 安全**。`available_days` 在"没有动销"时是 `float('inf')` ——
   直接塞给响应会产出 `Infinity`，前端 `JSON.parse` 会直接抛错。
   所有数值都过一遍 `_n()` 把 inf/nan 变成 null。
3. **不向用户暴露内部实现**。引擎名、prompt 字符数这类东西只在「运行档案」
   这一个诊断视图里出现，业务页面上没有 —— 看报表的人只关心业务结论。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from .. import config, db, rag_bridge, runs, seed as seed_mod
from ..domain import ads as ads_domain
from ..domain import catalog as catalog_domain
from ..domain import inventory as inv_domain
from ..domain import orders as orders_domain
from ..pipelines import compliance, listing as listing_pipe, weekly as weekly_pipe

SEVERITY_LABEL = {"critical": "严重", "warning": "预警", "opportunity": "机会", "info": "提示"}
LEVEL_LABEL = {"stockout_risk": "断货风险", "overstock": "压货", "healthy": "健康", "no_sales": "无动销"}
FEEDS_LABEL = {"listing": "内容侧", "ads": "投放侧", "inventory": "供应链"}

_FRESH_CHECKED = False


def _ensure_fresh_once() -> None:
    """进程内只做一次"数据保鲜"检查（真正的锁在 `seed.ensure_fresh_once`）。

    演示数据以"今天"为锚点生成，放几天就会过期（见 `seed.ensure_fresh`）。
    放在服务第一次被访问时检查，而不是启动时：启动阶段应该尽量快，
    而这个检查只多两条 `MAX(date)` 查询。

    ★ 互斥必须放在 `seed` 里，不能只靠本模块的布尔开关：
    启动预热线程也会触发同一个检查，两处各自有一份状态挡不住并发 ——
    实测会让 1500 条订单被重灌两遍，首屏从 4 秒变成 31 秒。
    """
    global _FRESH_CHECKED
    if _FRESH_CHECKED:
        return
    _FRESH_CHECKED = True
    try:
        info = seed_mod.ensure_fresh_once()
        if info.get("action") == "reseeded":
            print(f"[数据保鲜] {info.get('reason')}")
    except Exception as e:  # noqa: BLE001 - 保鲜失败不该让页面打不开
        print(f"[数据保鲜] 跳过（{type(e).__name__}: {e}）")


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _n(value: Any, digits: int | None = None) -> Any:
    """把数值变成 JSON 安全的形态：inf/nan → None，浮点按位数四舍五入。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
            return None
        if digits is not None and isinstance(value, float):
            return round(value, digits)
        return value
    return value


def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _delta(cur: float | None, prev: float | None) -> dict:
    """环比：没有基准就不编造百分比。"""
    if cur is None or prev is None:
        return {"pct": None, "diff": None}
    if prev == 0:
        return {"pct": None, "diff": _n(cur - prev, 4)}
    return {"pct": _n((cur - prev) / prev, 4), "diff": _n(cur - prev, 4)}


def _media_product(name: str) -> str:
    return f"/media/product/{name}" if name else ""


def _hero_images(sku: str) -> list[dict]:
    d = config.OUTPUTS_DIR / "hero_images" / sku
    if not d.is_dir():
        return []
    out = []
    names = {"clean": "主图规范版", "banner": "副图 / A+ 版", "split": "详情页首屏版"}
    for p in sorted(d.glob("*.png")):
        variant = next((k for k in names if k in p.stem), "")
        out.append({
            "name": p.name,
            "variant": variant,
            "label": names.get(variant, p.stem),
            "url": f"/media/hero/{sku}/{p.name}",
            "kb": round(p.stat().st_size / 1024, 1),
        })
    return out


# --------------------------------------------------------------------------
# 概览
# --------------------------------------------------------------------------
def meta() -> dict:
    """界面上的"关于"信息。刻意不含模型/引擎名 —— 这些是内部实现。

    ★ 这里**不能**等向量库加载完（那是 4~13 秒）。前端拿到 /api/meta 才开始
    渲染，一等就是十几秒白屏。所以只读 `stats()`（瞬时），并用 `warming`
    告诉前端"索引还在后台加载"，前端负责稍后补上"知识片段"这个数字。
    """
    _ensure_fresh_once()
    counts = db.table_counts()
    info = rag_bridge.stats()
    return {
        "brand": "Saviland",
        "tagline": "电商运营台 · 内容与投放自动化",
        "version": "1.1.0",
        "counts": {
            "products": counts.get("products", 0),
            "orders": counts.get("orders", 0),
            "ad_daily": counts.get("ad_daily", 0),
            "copies": counts.get("listing_copies", 0),
            "runs": counts.get("generation_runs", 0),
        },
        "knowledge": {
            "docs": info["kb_files"],
            "chunks": info["chunks"],
            "ready": info["ready"],
            "warming": not info["ready"],     # 索引还在后台加载 → 前端稍后补
        },
        "report_week": ads_domain.default_report_week(),
        "weeks": ads_domain.available_weeks(),
        "has_data": counts.get("products", 0) > 0,
    }


def overview() -> dict:
    _ensure_fresh_once()
    counts = db.table_counts()
    if not counts.get("products"):
        return {"empty": True, "counts": counts}

    week = ads_domain.default_report_week()
    metrics = ads_domain.weekly_metrics(week)
    findings = ads_domain.diagnose(metrics)
    diag = ads_domain.diagnostics_summary(findings)
    total = metrics["total"]
    o30 = orders_domain.sales_summary(30)
    advice = inv_domain.restock_advice()
    levels: dict[str, int] = {}
    for a in advice:
        levels[a["level"]] = levels.get(a["level"], 0) + 1

    return {
        "empty": False,
        "kpis": {
            "products": counts.get("products", 0),
            "categories": len(catalog_domain.categories()),
            "revenue30": _n(o30["revenue"], 2),
            "gross_profit30": _n(o30["gross_profit"], 2),
            "gross_margin": _n(o30["gross_margin"], 4),
            "refund_rate": _n(orders_domain.refund_rate(30)["rate"], 4),
            "acos": _n(total["acos"], 4),
            "spend": _n(total["spend"], 2),
            "sales": _n(total["sales"], 2),
            "roas": _n(total["roas"], 2),
            "target_acos": metrics["target_acos"],
            "stockout": levels.get("stockout_risk", 0),
            "overstock": levels.get("overstock", 0),
            "healthy": levels.get("healthy", 0),
            "skus": len(advice),
            "copies": counts.get("listing_copies", 0),
            "hero_images": sum(1 for _ in (config.OUTPUTS_DIR / "hero_images").rglob("*.png")),
            "findings": diag["total"],
            "waste": _n(diag["waste_usd"], 2),
        },
        "week": metrics["week_label"],
        "week_start": metrics["week_start"],
        "wow": {k: _delta(total.get(k), metrics["prev_total"].get(k))
                for k in ("spend", "sales", "orders", "clicks")},
        "severity": diag["by_severity"],
        "feeds": {"listing": diag["listing_actions"], "ads": diag["ads_actions"]},
        "top_findings": [_finding_view(f, i) for i, f in enumerate(findings[:4], 1)],
        "daily": [{"date": d["date"], "spend": _n(d["spend"], 2), "sales": _n(d["sales"], 2),
                   "orders": d["orders"]} for d in metrics["daily"]],
        "channels": [{"channel": c["channel"], "revenue": _n(c["revenue"], 2),
                      "units": c["units"], "share": None}
                     for c in orders_domain.by_channel(30)],
    }


def _finding_view(f: dict, index: int) -> dict:
    return {
        "index": index,
        "rule": f["rule"],
        "severity": f["severity"],
        "severity_label": SEVERITY_LABEL.get(f["severity"], f["severity"]),
        "scope": f["scope"],
        "subject": f["subject"],
        "subject_name": f["subject_name"],
        "diagnosis": f["diagnosis"],
        "action": f["action"],
        "feeds": f["feeds"],
        "feeds_label": FEEDS_LABEL.get(f["feeds"], f["feeds"]),
        "evidence": f["evidence"],
        "impact_usd": _n(f.get("impact_usd"), 2),
    }


# --------------------------------------------------------------------------
# 商品
# --------------------------------------------------------------------------
def products() -> dict:
    rows = catalog_domain.list_products()
    out = []
    for p in rows:
        out.append({
            "sku": p["sku"],
            "title": p["title_cn"],
            "title_en": p.get("title_en") or "",
            "category": p["category"],
            "sub_category": p.get("sub_category") or "",
            "price": _n(p["price_usd"], 2),
            "cost": _n(p["cost_usd"], 2),
            "margin": _n(catalog_domain.margin_of(p), 4),
            "stock": p["stock"],
            "rating": _n(p.get("rating"), 2),
            "reviews": p.get("review_count"),
            "evidence": p.get("evidence") or "",
            "image": _media_product(p.get("hero_image", "").replace("assets/products/", "").split("/")[-1]),
            "has_copy": catalog_domain.latest_listing_copy(p["sku"]) is not None,
        })
    return {"items": out, "categories": catalog_domain.categories(), "total": len(out)}


def product_detail(sku: str) -> dict | None:
    p = catalog_domain.get_product(sku)
    if not p:
        return None
    copies = catalog_domain.list_listing_copies(sku, limit=20)
    latest = copies[0] if copies else None
    return {
        "sku": p["sku"],
        "title": p["title_cn"],
        "title_en": p.get("title_en") or "",
        "category": p["category"],
        "sub_category": p.get("sub_category") or "",
        "price": _n(p["price_usd"], 2),
        "cost": _n(p["cost_usd"], 2),
        "margin": _n(catalog_domain.margin_of(p), 4),
        "rating": _n(p.get("rating"), 2),
        "reviews": p.get("review_count"),
        "stock": p["stock"],
        "audience": p.get("target_audience") or "",
        "evidence": p.get("evidence") or "",
        "source_url": p.get("source_url") or "",
        "image": _media_product(p.get("hero_image", "").replace("assets/products/", "").split("/")[-1]),
        "specs": p.get("specs") or {},
        "selling_points": p.get("selling_points") or [],
        "five_bullets": p.get("five_bullets") or [],
        "pain_points": p.get("pain_points") or [],
        "scenarios": p.get("use_scenarios") or [],
        "keywords": p.get("keywords") or [],
        "competitors": p.get("competitors") or [],
        "hero_images": _hero_images(sku),
        "copies": [{"version": c["version"], "created_at": c["created_at"],
                    "headline": c["hero_headline"] or "", "title": c["title"] or ""}
                   for c in copies],
        "latest_copy": _copy_view(latest) if latest else None,
    }


def _copy_view(c: dict) -> dict:
    return {
        "version": c["version"],
        "title": c.get("title") or "",
        "headline": c.get("hero_headline") or "",
        "subhead": c.get("hero_subhead") or "",
        "badge": c.get("hero_badge") or "",
        "cta": c.get("cta") or "",
        "bullets": c.get("bullets") or [],
        "description": c.get("description") or "",
        "keywords": c.get("keywords") or [],
        "created_at": c.get("created_at") or "",
    }


def copies() -> dict:
    """全部 SKU 的最新文案 —— 「文案」页用。"""
    items = []
    for p in catalog_domain.list_products():
        c = catalog_domain.latest_listing_copy(p["sku"])
        if not c:
            continue
        items.append({"sku": p["sku"], "title": p["title_cn"], "category": p["category"],
                      "image": _media_product(
                          (p.get("hero_image") or "").replace("assets/products/", "").split("/")[-1]),
                      "copy": _copy_view(c),
                      "hero_images": _hero_images(p["sku"])})
    return {"items": items, "total": len(items)}


# --------------------------------------------------------------------------
# 库存
# --------------------------------------------------------------------------
def inventory() -> dict:
    advice = inv_domain.restock_advice()
    items = []
    for a in advice:
        items.append({
            "sku": a["sku"],
            "title": a["title_cn"],
            "category": a["category"],
            "level": a["level"],
            "level_label": LEVEL_LABEL.get(a["level"], a["level"]),
            "available": a["available"],
            "on_hand": a["on_hand"],
            "velocity": _n(a["velocity"], 2),
            "days": _n(a["available_days"], 1),
            "lead_time": a["lead_time_days"],
            "suggest": a["suggest_qty"],
        })
    levels: dict[str, int] = {}
    for i in items:
        levels[i["level"]] = levels.get(i["level"], 0) + 1
    warehouses = {}
    for r in inv_domain.list_inventory():
        warehouses.setdefault(r["sku"], []).append({
            "warehouse": r["warehouse"], "on_hand": r["on_hand"],
            "reserved": r["reserved"], "safety": r["safety_stock"],
            "lead_time": r["lead_time_days"],
        })
    return {"items": items, "levels": levels,
            "thresholds": {"stockout": config.Biz.STOCKOUT_WARN_DAYS,
                           "overstock": config.Biz.OVERSTOCK_DAYS,
                           "lookback": 14},
            "warehouses": warehouses}


# --------------------------------------------------------------------------
# 订单
# --------------------------------------------------------------------------
def orders(days: int = 30) -> dict:
    s = orders_domain.sales_summary(days)
    rr = orders_domain.refund_rate(days)
    return {
        "days": days,
        "summary": {
            "revenue": _n(s["revenue"], 2),
            "units": s["units"],
            "gross_profit": _n(s["gross_profit"], 2),
            "gross_margin": _n(s["gross_margin"], 4),
            "cogs": _n(s["cogs"], 2),
            "refund_units": s["refund_units"],
            "refund_rate": _n(rr["rate"], 4),
            "order_rows": s["order_rows"],
        },
        "by_channel": [{"channel": c["channel"], "revenue": _n(c["revenue"], 2),
                        "units": c["units"], "gross_profit": _n(c["gross_profit"], 2)}
                       for c in orders_domain.by_channel(days)],
        "by_category": [{"category": c["category"], "revenue": _n(c["revenue"], 2),
                         "units": c["units"]} for c in orders_domain.by_category(days)],
        "top_products": [{"sku": p["sku"], "title": p["title_cn"], "category": p["category"],
                          "units": p["units"], "revenue": _n(p["revenue"], 2),
                          "gross_profit": _n(p["gross_profit"], 2)}
                         for p in orders_domain.top_products(days, 10)],
        "daily": [{"date": d["date"], "units": d["units"], "revenue": _n(d["revenue"], 2)}
                  for d in orders_domain.daily_series(days)],
    }


# --------------------------------------------------------------------------
# 广告 / 诊断
# --------------------------------------------------------------------------
def ads(week: str | None = None) -> dict:
    week = week or ads_domain.default_report_week()
    m = ads_domain.weekly_metrics(week)
    t = m["total"]
    return {
        "week": m["week_label"],
        "week_start": m["week_start"],
        "week_end": m["week_end"],
        "prev_week_start": m["prev_week_start"],
        "prev_week_end": m["prev_week_end"],
        "weeks": ads_domain.available_weeks(),
        "target_acos": m["target_acos"],
        "target_roas": m["target_roas"],
        "total": {k: _n(v, 4) for k, v in t.items()},
        "prev_total": {k: _n(v, 4) for k, v in m["prev_total"].items()},
        "wow": {k: _delta(t.get(k), m["prev_total"].get(k)) for k in m["wow"]},
        "by_campaign": [{
            "id": c["campaign_id"], "name": c["name"], "sku": c["sku"],
            "type": f"{c['ad_type']}/{c['targeting']}",
            "impressions": c["impressions"], "clicks": c["clicks"],
            "ctr": _n(c["ctr"], 5), "spend": _n(c["spend"], 2),
            "orders": c["orders"], "sales": _n(c["sales"], 2),
            "cvr": _n(c["cvr"], 5), "cpc": _n(c["cpc"], 3),
            "acos": _n(c["acos"], 4), "roas": _n(c["roas"], 2),
            "budget": _n(c["daily_budget_usd"], 2),
            "budget_use": _n(c["budget_utilization"], 4),
        } for c in m["by_campaign"]],
        "by_sku": [{"sku": s["sku"], "title": s.get("title_cn") or "", "category": s.get("category") or "",
                    "impressions": s["impressions"], "clicks": s["clicks"], "spend": _n(s["spend"], 2),
                    "orders": s["orders"], "sales": _n(s["sales"], 2), "acos": _n(s["acos"], 4),
                    "cvr": _n(s["cvr"], 5)} for s in m["by_sku"]],
        "daily": [{"date": d["date"], "impressions": d["impressions"], "clicks": d["clicks"],
                   "spend": _n(d["spend"], 2), "orders": d["orders"],
                   "sales": _n(d["sales"], 2), "acos": _n(d["acos"], 4),
                   "ctr": _n(d["ctr"], 5)} for d in m["daily"]],
    }


def findings(week: str | None = None) -> dict:
    week = week or ads_domain.default_report_week()
    m = ads_domain.weekly_metrics(week)
    fs = ads_domain.diagnose(m)
    diag = ads_domain.diagnostics_summary(fs)
    targets = weekly_pipe.listing_targets(fs)
    return {
        "week": m["week_label"],
        "week_start": m["week_start"],
        "items": [_finding_view(f, i) for i, f in enumerate(fs, 1)],
        "summary": {
            "total": diag["total"],
            "by_severity": diag["by_severity"],
            "waste_usd": _n(diag["waste_usd"], 2),
            "listing_actions": diag["listing_actions"],
            "ads_actions": diag["ads_actions"],
        },
        "listing_targets": targets,
        "targets_detail": [
            {"sku": s, "title": (catalog_domain.get_product(s) or {}).get("title_cn", "")}
            for s in targets
        ],
    }


# --------------------------------------------------------------------------
# 周报产物
# --------------------------------------------------------------------------
def report(week: str | None = None) -> dict:
    week = week or ads_domain.default_report_week()
    label = ads_domain.week_label(week)
    out_dir = config.OUTPUTS_DIR / "weekly_reports"
    md = out_dir / f"weekly_{label}.md"
    xlsx = out_dir / f"weekly_{label}.xlsx"
    html = out_dir / f"weekly_{label}.html"
    charts_dir = config.OUTPUTS_DIR / "weekly_charts"
    charts = []
    names = {"daily_trend": "日趋势：花费与销售", "acos_by_campaign": "各活动 ACOS 排名",
             "wow_compare": "本周 vs 上周", "spend_structure": "花费结构",
             "ctr_cvr_quadrant": "CTR × CVR 四象限分诊"}
    for key, label_cn in names.items():
        p = charts_dir / f"{key}.png"
        if p.is_file():
            charts.append({"key": key, "label": label_cn, "url": f"/media/chart/{p.name}"})
    return {
        "week": label,
        "week_start": week,
        "exists": md.is_file(),
        "downloads": {
            "html": f"/files/report/{label}/html" if html.is_file() else None,
            "md": f"/files/report/{label}/md" if md.is_file() else None,
            "xlsx": f"/files/report/{label}/xlsx" if xlsx.is_file() else None,
        },
        "meta": _report_meta(md),
        "charts": charts,
    }


def _report_meta(md_path) -> dict:
    """从周报 Markdown 里抽出生成时间（正脸只显示"什么时候出的"，不含引擎）。"""
    if not md_path.is_file():
        return {}
    try:
        head = md_path.read_text(encoding="utf-8").splitlines()[:8]
    except OSError:
        return {}
    for line in head:
        if line.startswith("> ") and "生成时间" in line:
            for part in line.lstrip("> ").split("｜"):
                part = part.strip()
                if part.startswith("生成时间"):
                    return {"generated_at": part.split("：", 1)[-1].strip()}
    return {}


def report_markdown(week: str) -> str:
    label = ads_domain.week_label(week) if not week.startswith("20") or "-W" not in week else week
    p = config.OUTPUTS_DIR / "weekly_reports" / f"weekly_{label}.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


# --------------------------------------------------------------------------
# 运行档案（唯一保留内部实现细节的地方）
# --------------------------------------------------------------------------
def runs_view(limit: int = 40) -> dict:
    rows = runs.recent_runs(None, limit=limit)
    return {
        "items": [{
            "run_id": r["run_id"], "pipeline": r["pipeline"], "target": r["target"],
            "engine": r["engine"] or "", "status": r["status"],
            "rag_hits": r["rag_hits"], "latency_ms": r["latency_ms"],
            "note": r["note"] or "", "created_at": r["created_at"],
        } for r in rows],
        "engine_usage": [{"engine": u["engine"], "runs": u["n"],
                          "avg_ms": u["avg_latency_ms"] or 0,
                          "rag_hits": u["total_rag_hits"]} for u in runs.engine_usage()],
        "by_status": runs.run_counts(),
        "artifact_kinds": [{"kind": a["kind"], "count": a["n"]} for a in db.query(
            "SELECT kind, COUNT(*) AS n FROM artifacts GROUP BY kind ORDER BY n DESC")],
        "current_jobs": __import__("saviland.web.jobs", fromlist=["recent"]).recent(8),
    }


def artifacts(run_id: str) -> dict:
    arts = runs.artifacts_of(run_id)
    out = []
    for a in arts:
        p = a["path"]
        url = None
        try:
            rel = str(__import__("pathlib").Path(p).resolve().relative_to(config.ROOT.resolve()))
            url = "/media/file/" + rel.replace("\\", "/")
        except Exception:  # noqa: BLE001
            url = None
        out.append({"kind": a["kind"], "path": p, "meta": a["meta"], "url": url})
    return {"items": out}


# --------------------------------------------------------------------------
# RAG
# --------------------------------------------------------------------------
def rag_search(q: str, k: int = 5, rewrite: bool = False) -> dict:
    res = rag_bridge.search(q, k=k, rewrite=rewrite)
    return {
        "query": q,
        "mode": res.mode,
        "rewritten": res.rewritten,
        "keywords": res.keywords,
        "note": res.note,
        "latency_ms": res.latency_ms,
        "pool_size": res.pool_size,
        "hits": [{
            "score": _n(h.score, 4), "vec": _n(h.vec_sim, 4), "kw": _n(h.kw_score, 4),
            "source": h.source, "line_start": h.line_start, "line_end": h.line_end,
            "matched": h.matched, "content": h.content,
        } for h in res.hits],
    }


def rag_status() -> dict:
    info = rag_bridge.ensure_loaded()
    return {"chunks": info["chunks"], "docs": info["kb_files"],
            "chars": info["kb_chars"], "ready": info["ready"],
            "error": info["error"], "collection": info["collection"]}


def rag_queries_hint() -> dict:
    return {"samples": [
        "美甲灯这个类目怎么写卖点",
        "哪些词不能用，会被平台下架",
        "SV-UVL-48W 的规格和竞品差异",
        "买家常抱怨什么问题",
        "美甲灯的功率和波长怎么讲清楚",
    ]}


# --------------------------------------------------------------------------
# 生成任务（供 jobs 调用）
# --------------------------------------------------------------------------
def run_listing_batch(job, skus: list[str], variants: int = 3, engine: str | None = None) -> dict:
    from . import jobs as jobs_mod
    results = []
    total = len(skus)
    for i, sku in enumerate(skus, 1):
        jobs_mod.step(job, i - 1, total, f"[{i}/{total}] {sku} 正在检索品牌知识并生成文案…")
        try:
            res = listing_pipe.generate_one(sku, variants=variants, engine=engine)
            imgs = len(res.hero_images)
            jobs_mod.step(job, i, total,
                          f"[{i}/{total}] {sku} 完成（主图 {imgs} 张，合规检查 {len(res.violations)} 处）")
            results.append({"sku": sku, "ok": True, "violations": len(res.violations),
                            "hero_images": imgs})
        except Exception as e:  # noqa: BLE001
            jobs_mod.step(job, i, total, f"[{i}/{total}] {sku} 失败：{type(e).__name__}")
            results.append({"sku": sku, "ok": False, "error": str(e)})
    ok = sum(1 for r in results if r["ok"])
    return {"requested": total, "succeeded": ok, "failed": total - ok,
            "items": results, "skus": skus}


def run_weekly(job, week: str, engine: str | None = None) -> dict:
    from . import jobs as jobs_mod
    jobs_mod.step(job, 0, 3, "聚合广告指标并运行诊断规则…")
    res = weekly_pipe.generate_weekly(week_start=week, engine=engine,
                                      on_event=lambda m: jobs_mod.step(job, job.done, 3, m))
    jobs_mod.step(job, 3, 3, "周报已生成")
    return {
        "week": res.week_label,
        "ok": res.ok,
        "findings": len(res.findings),
        "reports": {"md": res.report_md, "html": res.report_html, "xlsx": res.report_xlsx},
        "charts": res.charts,
        "downloads": {
            "html": f"/files/report/{res.week_label}/html",
            "md": f"/files/report/{res.week_label}/md",
            "xlsx": f"/files/report/{res.week_label}/xlsx",
        },
    }
