# -*- coding: utf-8 -*-
"""广告域：广告投放数据、周度指标、可视化图表 + 规则诊断引擎。

这个模块是整个系统里**最像真实业务**的部分，设计上有三个刻意的取舍：

1. **指标口径统一在一处**。ACOS / ROAS / CVR / CTR / CPC 全部由 `_metrics()`
   一个函数算出来 —— 报告、图表、规则诊断共用同一份数字。运维事故最常见的一种
   就是"报表说 ACOS 30%，诊断却说 45%"，因为两处各算了一遍。

2. **规则引擎不猜**。每条结论都必须挂上可核对的证据数字（花费 / 点击 / ACOS / 环比），
   而且**点击量不够就不下结论**（`MIN_CLICKS_FOR_JUDGE`）—— "1 次点击 0 单"的
   转化率是 0%，但那不是结论，是噪声。

3. **诊断要指向动作，而不是描述现象**。"ACOS 偏高"不是建议；"转化率 12%（健康）
   但 CPC 涨到 1.8 美元，是买贵了不是买错了 → 降竞价 20%，不要砍词"才是。
   每条 finding 还标注 `feeds`：这条结论该由哪条链路去执行
   （`listing` = 回头改主图/五点描述，`ads` = 调价调预算，`inventory` = 备货）。
   两条 AI 链路就是靠这个字段闭合成环的。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from .. import config, db

BIZ = config.Biz
INF = float("inf")


# --------------------------------------------------------------------------
# 周窗口工具
# --------------------------------------------------------------------------
def week_bounds(anchor: date | str | None = None) -> tuple[str, str]:
    """返回 anchor 所在自然周（周一~周日）的 (start, end)，ISO 字符串。"""
    if anchor is None:
        d = date.today()
    elif isinstance(anchor, str):
        d = datetime.fromisoformat(anchor).date()
    else:
        d = anchor
    start = d - timedelta(days=d.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def week_label(start: str) -> str:
    d = datetime.fromisoformat(start).date()
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def default_report_week() -> str:
    """默认报告周 = 最近一个**已经完整结束**的自然周。

    为什么不是"本周"：今天是周三时，"本周"只有三天数据，把三天和一个完整周
    环比是错的（也是运营报表里最常见的口径事故）。锚点取 7 天前，无论今天是
    周几，那天所在的周一~周日都一定已经过完。
    """
    return week_bounds(date.today() - timedelta(days=7))[0]


def available_weeks() -> list[str]:
    """数据库里实际有广告数据的周（周一日期），倒序。"""
    rows = db.query(
        "SELECT DISTINCT date FROM ad_daily ORDER BY date DESC"
    )
    seen: list[str] = []
    for r in rows:
        start = week_bounds(r["date"])[0]
        if start not in seen:
            seen.append(start)
    return seen


def prev_week(start: str) -> str:
    d = datetime.fromisoformat(start).date()
    return (d - timedelta(days=7)).isoformat()


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def upsert_campaign(campaign_id: str, name: str, sku: str, ad_type: str,
                    targeting: str, daily_budget_usd: float, status: str = "enabled") -> None:
    db.execute(
        "INSERT INTO ad_campaigns (campaign_id, name, sku, ad_type, targeting, daily_budget_usd, status) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(campaign_id) DO UPDATE SET name=excluded.name, sku=excluded.sku, "
        "  ad_type=excluded.ad_type, targeting=excluded.targeting, "
        "  daily_budget_usd=excluded.daily_budget_usd, status=excluded.status",
        (campaign_id, name, sku, ad_type, targeting, float(daily_budget_usd), status),
    )


def insert_ad_daily(rows: Iterable[Sequence[Any]]) -> int:
    """rows: (date, campaign_id, sku, impressions, clicks, spend_usd, orders, units, sales_usd)"""
    rows = list(rows)
    if not rows:
        return 0
    with db.session() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO ad_daily "
            "(date, campaign_id, sku, impressions, clicks, spend_usd, orders, units, sales_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


# --------------------------------------------------------------------------
# 指标计算（唯一口径）
# --------------------------------------------------------------------------
def _metrics(impressions: float, clicks: float, spend: float,
             orders: float, units: float, sales: float) -> dict[str, Any]:
    impressions = float(impressions or 0)
    clicks = float(clicks or 0)
    spend = float(spend or 0)
    orders = float(orders or 0)
    units = float(units or 0)
    sales = float(sales or 0)
    return {
        "impressions": int(impressions),
        "clicks": int(clicks),
        "spend": round(spend, 2),
        "orders": int(orders),
        "units": int(units),
        "sales": round(sales, 2),
        "ctr": round(clicks / impressions, 5) if impressions else 0.0,
        "cvr": round(orders / clicks, 5) if clicks else 0.0,
        "cpc": round(spend / clicks, 3) if clicks else 0.0,
        "cpa": round(spend / orders, 2) if orders else None,
        # ACOS = 广告花费 / 广告销售额；销售额为 0 时无意义，返回 None 而不是 inf
        "acos": round(spend / sales, 4) if sales > 0 else None,
        "roas": round(sales / spend, 2) if spend > 0 else None,
        "aov": round(sales / orders, 2) if orders else 0.0,
    }


def _delta(cur: float | None, prev: float | None) -> dict[str, Any]:
    """环比。prev 缺失或为 0 时不编造百分比。"""
    if cur is None or prev is None:
        return {"cur": cur, "prev": prev, "diff": None, "pct": None}
    diff = cur - prev
    pct = (diff / prev) if prev else None
    return {"cur": round(cur, 4), "prev": round(prev, 4), "diff": round(diff, 4),
            "pct": round(pct, 4) if pct is not None else None}


# --------------------------------------------------------------------------
# 周度聚合
# --------------------------------------------------------------------------
def _aggregate(where: str, params: Sequence[Any]) -> dict:
    row = db.query_one(
        "SELECT COALESCE(SUM(impressions),0) AS impressions, COALESCE(SUM(clicks),0) AS clicks, "
        "       COALESCE(SUM(spend_usd),0) AS spend, COALESCE(SUM(orders),0) AS orders, "
        "       COALESCE(SUM(units),0) AS units, COALESCE(SUM(sales_usd),0) AS sales "
        f"FROM ad_daily {where}",
        params,
    ) or {}
    return _metrics(row.get("impressions", 0), row.get("clicks", 0), row.get("spend", 0),
                    row.get("orders", 0), row.get("units", 0), row.get("sales", 0))


def weekly_metrics(week_start: str | None = None) -> dict[str, Any]:
    """给定周的全部指标：账户整体 + 分活动 + 分 SKU + 环比上周。"""
    start, end = week_bounds(week_start) if week_start else week_bounds(None)
    p_start = prev_week(start)
    p_end = week_bounds(p_start)[1]      # ★ 必须取元组的第 2 个元素（周末），
    #   写成 `p_end, _ = week_bounds(p_start)` 会把"周一"当成"对比周的周日"，
    #   于是环比变成"整周 vs 单日"，出现 +617% 这种荒谬数字。真实事故，已修。

    total = _aggregate("WHERE date BETWEEN ? AND ?", (start, end))
    prev_total = _aggregate("WHERE date BETWEEN ? AND ?", (p_start, p_end))

    campaigns = db.query(
        "SELECT c.campaign_id, c.name, c.sku, c.ad_type, c.targeting, c.daily_budget_usd, c.status, "
        "       p.title_cn, p.category, "
        "       COALESCE(SUM(a.impressions),0) AS impressions, COALESCE(SUM(a.clicks),0) AS clicks, "
        "       COALESCE(SUM(a.spend_usd),0) AS spend, COALESCE(SUM(a.orders),0) AS orders, "
        "       COALESCE(SUM(a.units),0) AS units, COALESCE(SUM(a.sales_usd),0) AS sales "
        "FROM ad_campaigns c "
        "JOIN ad_daily a ON a.campaign_id = c.campaign_id "
        "JOIN products p ON p.sku = c.sku "
        "WHERE a.date BETWEEN ? AND ? "
        "GROUP BY c.campaign_id ORDER BY spend DESC",
        (start, end),
    )
    for c in campaigns:
        c.update(_metrics(c.pop("impressions"), c.pop("clicks"), c.pop("spend"),
                          c.pop("orders"), c.pop("units"), c.pop("sales")))
        days = max(1, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1)
        budget_cap = float(c.get("daily_budget_usd") or 0) * days
        c["budget_cap"] = round(budget_cap, 2)
        c["budget_utilization"] = round(c["spend"] / budget_cap, 4) if budget_cap else None
        c["sku"] = c["sku"]

    skus = db.query(
        "SELECT a.sku, p.title_cn, p.category, "
        "       COALESCE(SUM(a.impressions),0) AS impressions, COALESCE(SUM(a.clicks),0) AS clicks, "
        "       COALESCE(SUM(a.spend_usd),0) AS spend, COALESCE(SUM(a.orders),0) AS orders, "
        "       COALESCE(SUM(a.units),0) AS units, COALESCE(SUM(a.sales_usd),0) AS sales "
        "FROM ad_daily a JOIN products p ON p.sku = a.sku "
        "WHERE a.date BETWEEN ? AND ? GROUP BY a.sku ORDER BY spend DESC",
        (start, end),
    )
    for s in skus:
        s.update(_metrics(s.pop("impressions"), s.pop("clicks"), s.pop("spend"),
                          s.pop("orders"), s.pop("units"), s.pop("sales")))

    daily = db.query(
        "SELECT date, SUM(impressions) AS impressions, SUM(clicks) AS clicks, "
        "       ROUND(SUM(spend_usd),2) AS spend, SUM(orders) AS orders, ROUND(SUM(sales_usd),2) AS sales "
        "FROM ad_daily WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
        (start, end),
    )
    for d in daily:
        d.update(_metrics(d.pop("impressions"), d.pop("clicks"), d.pop("spend"),
                          d.pop("orders"), 0, d.pop("sales")))

    return {
        "week_start": start,
        "week_end": end,
        "week_label": week_label(start),
        "prev_week_start": p_start,
        "prev_week_end": p_end,
        "total": total,
        "prev_total": prev_total,
        "wow": {k: _delta(total.get(k), prev_total.get(k))
                for k in ("spend", "sales", "orders", "clicks", "impressions", "ctr", "cvr", "cpc", "acos", "roas")},
        "by_campaign": campaigns,
        "by_sku": skus,
        "daily": daily,
        "target_acos": BIZ.TARGET_ACOS,
        "target_roas": BIZ.TARGET_ROAS,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# 规则诊断引擎
# --------------------------------------------------------------------------
def _finding(rule: str, severity: str, scope: str, subject: str, subject_name: str,
             evidence: dict, diagnosis: str, action: str, feeds: str,
             impact_usd: float | None = None) -> dict:
    return {
        "rule": rule,
        "severity": severity,       # critical / warning / opportunity / info
        "scope": scope,             # account / campaign / sku
        "subject": subject,         # campaign_id 或 sku
        "subject_name": subject_name,
        "evidence": evidence,       # 可核对的数字
        "diagnosis": diagnosis,     # 为什么
        "action": action,           # 做什么
        "feeds": feeds,             # listing / ads / inventory —— 闭环指向
        "impact_usd": impact_usd,   # 预估可挽回/可增加的花费口径金额（保守估计）
    }


def diagnose(m: dict) -> list[dict]:
    """对一周指标跑规则引擎，返回按严重度排序的 findings。"""
    out: list[dict] = []
    total = m["total"]

    # ---------- 账户层 ----------
    acos = total["acos"]
    if total["spend"] > 0 and acos is not None:
        # 判定顺序必须"先跟目标线比、再跟预警线比"。
        # 曾经的写法是 `elif acos > ACOS_WARN(35%)` 才预警，于是 29.2% 这种
        # "高于目标 28% 但没到 35%" 的情况被报成"健康" —— 目标线形同虚设。
        if acos > BIZ.ACOS_CRITICAL:
            over = total["spend"] - total["sales"] * BIZ.TARGET_ACOS
            out.append(_finding(
                "account_acos_critical", "critical", "account", "ALL", "账户整体",
                {"acos": acos, "target": BIZ.TARGET_ACOS, "spend": total["spend"], "sales": total["sales"]},
                f"整体 ACOS {acos:.1%}，超过红线 {BIZ.ACOS_CRITICAL:.0%}，广告在亏钱换单。",
                f"优先处理下面按浪费额排序的活动；把预算从 ACOS 超标活动挪到健康活动。"
                f"按目标 ACOS {BIZ.TARGET_ACOS:.0%} 计，本周超出花费约 {over:.0f} 美元。",
                "ads", round(max(0.0, over), 2),
            ))
        elif acos > BIZ.ACOS_WARN:
            over = total["spend"] - total["sales"] * BIZ.TARGET_ACOS
            out.append(_finding(
                "account_acos_warning", "warning", "account", "ALL", "账户整体",
                {"acos": acos, "target": BIZ.TARGET_ACOS, "warn_line": BIZ.ACOS_WARN,
                 "spend": total["spend"], "sales": total["sales"]},
                f"整体 ACOS {acos:.1%}，高于目标 {BIZ.TARGET_ACOS:.0%}（预警线 {BIZ.ACOS_WARN:.0%}），尚未失控。",
                "抓出高 ACOS 活动逐个降竞价 10%~20%，不要全账户一刀切降价（会连好活动一起砍流量）。",
                "ads", round(max(0.0, over), 2),
            ))
        elif acos > BIZ.TARGET_ACOS:
            over = total["spend"] - total["sales"] * BIZ.TARGET_ACOS
            out.append(_finding(
                "account_acos_slightly_over", "warning", "account", "ALL", "账户整体",
                {"acos": acos, "target": BIZ.TARGET_ACOS, "gap_pp": round((acos - BIZ.TARGET_ACOS) * 100, 1),
                 "spend": total["spend"], "sales": total["sales"]},
                f"整体 ACOS {acos:.1%}，比目标 {BIZ.TARGET_ACOS:.0%} 高出 "
                f"{(acos - BIZ.TARGET_ACOS) * 100:.1f} 个百分点 —— 只是轻微超标，"
                "但按这个花费体量折算成钱并不小。",
                f"不需要大动作：只要把浪费最大的 1~2 个活动压住，就能把整体拉回目标线。"
                f"按目标 ACOS 折算，本周可优化空间约 {over:.0f} 美元。",
                "ads", round(max(0.0, over), 2),
            ))
        else:
            out.append(_finding(
                "account_acos_healthy", "opportunity", "account", "ALL", "账户整体",
                {"acos": acos, "target": BIZ.TARGET_ACOS, "roas": total["roas"]},
                f"整体 ACOS {acos:.1%} 优于目标 {BIZ.TARGET_ACOS:.0%}，ROAS {total['roas']}。",
                "广告效率健康，本周的重点不是省钱而是放量：给预算打满且 ACOS 健康的活动加预算。",
                "ads",
            ))

    # 花费集中度：少数活动吃掉大部分预算 → 单点风险
    camp_spend = [(c["campaign_id"], c["name"], c["spend"]) for c in m["by_campaign"]]
    if total["spend"] > 0 and len(camp_spend) >= 4:
        top3 = sum(s for _, _, s in sorted(camp_spend, key=lambda x: -x[2])[:3])
        share = top3 / total["spend"]
        if share > 0.6:
            out.append(_finding(
                "spend_concentration", "warning", "account", "ALL", "账户整体",
                {"top3_share": round(share, 4), "campaigns": len(camp_spend)},
                f"前 3 个活动吃掉 {share:.0%} 的广告花费，预算集中度过高。",
                "对 Top3 活动做单独的健康监控；同时用低预算测试 2~3 个新活动，避免单点失效导致整账户失速。",
                "ads",
            ))

    # 环比异动
    wow = m["wow"]
    if wow["spend"]["pct"] is not None and wow["spend"]["pct"] > 0.4 and \
       wow["sales"]["pct"] is not None and wow["sales"]["pct"] < 0.05:
        out.append(_finding(
            "wow_spend_up_sales_flat", "warning", "account", "ALL", "账户整体",
            {"spend_wow": wow["spend"]["pct"], "sales_wow": wow["sales"]["pct"]},
            f"花费环比 +{wow['spend']['pct']:.0%}，但销售额几乎没动（{wow['sales']['pct']:+.0%}）"
            "—— 多花的钱没有换来增量。",
            "检查本周是否有活动预算上调或竞价被抬高；把增量预算停掉，回到上周的投放结构再逐步试探。",
            "ads", round(max(0.0, wow["spend"]["diff"] or 0), 2),
        ))
    if wow["cpc"]["pct"] is not None and wow["cpc"]["pct"] > 0.15:
        out.append(_finding(
            "cpc_rising", "info", "account", "ALL", "账户整体",
            {"cpc_wow": wow["cpc"]["pct"], "cpc": total["cpc"]},
            f"CPC 环比上涨 {wow['cpc']['pct']:.0%}（现 {total['cpc']} 美元），是流量变贵了。",
            "流量涨价时不要用提价硬顶：优化转化率（主图/五点描述）和否定词，用更精准的流量对冲 CPC 上涨。",
            "listing",
        ))

    # ---------- 活动层 ----------
    for c in m["by_campaign"]:
        cid, name, sku = c["campaign_id"], c["name"], c["sku"]
        clicks, spend, orders = c["clicks"], c["spend"], c["orders"]
        c_acos, c_ctr, c_cvr = c["acos"], c["ctr"], c["cvr"]
        judged = clicks >= BIZ.MIN_CLICKS_FOR_JUDGE
        util = c["budget_utilization"]

        # 1) 只花钱不出单
        if judged and orders == 0 and spend >= BIZ.ZERO_ORDER_SPEND:
            out.append(_finding(
                "zero_order_burn", "critical", "campaign", cid, name,
                {"clicks": clicks, "spend": spend, "orders": 0, "cvr": 0.0, "ctr": c_ctr},
                f"{clicks} 次点击、花费 {spend:.2f} 美元，零订单 —— 流量进来了但一单没成。",
                "先别急着降价：如果是词不精准，加否定词；如果流量精准仍不成交，问题在详情页/价格/评论，"
                "把这条交给主图与卖点链路重写文案。",
                "listing", round(spend, 2),
            ))
            continue

        # 2) ACOS 严重超标
        if judged and c_acos is not None and c_acos > BIZ.ACOS_CRITICAL:
            # 先判断"是不是客单价根本撑不住广告成本" —— 这一类问题和文案、竞价都无关，
            # 是商品经济性问题。如果不先拦下来，系统会给出"优化详情页"这种无效建议。
            #
            # 判定必须加"CPC 已在平台底价区间"这个前提：
            #   · 底胶：CPC $0.15（已经是亚马逊最低价）+ ACOS 82% → 是客单价撑不住，降无可降
            #   · 168W 灯：CPC $4.81 + ACOS 62% → 是买贵了，降竞价就能解决
            # 两者的建议完全相反，混在一起就会给出错误动作。这个反例是真实数据里发现的。
            required_aov = (c["cpc"] / (BIZ.TARGET_ACOS * c["cvr"])) if c["cvr"] > 0 else None
            cpc_at_floor = c["cpc"] <= BIZ.CPC_FLOOR_ZONE
            if (cpc_at_floor and required_aov and c["aov"] > 0 and c["aov"] < required_aov):
                out.append(_finding(
                    "low_aov_unprofitable", "critical", "campaign", cid, name,
                    {"acos": c_acos, "aov": c["aov"], "required_aov": round(required_aov, 2),
                     "cpc": c["cpc"], "cvr": c_cvr, "cpc_at_platform_floor": True},
                    f"ACOS {c_acos:.1%} 超标的根因不是流量也不是文案：CPC 已经低到 {c['cpc']} 美元"
                    f"（接近平台底价，降无可降），但客单价只有 {c['aov']:.2f} 美元。"
                    f"按目标 ACOS {BIZ.TARGET_ACOS:.0%} 与当前转化率 {c_cvr:.1%} 反推，"
                    f"客单价至少要 {required_aov:.2f} 美元才养得起这个 CPC。"
                    "**这个 SKU 在当前价格下，转化率再优化也救不回来。**",
                    "三条路选一条：① 做成组合装/加购装把客单价抬到 "
                    f"{required_aov:.2f} 美元以上；② 只投转化率显著更高的精准长尾词，"
                    "把整体 CPC 继续压低；③ 承认它是引流款，转为自然流量运营、广告压到最低预算。",
                    "ads", round(max(0.0, spend - c["sales"] * BIZ.TARGET_ACOS), 2),
                ))
            # 转化率健康却买贵了 -> 降竞价；转化率也差 -> 先修文案
            elif c_cvr >= BIZ.HIGH_CVR:
                out.append(_finding(
                    "high_acos_good_cvr", "warning", "campaign", cid, name,
                    {"acos": c_acos, "cvr": c_cvr, "cpc": c["cpc"], "orders": orders},
                    f"ACOS {c_acos:.1%} 超标，但转化率 {c_cvr:.1%} 很好 —— 是**买贵了**，不是买错了。",
                    f"把竞价下调 15%~20%（当前 CPC {c['cpc']} 美元）。转化率高说明流量是对的，"
                    "不要砍词、不要暂停，降价就能把 ACOS 拉回目标线附近。",
                    "ads", round(max(0.0, spend - c["sales"] * BIZ.TARGET_ACOS), 2),
                ))
            else:
                out.append(_finding(
                    "high_acos_low_cvr", "critical", "campaign", cid, name,
                    {"acos": c_acos, "cvr": c_cvr, "clicks": clicks, "spend": spend},
                    f"ACOS {c_acos:.1%} 超标且转化率仅 {c_cvr:.1%} —— 流量不精准或详情页说服力不足。",
                    "两手一起做：投放侧收窄匹配方式（广泛改精准）+ 否掉不转化词；"
                    "内容侧用主图/卖点链路重写标题与五点描述，先修转化率再谈放量。",
                    "listing", round(max(0.0, spend - c["sales"] * BIZ.TARGET_ACOS), 2),
                ))

        # 3) 转化率低（点击够多）
        if judged and c_cvr < BIZ.LOW_CVR and c_ctr >= BIZ.LOW_CTR:
            out.append(_finding(
                "low_cvr_listing_issue", "warning", "campaign", cid, name,
                {"cvr": c_cvr, "benchmark": BIZ.HIGH_CVR, "clicks": clicks, "orders": orders},
                f"点击率正常（{c_ctr:.2%}）但转化率只有 {c_cvr:.1%}，低于健康线 {BIZ.LOW_CVR:.0%}。"
                "说明主图能吸引点击，但详情页没接住。",
                "这是**内容问题不是投放问题**：用主图/卖点链路重写五点描述，把买家最在意的三个问题"
                "（能不能自己在家做、伤不伤甲、能用多久）前置到前两条 bullet。",
                "listing",
            ))

        # 4) 点击率低（曝光够多）
        if c["impressions"] >= 3000 and c_ctr < BIZ.LOW_CTR:
            out.append(_finding(
                "low_ctr_hero_issue", "warning", "campaign", cid, name,
                {"ctr": c_ctr, "impressions": c["impressions"], "benchmark": BIZ.HIGH_CTR},
                f"曝光 {c['impressions']} 次但点击率只有 {c_ctr:.2%}，低于品类基准 {BIZ.LOW_CTR:.2%}。"
                "曝光在浪费 —— 买家看到了却没点进来。",
                "主图/标题没抓住人：用主图与卖点链路生成新主图文案（把 48W、36 灯珠、低热模式这类"
                "可量化的硬指标提到主图第一视觉位），并跑 A/B 对比点击率。",
                "listing", round(spend, 2),
            ))

        # 5) 预算打满且健康 → 放量
        if util is not None and util >= BIZ.BUDGET_CAP_RATIO and \
           (c_acos is None or c_acos <= BIZ.TARGET_ACOS) and (c_cvr >= BIZ.LOW_CVR or orders > 0):
            out.append(_finding(
                "budget_capped_scale", "opportunity", "campaign", cid, name,
                {"budget_utilization": util, "daily_budget": c["daily_budget_usd"], "acos": c_acos, "cvr": c_cvr},
                f"预算利用率 {util:.0%}（基本打满）且 ACOS "
                f"{'健康 ' + format(c_acos, '.1%') if c_acos is not None else '无销售'} —— 很可能是被预算截流了。",
                f"日预算从 {c['daily_budget_usd']:.0f} 美元上调 30%（到 "
                f"{c['daily_budget_usd'] * 1.3:.0f} 美元），观察 3 天 ACOS 是否仍守在目标线内。",
                "ads",
            ))

        # 6) 转化好又便宜 → 加码
        if judged and c_acos is not None and c_acos <= BIZ.TARGET_ACOS * 0.7 and c_cvr >= BIZ.HIGH_CVR:
            out.append(_finding(
                "scale_winner", "opportunity", "campaign", cid, name,
                {"acos": c_acos, "cvr": c_cvr, "roas": c["roas"], "orders": orders},
                f"ACOS {c_acos:.1%} 远优于目标且转化率 {c_cvr:.1%} —— 这是本周的效率冠军。",
                "提竞价 10% 抢更多曝光，并同步确认库存能撑住（见库存页的可售天数）。",
                "ads",
            ))

    # 7) 自动投放 vs 手动精准：自动往往更贵，要把跑出来的词迁到手动
    auto = [c for c in m["by_campaign"] if "自动" in (c["targeting"] or "")]
    manual = [c for c in m["by_campaign"] if "手动" in (c["targeting"] or "")]
    if auto and manual:
        a_spend = sum(c["spend"] for c in auto)
        a_sales = sum(c["sales"] for c in auto)
        m_spend = sum(c["spend"] for c in manual)
        m_sales = sum(c["sales"] for c in manual)
        a_acos = a_spend / a_sales if a_sales > 0 else None
        m_acos = m_spend / m_sales if m_sales > 0 else None
        if a_acos is not None and m_acos is not None and a_acos > m_acos * 1.25:
            out.append(_finding(
                "auto_vs_manual", "opportunity", "account", "ALL", "投放方式对比",
                {"auto_acos": round(a_acos, 4), "manual_acos": round(m_acos, 4),
                 "auto_spend": round(a_spend, 2), "manual_spend": round(m_spend, 2)},
                f"自动投放 ACOS {a_acos:.1%}，比手动精准 {m_acos:.1%} 高出 "
                f"{(a_acos / m_acos - 1):.0%} —— 自动在替你花钱试错。",
                "把自动活动里出单的搜索词导出，迁到手动精准活动并单独定价；"
                "自动活动只留做新词发现，日预算压到能持续跑数据的最小值。",
                "ads", round(max(0.0, a_spend - a_sales * BIZ.TARGET_ACOS), 2),
            ))

    # ---------- SKU 层：广告卖得好但可能缺货 ----------
    from . import inventory as inv
    for s in m["by_sku"][:8]:
        if s["units"] <= 0:
            continue
        stock = inv.stock_of(s["sku"])
        vel = inv.sales_velocity(s["sku"], 14)
        if vel > 0 and stock["available"] / vel < BIZ.STOCKOUT_WARN_DAYS:
            days = round(stock["available"] / vel, 1)
            out.append(_finding(
                "ad_scale_vs_stockout", "critical", "sku", s["sku"], s.get("title_cn") or s["sku"],
                {"available": stock["available"], "velocity": vel, "available_days": days,
                 "ad_units": s["units"], "lead_time_days": stock["lead_time_days"]},
                f"广告本周出了 {s['units']} 单，但可用库存只够卖 {days} 天，而补货要 "
                f"{stock['lead_time_days']} 天 —— 加预算会直接变成断货。",
                f"先补货（建议补 {max(0, int(vel * (stock['lead_time_days'] + 15) - stock['available']))} 件），"
                "补货到仓前不要给这个 SKU 加广告预算，否则是花钱把自己的 listing 打到断货。",
                "inventory",
            ))

    order = {"critical": 0, "warning": 1, "opportunity": 2, "info": 3}
    out.sort(key=lambda f: (order.get(f["severity"], 9), -(f.get("impact_usd") or 0)))
    return out


def diagnostics_summary(findings: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "by_severity": counts,
        "total": len(findings),
        "waste_usd": round(sum(f.get("impact_usd") or 0 for f in findings
                               if f["severity"] in ("critical", "warning")), 2),
        "listing_actions": sum(1 for f in findings if f["feeds"] == "listing"),
        "ads_actions": sum(1 for f in findings if f["feeds"] == "ads"),
    }


def account_summary() -> dict:
    """后台首页的广告概览：近 7 天 vs 前 7 天。"""
    start, end = week_bounds(None)
    return weekly_metrics(start)
