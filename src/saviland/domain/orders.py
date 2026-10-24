# -*- coding: utf-8 -*-
"""订单域：订单写入与销售分析（营收 / 毛利 / 渠道 / 类目 / 趋势）。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .. import config, db

BIZ = config.Biz


def insert_orders(rows: Iterable[Sequence[Any]]) -> int:
    """批量写入订单。rows 顺序：
    (order_id, sku, channel, qty, unit_price_usd, unit_cost_usd, status, country, ordered_at)
    """
    rows = list(rows)
    if not rows:
        return 0
    with db.session() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO orders "
            "(order_id, sku, channel, qty, unit_price_usd, unit_cost_usd, status, country, ordered_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def _window(days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def sales_summary(days: int = 30) -> dict[str, Any]:
    """近 N 天销售汇总。营收只算非退款订单，退款单独列出。"""
    start, end = _window(days)
    row = db.query_one(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN status != 'refunded' THEN qty * unit_price_usd END),0) AS revenue, "
        "  COALESCE(SUM(CASE WHEN status != 'refunded' THEN qty END),0) AS units, "
        "  COALESCE(SUM(CASE WHEN status != 'refunded' THEN qty * unit_cost_usd END),0) AS cogs, "
        "  COALESCE(SUM(CASE WHEN status = 'refunded' THEN qty END),0) AS refund_units, "
        "  COUNT(*) AS order_rows "
        "FROM orders WHERE ordered_at BETWEEN ? AND ?",
        (start, end),
    ) or {}
    revenue = float(row.get("revenue") or 0)
    cogs = float(row.get("cogs") or 0)
    gross = revenue - cogs
    return {
        "window": f"{start} ~ {end}",
        "days": days,
        "revenue": round(revenue, 2),
        "units": int(row.get("units") or 0),
        "cogs": round(cogs, 2),
        "gross_profit": round(gross, 2),
        "gross_margin": round(gross / revenue, 4) if revenue else 0.0,
        "refund_units": int(row.get("refund_units") or 0),
        "order_rows": int(row.get("order_rows") or 0),
    }


def by_channel(days: int = 30) -> list[dict]:
    start, end = _window(days)
    return db.query(
        "SELECT channel, "
        "  ROUND(SUM(CASE WHEN status != 'refunded' THEN qty * unit_price_usd ELSE 0 END),2) AS revenue, "
        "  SUM(CASE WHEN status != 'refunded' THEN qty ELSE 0 END) AS units, "
        "  ROUND(SUM(CASE WHEN status != 'refunded' THEN (unit_price_usd - unit_cost_usd) * qty ELSE 0 END),2) AS gross_profit "
        "FROM orders WHERE ordered_at BETWEEN ? AND ? GROUP BY channel ORDER BY revenue DESC",
        (start, end),
    )


def by_category(days: int = 30) -> list[dict]:
    start, end = _window(days)
    return db.query(
        "SELECT p.category, "
        "  ROUND(SUM(CASE WHEN o.status != 'refunded' THEN o.qty * o.unit_price_usd ELSE 0 END),2) AS revenue, "
        "  SUM(CASE WHEN o.status != 'refunded' THEN o.qty ELSE 0 END) AS units "
        "FROM orders o JOIN products p ON p.sku = o.sku "
        "WHERE o.ordered_at BETWEEN ? AND ? GROUP BY p.category ORDER BY revenue DESC",
        (start, end),
    )


def top_products(days: int = 30, limit: int = 10) -> list[dict]:
    start, end = _window(days)
    return db.query(
        "SELECT o.sku, p.title_cn, p.category, "
        "  SUM(CASE WHEN o.status != 'refunded' THEN o.qty ELSE 0 END) AS units, "
        "  ROUND(SUM(CASE WHEN o.status != 'refunded' THEN o.qty * o.unit_price_usd ELSE 0 END),2) AS revenue, "
        "  ROUND(SUM(CASE WHEN o.status != 'refunded' THEN (o.unit_price_usd - o.unit_cost_usd) * o.qty ELSE 0 END),2) AS gross_profit "
        "FROM orders o JOIN products p ON p.sku = o.sku "
        "WHERE o.ordered_at BETWEEN ? AND ? "
        "GROUP BY o.sku ORDER BY revenue DESC LIMIT ?",
        (start, end, limit),
    )


def daily_series(days: int = 30) -> list[dict]:
    start, end = _window(days)
    return db.query(
        "SELECT ordered_at AS date, "
        "  SUM(CASE WHEN status != 'refunded' THEN qty ELSE 0 END) AS units, "
        "  ROUND(SUM(CASE WHEN status != 'refunded' THEN qty * unit_price_usd ELSE 0 END),2) AS revenue "
        "FROM orders WHERE ordered_at BETWEEN ? AND ? GROUP BY ordered_at ORDER BY ordered_at",
        (start, end),
    )


def refund_rate(days: int = 30) -> dict:
    start, end = _window(days)
    row = db.query_one(
        "SELECT "
        "  SUM(CASE WHEN status = 'refunded' THEN qty ELSE 0 END) AS refund_qty, "
        "  SUM(qty) AS total_qty FROM orders WHERE ordered_at BETWEEN ? AND ?",
        (start, end),
    ) or {}
    refund_qty = int(row.get("refund_qty") or 0)
    total_qty = int(row.get("total_qty") or 0)
    return {
        "refund_qty": refund_qty,
        "total_qty": total_qty,
        "rate": round(refund_qty / total_qty, 4) if total_qty else 0.0,
    }
