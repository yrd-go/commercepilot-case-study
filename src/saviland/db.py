# -*- coding: utf-8 -*-
"""数据库层：SQLite schema + 通用 DAO。

为什么用 SQLite 而不是 MySQL/MongoDB
------------------------------------
这是一个**要能在任何人机器上双击就跑起来**的作品集项目：SQLite 零部署、
单文件、可随仓库带走，而且这个量级（几千行广告事实 + 几百个订单）完全不需要
独立数据库进程。业务层写的是标准 SQL，真要换 MySQL 只需替换本文件的连接层。

表设计遵循"事实表 + 维度表"：
    products / inventory / orders / ad_campaigns / ad_daily   —— 业务事实
    listing_copies / generation_runs / artifacts              —— AI 链路的产出与可追溯记录
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================ 商品 ============================
CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    asin            TEXT,
    title_cn        TEXT NOT NULL,
    title_en        TEXT,
    category        TEXT NOT NULL,
    sub_category    TEXT,
    price_usd       REAL NOT NULL,
    cost_usd        REAL NOT NULL,
    stock           INTEGER NOT NULL DEFAULT 0,
    rating          REAL,
    review_count    INTEGER,
    hero_image      TEXT,
    target_audience TEXT,
    status          TEXT NOT NULL DEFAULT 'active',   -- active / paused / draft
    evidence        TEXT,                             -- verified / inferred（数据可信度）
    source_url      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);

-- 商品的文本资产：卖点 / 五点描述 / 关键词 / 痛点 / 场景 / 规格
CREATE TABLE IF NOT EXISTS product_attributes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sku     TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    kind    TEXT NOT NULL,   -- selling_point|five_bullet|keyword|pain_point|scenario|spec
    seq     INTEGER NOT NULL DEFAULT 0,
    name    TEXT,            -- kind=spec 时的规格名
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_sku_kind ON product_attributes(sku, kind);

-- ============================ 库存 ============================
CREATE TABLE IF NOT EXISTS inventory (
    sku            TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    warehouse      TEXT NOT NULL,          -- FBA-US / FBA-UK / 海外仓-CA
    on_hand        INTEGER NOT NULL DEFAULT 0,
    reserved       INTEGER NOT NULL DEFAULT 0,
    safety_stock   INTEGER NOT NULL DEFAULT 30,
    lead_time_days INTEGER NOT NULL DEFAULT 25,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sku, warehouse)
);

CREATE TABLE IF NOT EXISTS inventory_moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sku        TEXT NOT NULL,
    warehouse  TEXT NOT NULL,
    move_type  TEXT NOT NULL,              -- inbound / outbound / adjust
    qty        INTEGER NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_moves_sku ON inventory_moves(sku);

-- ============================ 订单 ============================
CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    sku            TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    channel        TEXT NOT NULL,          -- amazon_us / amazon_uk / tiktok_shop / shopify
    qty            INTEGER NOT NULL,
    unit_price_usd REAL NOT NULL,
    unit_cost_usd  REAL NOT NULL,
    status         TEXT NOT NULL,          -- paid / shipped / refunded / pending
    country        TEXT,
    ordered_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_sku ON orders(sku);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(ordered_at);

-- ============================ 广告 ============================
CREATE TABLE IF NOT EXISTS ad_campaigns (
    campaign_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    sku             TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    ad_type         TEXT NOT NULL,         -- SP / SB / SD
    targeting       TEXT,                  -- 手动精准 / 手动广泛 / 自动 / 商品投放
    daily_budget_usd REAL,
    status          TEXT NOT NULL DEFAULT 'enabled'
);
CREATE INDEX IF NOT EXISTS idx_campaign_sku ON ad_campaigns(sku);

CREATE TABLE IF NOT EXISTS ad_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,            -- YYYY-MM-DD
    campaign_id  TEXT NOT NULL REFERENCES ad_campaigns(campaign_id) ON DELETE CASCADE,
    sku          TEXT NOT NULL,
    impressions  INTEGER NOT NULL DEFAULT 0,
    clicks       INTEGER NOT NULL DEFAULT 0,
    spend_usd    REAL NOT NULL DEFAULT 0,
    orders       INTEGER NOT NULL DEFAULT 0,
    units        INTEGER NOT NULL DEFAULT 0,
    sales_usd    REAL NOT NULL DEFAULT 0,
    UNIQUE(date, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_ad_daily_date ON ad_daily(date);
CREATE INDEX IF NOT EXISTS idx_ad_daily_sku ON ad_daily(sku);

-- ==================== AI 链路产物与可追溯 ====================
-- 链路 1 的文案版本：每次生成都留版本，可回滚、可 A/B
CREATE TABLE IF NOT EXISTS listing_copies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sku           TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    language      TEXT NOT NULL DEFAULT 'en',
    version       INTEGER NOT NULL,
    title         TEXT,
    hero_headline TEXT,                    -- 主图大字
    hero_subhead  TEXT,                    -- 主图副标题
    hero_badge    TEXT,                    -- 主图角标（如 "48W · 36 LEDs"）
    cta           TEXT,
    bullets_json  TEXT,
    description   TEXT,
    keywords_json TEXT,
    engine        TEXT,                    -- zhipu:glm-4-flash / deepseek / offline
    run_id        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(sku, language, version)
);

-- 每一次 AI 运行都落一条：谁跑的、用了哪个引擎、检索命中几条、耗时、状态
CREATE TABLE IF NOT EXISTS generation_runs (
    run_id      TEXT PRIMARY KEY,
    pipeline    TEXT NOT NULL,             -- listing / weekly
    target      TEXT,                      -- SKU 或 周次
    engine      TEXT,
    rag_hits    INTEGER DEFAULT 0,
    latency_ms  INTEGER DEFAULT 0,
    prompt_chars INTEGER DEFAULT 0,
    status      TEXT NOT NULL,             -- ok / degraded / failed
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_runs_pipeline ON generation_runs(pipeline, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT,
    kind       TEXT NOT NULL,              -- hero_image / copy / weekly_report / chart / export
    path       TEXT NOT NULL,
    meta_json  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
"""


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------
def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    """建表（幂等）。返回数据库文件路径。"""
    path = Path(db_path) if db_path else config.DB_PATH
    with connect(path) as conn:
        conn.executescript(SCHEMA)
    return path


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """事务上下文：正常提交，异常回滚。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 通用 DAO 助手
# --------------------------------------------------------------------------
def query(sql: str, params: Sequence[Any] = (), db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        cur = conn.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: Sequence[Any] = (), db_path: Path | str | None = None) -> dict | None:
    rows = query(sql, params, db_path)
    return rows[0] if rows else None


def scalar(sql: str, params: Sequence[Any] = (), db_path: Path | str | None = None) -> Any:
    row = query_one(sql, params, db_path)
    if row is None:
        return None
    return next(iter(row.values()))


def execute(sql: str, params: Sequence[Any] = (), db_path: Path | str | None = None) -> int:
    with session(db_path) as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.rowcount


def executemany(sql: str, rows: Iterable[Sequence[Any]], db_path: Path | str | None = None) -> int:
    rows = list(rows)
    with session(db_path) as conn:
        conn.executemany(sql, rows)
    return len(rows)


def table_counts(db_path: Path | str | None = None) -> dict[str, int]:
    """各表行数 —— 后台首页与自检都用它。"""
    tables = [
        "products", "product_attributes", "inventory", "inventory_moves",
        "orders", "ad_campaigns", "ad_daily", "listing_copies",
        "generation_runs", "artifacts",
    ]
    out: dict[str, int] = {}
    for t in tables:
        try:
            out[t] = int(scalar(f"SELECT COUNT(*) FROM {t}", (), db_path) or 0)
        except sqlite3.Error:
            out[t] = -1
    return out


def reset_business_data(db_path: Path | str | None = None) -> None:
    """清空业务事实表（保留 schema），便于重新灌数据。

    注意：只清业务表，`generation_runs` / `artifacts` 属于"AI 运行档案"，
    默认保留 —— 作品集里那些运行记录本身就是证据。
    """
    with session(db_path) as conn:
        for t in ("ad_daily", "ad_campaigns", "orders", "inventory_moves", "inventory",
                  "product_attributes", "products"):
            conn.execute(f"DELETE FROM {t}")
