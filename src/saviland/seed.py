# -*- coding: utf-8 -*-
"""数据灌入层：产品主数据、库存、订单、广告投放数据。

关于"真实数据"的诚实说明
------------------------
- **产品主数据**：来自 `data/catalog/products.json`（另一条链路产出，字段里带
  `evidence: verified/inferred`，标明哪些是查证到的事实、哪些是补全的）。
- **广告与订单数据**：**仿真的**。亚马逊广告报表需要卖家后台权限，拿不到真数据；
  这里用带固定随机种子的生成器造一份结构完全对齐亚马逊广告后台导出格式的数据。
  **这不是为了假装有数据，而是为了让两条 AI 链路的输入是可复现的**：
  同一台机器跑两次，得到完全一样的周报，评审演示才可控。
- **要换成真数据**：把亚马逊广告后台导出的 CSV 丢进 `data/ads/`，调
  `import_ads_csv()` 即可（列名做了模糊映射，能吃掉亚马逊的中英文表头）。
  这也是"离线内置 + 可切换真实数据"这条设计线的落点。

关于刻意注入的问题
------------------
生成器会给不同 SKU 分配不同的"投放画像"（archetype），**故意**造出真实运营里
最常见的几类问题：预算打满被截流、只花钱不出单、点击率崩了（主图问题）、
转化率崩了（详情页问题）、自动投放乱花钱、广告加码但库存撑不住。
这样链路 2 的诊断引擎才有东西可诊断 —— 一份全是健康数据的周报，演示不出能力。
"""
from __future__ import annotations

import csv
import json
import random
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import config, db
from .domain import ads as ads_domain
from .domain import catalog as catalog_domain
from .domain import inventory as inv_domain
from .domain import orders as orders_domain

SEED = 20260214          # 固定随机种子：保证任何人跑出来的周报一模一样
CHANNELS = [
    ("amazon_us", "US", 0.62),
    ("amazon_uk", "GB", 0.14),
    ("tiktok_shop", "US", 0.16),
    ("shopify", "US", 0.08),
]

# --------------------------------------------------------------------------
# 投放画像：每个活动一个"性格"，决定它的 CTR / CVR / ACOS 落点
#   cpc 不直接给，而是由 acos 反推：cpc = acos × cvr × aov
#   —— 这样"我想要它 ACOS 是多少"就精确可控，不用反复试参数
# --------------------------------------------------------------------------
ARCHETYPES: list[dict[str, Any]] = [
    # 效率冠军：点击率高、转化率高、ACOS 极低，且预算被打满 → 该加预算
    dict(sku="SV-UVL-48W", name="Saviland 48W 美甲灯-手动精准-核心词", ad_type="SP",
         targeting="手动精准", ctr=0.0068, cvr=0.145, acos=0.17, budget=60.0,
         pattern="budget_capped", note="效率冠军"),
    # 健康且预算打满 → 放量
    dict(sku="SV-GEL-SET12", name="Saviland 12色甲油胶套装-手动精准", ad_type="SP",
         targeting="手动精准", ctr=0.0055, cvr=0.120, acos=0.22, budget=45.0,
         pattern="budget_capped", note="健康且打满"),
    # 转化很好但买贵了 → 降竞价，不是砍词
    dict(sku="SV-UVL-168W", name="Saviland 168W 专业美甲灯-手动精准", ad_type="SP",
         targeting="手动精准", ctr=0.0046, cvr=0.115, acos=0.62, budget=48.0,
         pattern="expensive", note="转化好但买贵了"),
    # 点击率还行，转化率崩了 → 详情页问题，回头找链路 1 改五点描述
    dict(sku="SV-TOOL-DRILL", name="Saviland 便携打磨机-手动广泛", ad_type="SP",
         targeting="手动广泛", ctr=0.0042, cvr=0.017, acos=1.05, budget=40.0,
         pattern="low_cvr", note="转化率崩了"),
    # 曝光巨大但没人点 → 主图问题，回头找链路 1 改主图
    dict(sku="SV-GEL-CAT", name="Saviland 猫眼甲油胶-手动精准", ad_type="SP",
         targeting="手动精准", ctr=0.00085, cvr=0.095, acos=0.30, budget=30.0,
         pattern="low_ctr", note="点击率崩了"),
    # 自动投放只花钱不出单
    dict(sku="SV-ACC-RHINE", name="Saviland 水钻饰品盒-自动投放", ad_type="SP",
         targeting="自动", ctr=0.0034, cvr=0.0, acos=None, budget=26.0,
         pattern="zero_order", note="只花钱不出单"),
    # 自动投放乱花钱 → 词迁移到手动
    dict(sku="SV-GEL-BASE", name="Saviland 底胶-自动投放", ad_type="SP",
         targeting="自动", ctr=0.0040, cvr=0.055, acos=0.58, budget=34.0,
         pattern="auto_waste", note="自动乱花钱"),
    # 稳定健康
    dict(sku="SV-KIT-BEGINNER", name="Saviland 新手套装-手动精准", ad_type="SP",
         targeting="手动精准", ctr=0.0050, cvr=0.095, acos=0.26, budget=35.0,
         pattern="steady", note="稳定"),
    # 新词测试活动：预算极小、点击样本不足 → 验证"点击量不够就不下结论"这条规则
    dict(sku="SV-CARE-OIL", name="Saviland 指缘护理油-新词测试", ad_type="SP",
         targeting="手动广泛", ctr=0.0039, cvr=0.070, acos=0.44, budget=3.0,
         pattern="low_volume", note="样本不足"),
    # 小而美
    dict(sku="SV-TOOL-FILESET", name="Saviland 抛光锉条套装-手动精准", ad_type="SP",
         targeting="手动精准", ctr=0.0061, cvr=0.130, acos=0.15, budget=20.0,
         pattern="winner", note="小而美"),
    # 稳定
    dict(sku="SV-REMOVER", name="Saviland 卸甲包-手动精准", ad_type="SP",
         targeting="手动精准", ctr=0.0044, cvr=0.085, acos=0.33, budget=12.0,
         pattern="steady", note="稳定"),
    # 品牌广告，靠曝光不靠转化
    dict(sku="SV-KIT-PRO", name="Saviland 专业套装-品牌广告", ad_type="SB",
         targeting="品牌推广-商品集", ctr=0.0022, cvr=0.060, acos=0.48, budget=22.0,
         pattern="brand", note="品牌曝光"),
]

# 库存画像：让四种状态都有代表，且比例贴近真实运营
#   —— 大部分 SKU 健康（可售 40~100 天），少数断货风险，少数压货。
#   第一版把在库给得普遍偏高，结果 20 个 SKU 里 14 个被判"压货"，
#   库存页上全是同一种颜色，反而看不出重点。
INVENTORY_PROFILE: dict[str, tuple[str, int, int]] = {
    # sku: (仓库, 在库, 安全库存)
    "SV-UVL-48W": ("FBA-US", 60, 40),          # 卖得最快 → 可售约 16 天，且广告还在放量（跨域结论）
    "SV-GEL-SET12": ("FBA-US", 370, 80),
    "SV-UVL-168W": ("FBA-US", 100, 40),
    "SV-TOOL-DRILL": ("FBA-US", 60, 40),
    "SV-GEL-CAT": ("FBA-US", 140, 60),
    "SV-ACC-RHINE": ("FBA-US", 120, 50),       # 广告零转化 + 高库存 → 压货（有意保留）
    "SV-GEL-BASE": ("FBA-US", 235, 80),
    "SV-KIT-BEGINNER": ("FBA-US", 150, 40),
    "SV-CARE-OIL": ("FBA-UK", 88, 30),
    "SV-TOOL-FILESET": ("FBA-US", 120, 30),
    "SV-REMOVER": ("FBA-UK", 130, 30),
    "SV-KIT-PRO": ("FBA-US", 20, 25),
    "SV-GEL-TOPC": ("FBA-US", 225, 70),
    "SV-GEL-BUILDER": ("FBA-US", 120, 50),
    "SV-TOOL-BRUSH": ("FBA-US", 45, 30),
    "SV-ACC-FOIL": ("FBA-US", 150, 40),        # 压货
    "SV-CARE-CUTICLE": ("FBA-UK", 30, 25),     # 断货风险
    "SV-CARE-HANDMASK": ("FBA-US", 30, 40),
    "SV-PREP-PRIMER": ("FBA-US", 90, 40),
    "SV-ACC-STICKER": ("FBA-US", 70, 40),
}

# 自然流量画像：没有广告的 SKU 靠它撑住订单
ORGANIC_VELOCITY: dict[str, float] = {
    "SV-UVL-48W": 3.2, "SV-GEL-SET12": 4.6, "SV-UVL-168W": 1.1,
    "SV-TOOL-DRILL": 0.9, "SV-GEL-CAT": 1.6, "SV-ACC-RHINE": 0.5,
    "SV-GEL-BASE": 3.4, "SV-KIT-BEGINNER": 1.8, "SV-CARE-OIL": 2.2,
    "SV-TOOL-FILESET": 1.5, "SV-REMOVER": 2.6, "SV-KIT-PRO": 0.6,
    "SV-GEL-TOPC": 3.0, "SV-GEL-BUILDER": 1.4, "SV-TOOL-BRUSH": 1.2,
    "SV-ACC-FOIL": 0.8, "SV-CARE-CUTICLE": 1.9, "SV-CARE-HANDMASK": 0.7,
    "SV-PREP-PRIMER": 1.1, "SV-ACC-STICKER": 1.3,
}


# --------------------------------------------------------------------------
# 产品
# --------------------------------------------------------------------------
def seed_products() -> int:
    return catalog_domain.seed_from_json()


# --------------------------------------------------------------------------
# 库存
# --------------------------------------------------------------------------
def seed_inventory(rng: random.Random) -> int:
    products = {p["sku"]: p for p in db.query("SELECT sku, price_usd FROM products")}
    n = 0
    for sku in products:
        warehouse, on_hand, safety = INVENTORY_PROFILE.get(sku, ("FBA-US", 200, 40))
        inv_domain.set_inventory(
            sku, warehouse, on_hand=on_hand,
            reserved=rng.randint(0, max(1, on_hand // 20)),
            safety_stock=safety, lead_time_days=rng.choice([21, 25, 28, 30]),
        )
        # 部分 SKU 额外铺一个海外仓，体现多仓
        if sku in ("SV-GEL-SET12", "SV-KIT-BEGINNER", "SV-TOOL-BRUSH"):
            inv_domain.set_inventory(sku, "海外仓-CA", on_hand=rng.randint(30, 70),
                                     safety_stock=30, lead_time_days=18)
        n += 1
    return n


def reseed_inventory() -> int:
    """只重灌库存，不动商品 / 订单 / 广告 / 文案。

    为什么需要这个入口：调整库存画像时如果走 `seed_all(force=True)`，
    `reset_business_data()` 会删掉 products，而 listing_copies 对 products 是
    `ON DELETE CASCADE` —— **已经生成的 24 个文案版本会一起没掉**，
    想恢复就得重跑一遍 20 个 SKU 的链路（约 11 分钟）。
    所以数据整形要有一个"只动这一张表"的口子。
    """
    with db.session() as conn:
        conn.execute("DELETE FROM inventory_moves")
        conn.execute("DELETE FROM inventory")
    return seed_inventory(random.Random(SEED))


def reseed_orders(days: int = 60) -> int:
    """只重灌订单（同上，避免误删文案版本）。"""
    with db.session() as conn:
        conn.execute("DELETE FROM orders")
    return seed_orders(random.Random(SEED), days=days)


def reseed_ads(days: int = 35) -> dict:
    """只重灌广告数据（campaigns + 每日事实）。"""
    with db.session() as conn:
        conn.execute("DELETE FROM ad_daily")
        conn.execute("DELETE FROM ad_campaigns")
    return seed_ads(random.Random(SEED), days=days)


_FRESH_LOCK = threading.Lock()
_FRESH_STATE: dict = {"done": False, "info": None}


def ensure_fresh_once(max_age_days: int = 1) -> dict:
    """进程内**只做一次**保鲜检查（带锁）。

    ★ 为什么需要它：启动时预热线程和第一个 API 请求会**同时**想保鲜。
    没锁的话两边各重灌一遍 1500 条订单 + 420 行广告 —— 既慢一倍，
    还可能在对方删表的中途读数据。实测把首屏从 4 秒拖到 31 秒。
    """
    with _FRESH_LOCK:
        if _FRESH_STATE["done"]:
            return _FRESH_STATE["info"] or {"action": "none", "reason": "已检查过"}
        info = ensure_fresh(max_age_days=max_age_days)
        _FRESH_STATE["done"] = True
        _FRESH_STATE["info"] = info
        return info


def ensure_fresh(max_age_days: int = 1) -> dict:
    """演示数据保鲜：数据过期就自动重灌。

    为什么必须有这个：所有生成器都以"今天"为锚点往回倒推（订单截止昨天、
    广告数据截止昨天、默认报告周是"今天所在周的前一周"）。
    数据一旦超过一周不更新，打开界面就会看到「近 30 天营收」少掉一截、
    「本周报告」变成空表 —— 一个放久了就自己坏掉的演示，等于没有演示。

    服务启动后第一次访问时检查一次：只要订单或广告数据的最新日期早于
    (今天 − max_age_days)，就把订单 / 广告 / 库存重灌一遍。
    重灌用固定随机种子，所以同一台机器在**同一天**重灌的结果完全一致。
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    today = _date.today()
    last_order = db.scalar("SELECT MAX(ordered_at) FROM orders")
    last_ad = db.scalar("SELECT MAX(date) FROM ad_daily")
    if not last_order or not last_ad:
        return {"action": "none", "reason": "还没有业务数据"}

    last = min(last_order, last_ad)
    threshold = (today - _td(days=max_age_days)).isoformat()
    if last >= threshold:
        return {"action": "none", "reason": f"数据已是最新（{last}）"}

    n_orders = reseed_orders(days=60)
    ads = reseed_ads(days=35)
    reseed_inventory()
    return {
        "action": "reseeded",
        "reason": f"数据最新只到 {last}，已重灌到今天（{today.isoformat()}）",
        "orders": n_orders,
        "ads": ads,
    }


# --------------------------------------------------------------------------
# 订单
# --------------------------------------------------------------------------
def seed_orders(rng: random.Random, days: int = 60) -> int:
    products = {p["sku"]: p for p in db.query("SELECT sku, price_usd, cost_usd FROM products")}
    today = date.today()
    rows: list[tuple] = []
    counter = 0
    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        # 周末美甲品类销量更高（真实规律），用 1.25 倍模拟
        weekend = 1.25 if day.weekday() >= 5 else 1.0
        # 越接近今天，自然流量缓慢增长（体现业务在涨）
        growth = 1.0 + (days - offset) / days * 0.25
        for sku, product in products.items():
            velocity = ORGANIC_VELOCITY.get(sku, 0.8)
            lam = velocity * weekend * growth
            units = int(rng.gauss(lam, lam * 0.45))
            if units <= 0:
                continue
            # ★ 按渠道拆分必须用**最大余数法**，不能各自 round 完事。
            #   第一版是 `take = int(round(units * share))`，units=2 时
            #   各渠道分别 round 成 1/0/0/0，剩下的 1 件因为 `take == 0: continue`
            #   被直接丢掉 —— 小销量 SKU 每天丢 1 件，订单表系统性少算，
            #   进而把库存全判成"压货"（20 个 SKU 里 14 个压货，就是这么来的）。
            assigned = 0
            acc = 0.0
            remaining = units
            for idx, (channel, country, share) in enumerate(CHANNELS):
                if remaining <= 0:
                    break
                acc += share
                if idx == len(CHANNELS) - 1:
                    take = remaining
                else:
                    target = int(round(units * acc))
                    take = max(0, min(remaining, target - assigned))
                if take <= 0:
                    continue
                assigned += take
                remaining -= take
                counter += 1
                # 售价带 ±5% 波动（优惠券/秒杀）
                price = round(float(product["price_usd"]) * rng.uniform(0.95, 1.05), 2)
                status = "refunded" if rng.random() < 0.022 else rng.choice(
                    ["shipped", "shipped", "shipped", "paid"])
                rows.append((
                    f"ORD-{day.strftime('%Y%m%d')}-{counter:06d}", sku, channel, take,
                    price, float(product["cost_usd"]), status, country, day.isoformat(),
                ))
    return orders_domain.insert_orders(rows)


# --------------------------------------------------------------------------
# 广告
# --------------------------------------------------------------------------
def _weekday_factor(d: date) -> float:
    return {0: 0.95, 1: 0.97, 2: 0.99, 3: 1.0, 4: 1.04, 5: 1.12, 6: 1.08}[d.weekday()]


# 每种画像的"预算利用率"：花掉日预算的多少。
#   budget_capped 顶到 92%~100%（触发"预算截流"规则）；low_volume 只花一点点。
PATTERN_UTIL: dict[str, tuple[float, float]] = {
    "budget_capped": (0.92, 1.00),
    "winner": (0.72, 0.90),
    "steady": (0.62, 0.84),
    "expensive": (0.84, 0.98),
    "low_cvr": (0.70, 0.90),
    "low_ctr": (0.55, 0.75),
    "zero_order": (0.80, 0.95),
    "auto_waste": (0.78, 0.95),
    "brand": (0.60, 0.80),
    "low_volume": (0.45, 0.65),
}
# 低样本画像：直接指定每周点击量，用来验证"点击不足不下结论"这条规则真的生效
CLICK_BUDGET_OVERRIDE: dict[str, tuple[int, int]] = {"low_volume": (1, 2)}


def seed_ads(rng: random.Random, days: int = 35) -> dict:
    """生成广告活动与每日事实数据。数据截止到**昨天**，保证默认报告周是完整的。

    建模顺序刻意是"花费 → 点击 → 曝光"，而不是"曝光 → 点击 → 花费"：

        1. 日花费 ≈ 日预算 × 利用率（预算是投放的唯一硬约束）
        2. CPC 由目标 ACOS 反推：cpc = acos × cvr × 客单价 × 每单件数
        3. 点击 = 花费 / CPC
        4. 曝光 = 点击 / CTR

    反过来（先造曝光再乘 CTR）会得到一个荒谬的结果：低预算活动的点击量小到
    每天不足 1 次，订单按天取整后**永远是 0** —— 于是所有低转化活动都被误判成
    "只花钱不出单"。这个 bug 真的出现过：第一版生成器让 5 个活动同时触发
    `zero_order_burn`，而其中 3 个本该有订单。修法是引入**跨天累积器**：
    订单按周累积到 1 才落一笔，这样整数订单不会抹掉低转化率的真实信号。
    """
    products = {p["sku"]: p for p in db.query("SELECT sku, price_usd FROM products")}
    today = date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)

    campaign_rows: list[tuple] = []
    daily_rows: list[tuple] = []
    created: list[dict] = []

    for idx, arch in enumerate(ARCHETYPES, 1):
        sku = arch["sku"]
        if sku not in products:
            continue
        price = float(products[sku]["price_usd"])
        campaign_id = f"C{idx:03d}-{sku}"
        ads_domain.upsert_campaign(
            campaign_id, arch["name"], sku, arch["ad_type"], arch["targeting"],
            arch["budget"], "enabled",
        )
        campaign_rows.append((campaign_id, arch["name"], sku, arch["ad_type"],
                              arch["targeting"], arch["budget"]))
        created.append({"campaign_id": campaign_id, "sku": sku, "pattern": arch["pattern"]})

        util_lo, util_hi = PATTERN_UTIL.get(arch["pattern"], (0.6, 0.85))
        ctr = max(arch["ctr"], 1e-5)
        cvr = arch["cvr"]
        orders_acc = 0.0                       # 跨天累积的订单（关键修复）
        click_override = CLICK_BUDGET_OVERRIDE.get(arch["pattern"])

        d = start
        while d <= end:
            wf = _weekday_factor(d)
            # 最近 7 天 CPC 上浮 18%，模拟竞争加剧
            cpc_boost = 1.18 if (end - d).days <= 6 else 1.0

            # 1) 日花费
            spend = arch["budget"] * rng.uniform(util_lo, util_hi) * rng.uniform(0.88, 1.1)
            spend = max(0.5, round(spend, 2))

            # 2) CPC：由目标 ACOS 反推（acos 为 None 的零转化活动给一个显式 CPC）
            if arch["acos"] is None:
                cpc = 0.62
            else:
                cpc = arch["acos"] * cvr * price * rng.uniform(0.92, 1.08)
            cpc = max(0.15, cpc * cpc_boost)

            # 3) 点击
            if click_override:
                clicks = rng.randint(*click_override)
                spend = round(clicks * cpc, 2)
            else:
                clicks = max(0, int(round(spend / cpc)))
                spend = round(clicks * cpc, 2)

            # 4) 曝光
            impressions = max(clicks, int(round(clicks / ctr * rng.uniform(0.9, 1.1))))

            # 订单：跨天累积，避免低转化率被按天取整抹平
            if arch["pattern"] == "zero_order":
                orders_n = 0
            else:
                orders_acc += clicks * cvr * rng.uniform(0.75, 1.25) * wf
                orders_n = int(orders_acc)
                orders_acc -= orders_n

            units_per_order = rng.uniform(1.0, 1.3)
            units = int(round(orders_n * units_per_order)) if orders_n else 0
            sales = round(units * price * rng.uniform(0.96, 1.04), 2) if units else 0.0

            daily_rows.append((d.isoformat(), campaign_id, sku, impressions, clicks,
                               spend, orders_n, units, sales))
            d += timedelta(days=1)

    n = ads_domain.insert_ad_daily(daily_rows)
    _export_ads_csv(campaign_rows, daily_rows)
    return {"campaigns": len(created), "daily_rows": n,
            "window": f"{start.isoformat()} ~ {end.isoformat()}", "detail": created}


# --------------------------------------------------------------------------
# CSV 导出 / 导入（"可切换真实数据"的落点）
# --------------------------------------------------------------------------
AD_DAILY_HEADER = ["date", "campaign_id", "sku", "impressions", "clicks",
                   "spend_usd", "orders", "units", "sales_usd"]
AD_CAMPAIGN_HEADER = ["campaign_id", "name", "sku", "ad_type", "targeting", "daily_budget_usd"]


def _export_ads_csv(campaign_rows: list[tuple], daily_rows: list[tuple]) -> None:
    config.ADS_DIR.mkdir(parents=True, exist_ok=True)
    (config.ADS_DIR / "ad_campaigns.csv").write_text(
        "\n".join([",".join(AD_CAMPAIGN_HEADER)] +
                  [",".join('"' + str(c) + '"' for c in row) for row in campaign_rows]),
        encoding="utf-8-sig",
    )
    with (config.ADS_DIR / "ad_daily.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(AD_DAILY_HEADER)
        w.writerows(daily_rows)


# 亚马逊后台导出表头的模糊映射（中英文都覆盖）
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期", "start date", "reporting date"),
    "campaign_id": ("campaign id", "campaignid", "广告活动 id", "广告活动编号"),
    "campaign_name": ("campaign name", "广告活动名称"),
    "sku": ("sku", "商品 sku", "msku", "advertised sku"),
    "impressions": ("impressions", "曝光量", "展示量"),
    "clicks": ("clicks", "点击量"),
    "spend_usd": ("spend", "cost", "花费", "支出"),
    "orders": ("orders", "purchases", "订单量", "购买量", "7 day total orders"),
    "units": ("units", "units sold", "销售量", "7 day total units"),
    "sales_usd": ("sales", "revenue", "销售额", "7 day total sales"),
}


def _norm(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def import_ads_csv(path: Path | str) -> dict:
    """导入亚马逊广告报表 CSV。列名做模糊映射，缺 SKU 时按活动名匹配。

    返回 {rows, skipped, mapped_columns}。导入失败的行不会中断整批。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    if not text:
        return {"rows": 0, "skipped": 0, "mapped_columns": {}}
    reader = csv.DictReader(text)
    headers = {_norm(h): h for h in (reader.fieldnames or [])}
    mapped: dict[str, str] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            key = _norm(alias)
            if key in headers:
                mapped[field] = headers[key]
                break
    if "date" not in mapped or "campaign_id" not in mapped:
        raise ValueError(f"报表缺少必要列（date / campaign id）：实际表头 {reader.fieldnames}")

    sku_by_campaign = {r["campaign_id"]: r["sku"] for r in db.query(
        "SELECT campaign_id, sku FROM ad_campaigns")}

    def num(row: dict, field: str) -> float:
        raw = row.get(mapped.get(field, ""), "") if field in mapped else ""
        raw = (raw or "").replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    rows: list[tuple] = []
    skipped = 0
    for row in reader:
        try:
            cid = (row.get(mapped["campaign_id"]) or "").strip()
            d = (row.get(mapped["date"]) or "").strip()
            if not cid or not d:
                skipped += 1
                continue
            # 日期归一化：支持 2026-02-14 / 2026/02/14 / 02/14/2026
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y%m%d"):
                try:
                    d = datetime.strptime(d, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
            sku = (row.get(mapped.get("sku", ""), "") or "").strip() or sku_by_campaign.get(cid, "")
            if not sku:
                skipped += 1
                continue
            rows.append((d, cid, sku, int(num(row, "impressions")), int(num(row, "clicks")),
                         num(row, "spend_usd"), int(num(row, "orders")), int(num(row, "units")),
                         num(row, "sales_usd")))
        except Exception:  # noqa: BLE001 - 单行脏数据不该毁掉整份报表
            skipped += 1
    inserted = ads_domain.insert_ad_daily(rows)
    return {"rows": inserted, "skipped": skipped,
            "mapped_columns": {k: v for k, v in mapped.items()}}


# --------------------------------------------------------------------------
# 一键灌数
# --------------------------------------------------------------------------
def seed_all(force: bool = False, days_orders: int = 60, days_ads: int = 35) -> dict:
    """灌入全部业务数据。已有数据且 force=False 时跳过（幂等）。"""
    db.init_db()
    existing = db.scalar("SELECT COUNT(*) FROM products") or 0
    if existing and not force:
        return {"skipped": True, "reason": f"已有 {existing} 个产品，未指定 force",
                "counts": db.table_counts()}
    if force:
        db.reset_business_data()

    rng = random.Random(SEED)
    result: dict[str, Any] = {"skipped": False}
    result["products"] = seed_products()
    result["inventory"] = seed_inventory(rng)
    result["orders"] = seed_orders(rng, days_orders)
    result["ads"] = seed_ads(rng, days_ads)
    result["counts"] = db.table_counts()

    # 灌完立刻跑一次周度指标，把"这周账户长什么样"打出来，方便核对
    week_start = ads_domain.week_bounds(date.today() - timedelta(days=7))[0]
    metrics = ads_domain.weekly_metrics(week_start)
    result["preview_week"] = week_start
    result["preview_total"] = metrics["total"]
    result["preview_findings"] = len(ads_domain.diagnose(metrics))
    return result


def reset_and_seed() -> dict:
    return seed_all(force=True)


if __name__ == "__main__":  # pragma: no cover
    out = seed_all(force=True)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
