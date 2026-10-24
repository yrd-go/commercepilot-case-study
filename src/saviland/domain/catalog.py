# -*- coding: utf-8 -*-
"""商品域：产品主数据、文本资产（卖点/关键词/痛点）、文案版本管理。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config, db

# 产品文本资产的合法类型
ATTR_KINDS = ("selling_point", "five_bullet", "keyword", "pain_point", "scenario", "spec")

PRODUCT_COLUMNS = (
    "sku", "asin", "title_cn", "title_en", "category", "sub_category",
    "price_usd", "cost_usd", "stock", "rating", "review_count", "hero_image",
    "target_audience", "status", "evidence", "source_url",
)


def upsert_product(product: dict[str, Any]) -> None:
    """写入/更新一个产品及其全部文本资产。"""
    row = {k: product.get(k) for k in PRODUCT_COLUMNS}
    row["status"] = row.get("status") or "active"
    placeholders = ", ".join(f":{c}" for c in PRODUCT_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in PRODUCT_COLUMNS if c != "sku")

    with db.session() as conn:
        conn.execute(
            f"INSERT INTO products ({', '.join(PRODUCT_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(sku) DO UPDATE SET {updates}",
            row,
        )
        conn.execute("DELETE FROM product_attributes WHERE sku = ?", (row["sku"],))
        attr_rows: list[tuple] = []
        specs = product.get("specs") or {}
        if isinstance(specs, dict):
            for i, (name, value) in enumerate(specs.items()):
                attr_rows.append((row["sku"], "spec", i, str(name), str(value)))
        for kind in ATTR_KINDS:
            if kind == "spec":
                continue
            values = product.get(kind) or product.get(kind + "s") or []
            if isinstance(values, str):
                values = [values]
            for i, value in enumerate(values):
                if value is None:
                    continue
                attr_rows.append((row["sku"], kind, i, None, str(value)))
        if attr_rows:
            conn.executemany(
                "INSERT INTO product_attributes (sku, kind, seq, name, content) "
                "VALUES (?, ?, ?, ?, ?)",
                attr_rows,
            )


def get_product(sku: str) -> dict | None:
    """取单个产品（含文本资产，已按 kind 分组）。"""
    product = db.query_one("SELECT * FROM products WHERE sku = ?", (sku,))
    if not product:
        return None
    rows = db.query(
        "SELECT kind, seq, name, content FROM product_attributes WHERE sku = ? ORDER BY kind, seq",
        (sku,),
    )
    grouped: dict[str, list] = {k: [] for k in ATTR_KINDS}
    specs: dict[str, str] = {}
    for r in rows:
        if r["kind"] == "spec":
            specs[r["name"]] = r["content"]
        elif r["kind"] in grouped:
            grouped[r["kind"]].append(r["content"])
    product["specs"] = specs
    product["attributes"] = grouped
    for kind, values in grouped.items():
        product[kind if kind != "selling_point" else "selling_points"] = values
        if kind == "selling_point":
            product["selling_points"] = values
    return product


def list_products(
    category: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    sql = "SELECT * FROM products WHERE 1=1"
    params: list[Any] = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if keyword:
        sql += " AND (sku LIKE ? OR title_cn LIKE ? OR title_en LIKE ?)"
        like = f"%{keyword}%"
        params += [like, like, like]
    sql += " ORDER BY category, sku"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.query(sql, params)


def get_attributes(sku: str, kind: str) -> list[str]:
    rows = db.query(
        "SELECT content FROM product_attributes WHERE sku = ? AND kind = ? ORDER BY seq",
        (sku, kind),
    )
    return [r["content"] for r in rows]


def categories() -> list[str]:
    rows = db.query("SELECT DISTINCT category FROM products ORDER BY category")
    return [r["category"] for r in rows]


def summary() -> dict:
    """后台首页用的商品概览。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n, "
        "       AVG(price_usd) AS avg_price, "
        "       SUM(stock) AS stock_total, "
        "       AVG(rating) AS avg_rating "
        "FROM products"
    ) or {}
    by_cat = db.query(
        "SELECT category, COUNT(*) AS n, AVG(price_usd) AS avg_price "
        "FROM products GROUP BY category ORDER BY n DESC"
    )
    return {"total": row.get("n") or 0, **row, "by_category": by_cat}


def margin_of(product: dict) -> float:
    """毛利率：(售价 - 成本) / 售价。"""
    price = float(product.get("price_usd") or 0)
    cost = float(product.get("cost_usd") or 0)
    return (price - cost) / price if price > 0 else 0.0


# --------------------------------------------------------------------------
# 文案版本管理（链路 1 的产物落地）
# --------------------------------------------------------------------------
def next_copy_version(sku: str, language: str = "en") -> int:
    current = db.scalar(
        "SELECT MAX(version) FROM listing_copies WHERE sku = ? AND language = ?",
        (sku, language),
    )
    return int(current or 0) + 1


def save_listing_copy(copy: dict, language: str = "en") -> dict:
    """保存一条文案；version 不传则自动递增。返回落库后的完整记录。"""
    sku = copy["sku"]
    version = int(copy.get("version") or next_copy_version(sku, language))
    payload = (
        sku, language, version,
        copy.get("title"), copy.get("hero_headline"), copy.get("hero_subhead"),
        copy.get("hero_badge"), copy.get("cta"),
        json.dumps(copy.get("bullets") or [], ensure_ascii=False),
        copy.get("description"),
        json.dumps(copy.get("keywords") or [], ensure_ascii=False),
        copy.get("engine"), copy.get("run_id"),
    )
    with db.session() as conn:
        cur = conn.execute(
            "INSERT OR REPLACE INTO listing_copies "
            "(sku, language, version, title, hero_headline, hero_subhead, hero_badge, cta, "
            " bullets_json, description, keywords_json, engine, run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        row_id = cur.lastrowid
    return get_listing_copy(row_id)


def _decode_copy(row: dict | None) -> dict | None:
    if not row:
        return None
    row["bullets"] = json.loads(row.pop("bullets_json") or "[]")
    row["keywords"] = json.loads(row.pop("keywords_json") or "[]")
    return row


def get_listing_copy(copy_id: int) -> dict | None:
    return _decode_copy(db.query_one("SELECT * FROM listing_copies WHERE id = ?", (copy_id,)))


def latest_listing_copy(sku: str, language: str = "en") -> dict | None:
    return _decode_copy(
        db.query_one(
            "SELECT * FROM listing_copies WHERE sku = ? AND language = ? "
            "ORDER BY version DESC LIMIT 1",
            (sku, language),
        )
    )


def list_listing_copies(sku: str | None = None, limit: int = 50) -> list[dict]:
    if sku:
        rows = db.query(
            "SELECT * FROM listing_copies WHERE sku = ? ORDER BY sku, version DESC LIMIT ?",
            (sku, limit),
        )
    else:
        rows = db.query(
            "SELECT * FROM listing_copies ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
    return [_decode_copy(r) for r in rows]  # type: ignore[misc]


def seed_from_json(path: Path | None = None) -> int:
    """把 data/catalog/products.json 灌进数据库。返回产品数。"""
    path = path or config.PRODUCTS_JSON
    if not Path(path).is_file():
        raise FileNotFoundError(f"找不到产品目录：{path}")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    products: Iterable[dict] = data.get("products", data if isinstance(data, list) else [])
    n = 0
    for p in products:
        if not p.get("sku"):
            continue
        upsert_product(p)
        n += 1
    return n
