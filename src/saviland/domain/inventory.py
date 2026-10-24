# -*- coding: utf-8 -*-
"""库存域：多仓库存、动销速度、可售天数、补货建议。

评审可讲的点：库存不是"数量够不够"，而是**相对动销速度**够不够。
同一个 300 件的库存，日销 2 件能卖 5 个月（压货），日销 30 件只能撑 10 天（断货）。
所以本模块所有判断都建立在"可售天数 = 可用库存 / 日销速度"上。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import config, db

BIZ = config.Biz


def set_inventory(
    sku: str, warehouse: str, on_hand: int,
    reserved: int = 0, safety_stock: int = 30, lead_time_days: int | None = None,
) -> None:
    db.execute(
        "INSERT INTO inventory (sku, warehouse, on_hand, reserved, safety_stock, lead_time_days, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(sku, warehouse) DO UPDATE SET "
        "  on_hand=excluded.on_hand, reserved=excluded.reserved, "
        "  safety_stock=excluded.safety_stock, lead_time_days=excluded.lead_time_days, "
        "  updated_at=datetime('now')",
        (sku, warehouse, int(on_hand), int(reserved), int(safety_stock),
         int(lead_time_days if lead_time_days is not None else BIZ.DEFAULT_LEAD_TIME_DAYS)),
    )


def record_move(sku: str, warehouse: str, move_type: str, qty: int, note: str = "") -> None:
    db.execute(
        "INSERT INTO inventory_moves (sku, warehouse, move_type, qty, note) VALUES (?,?,?,?,?)",
        (sku, warehouse, move_type, int(qty), note),
    )
    # 入库为正、出库为负
    delta = int(qty) if move_type == "inbound" else -int(qty)
    db.execute(
        "UPDATE inventory SET on_hand = MAX(0, on_hand + ?), updated_at = datetime('now') "
        "WHERE sku = ? AND warehouse = ?",
        (delta, sku, warehouse),
    )


def list_inventory(sku: str | None = None) -> list[dict]:
    if sku:
        return db.query(
            "SELECT i.*, p.title_cn, p.category FROM inventory i "
            "JOIN products p ON p.sku = i.sku WHERE i.sku = ? ORDER BY i.warehouse",
            (sku,),
        )
    return db.query(
        "SELECT i.*, p.title_cn, p.category FROM inventory i "
        "JOIN products p ON p.sku = i.sku ORDER BY i.sku, i.warehouse"
    )


def stock_of(sku: str) -> dict:
    """单个 SKU 的跨仓汇总库存。"""
    row = db.query_one(
        "SELECT COALESCE(SUM(on_hand),0) AS on_hand, COALESCE(SUM(reserved),0) AS reserved, "
        "       COALESCE(MAX(lead_time_days),0) AS lead_time_days, "
        "       COALESCE(MAX(safety_stock),0) AS safety_stock "
        "FROM inventory WHERE sku = ?",
        (sku,),
    ) or {}
    on_hand = int(row.get("on_hand") or 0)
    reserved = int(row.get("reserved") or 0)
    return {
        "on_hand": on_hand,
        "reserved": reserved,
        "available": max(0, on_hand - reserved),
        "lead_time_days": int(row.get("lead_time_days") or BIZ.DEFAULT_LEAD_TIME_DAYS),
        "safety_stock": int(row.get("safety_stock") or 30),
    }


def sales_velocity(sku: str, days: int = 14) -> float:
    """近 N 天的日均销量（按订单件数，剔除退款）。

    用订单表算而不是拍脑袋给常量 —— 这样库存建议和真实销售是联动的。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    units = db.scalar(
        "SELECT COALESCE(SUM(qty),0) FROM orders "
        "WHERE sku = ? AND ordered_at >= ? AND status != 'refunded'",
        (sku, since),
    )
    return round(float(units or 0) / max(1, days), 2)


def available_days(sku: str, days: int = 14) -> float:
    velocity = sales_velocity(sku, days)
    available = stock_of(sku)["available"]
    if velocity <= 0:
        return float("inf")     # 没有动销 → 天数无意义，由 overstock 规则去管
    return round(available / velocity, 1)


def restock_advice(days: int = 14) -> list[dict]:
    """全量补货建议：断货风险 / 压货 两类。

    补货公式（跨境电商常用简化版）：
        目标库存 = 日销速度 × (补货在途 + 安全库存天数)
        建议补货量 = 目标库存 - 当前可用 - 在途
    本演示没有"在途"字段，故在途按 0 计，并在返回值里显式标注。
    """
    rows = db.query("SELECT sku, title_cn, category, price_usd FROM products ORDER BY sku")
    out: list[dict] = []
    for p in rows:
        sku = p["sku"]
        stock = stock_of(sku)
        velocity = sales_velocity(sku, days)
        avail_days = float("inf") if velocity <= 0 else round(stock["available"] / velocity, 1)
        lead = stock["lead_time_days"]
        target = velocity * (lead + 15)          # 安全库存按 15 天计
        suggest = int(max(0, round(target - stock["available"])))
        if velocity <= 0:
            level = "no_sales"
        elif avail_days <= BIZ.STOCKOUT_WARN_DAYS:
            level = "stockout_risk"
        elif avail_days >= BIZ.OVERSTOCK_DAYS:
            level = "overstock"
        else:
            level = "healthy"
        out.append({
            **p,
            "velocity": velocity,                 # 日均销量（件/天）
            "on_hand": stock["on_hand"],
            "available": stock["available"],
            "available_days": avail_days,
            "lead_time_days": lead,
            "level": level,
            "suggest_qty": suggest if level in ("stockout_risk", "healthy") else 0,
            "in_transit": 0,
        })
    order = {"stockout_risk": 0, "no_sales": 1, "overstock": 2, "healthy": 3}
    out.sort(key=lambda r: (order.get(r["level"], 9), r["available_days"]))
    return out


def summary() -> dict[str, Any]:
    advice = restock_advice()
    counts: dict[str, int] = {}
    for r in advice:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    return {
        "skus": len(advice),
        "by_level": counts,
        "total_on_hand": sum(r["on_hand"] for r in advice),
        "total_value": round(sum(r["on_hand"] * 0 for r in advice), 2),   # 占位：成本口径见 orders
        "alerts": [r for r in advice if r["level"] in ("stockout_risk", "overstock")],
    }
