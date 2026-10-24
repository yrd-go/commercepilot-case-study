# -*- coding: utf-8 -*-
"""现场验证（what-if）：改一处数据，看规则引擎是否立刻给出对应结论。

为什么需要这个模块
------------------
演示说明里有一句承诺："我可以现场改数据验证规则 —— 把一个活动的花费调高、
订单清零，'只花钱不出单'的结论立刻出现。"

**这句话在很长时间里只是说明，没有对应命令** —— 也就是说，评审时要么现场手写
SQL（难看且容易出错），要么这句承诺就是空的。这个模块把承诺变成一条命令：

    python run.py whatif --campaign C006 --spend 320 --orders 0

它做四件事：
1. 定位活动（支持 campaign_id 前缀 / 名称片段）；
2. 把**这一周**该活动的花费改成指定值、订单清零（点击量不足时补到规则门槛之上，
   否则"样本太小不下结论"这条规则会正确地拦住它）；
3. 重新算指标、跑规则引擎，打印"改前 / 改后"的对比，**指出是哪条规则新出现**；
4. 默认**自动还原**（`--keep` 可以留下改动，用于连续演示）。

为什么用"重灌广告数据"来还原：数据生成器是固定随机种子 + 以今天为锚点，
所以同一天内重灌出来的结果与改动前完全一致 —— 比手工备份/回滚更省事，也更可靠。
"""
from __future__ import annotations

from typing import Any

from . import config, db, seed
from .domain import ads as ads_domain


def _resolve_campaign(key: str) -> dict[str, Any]:
    """按 campaign_id / id 前缀 / 名称片段找活动。"""
    key = (key or "").strip()
    if not key:
        raise ValueError("请用 --campaign 指定一个活动（id 或名称片段）")

    rows = db.query(
        "SELECT campaign_id, name, sku, ad_type, targeting, daily_budget_usd FROM ad_campaigns"
    )
    low = key.lower()
    exact = [r for r in rows if r["campaign_id"].lower() == low]
    if exact:
        return exact[0]
    prefix = [r for r in rows if r["campaign_id"].lower().startswith(low)]
    if len(prefix) == 1:
        return prefix[0]
    by_name = [r for r in rows if low in (r["name"] or "").lower()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        names = "、".join(f"{r['campaign_id']}({r['name']})" for r in by_name[:6])
        raise ValueError(f"“{key}”匹配到多个活动，请写得更具体：{names}")
    raise ValueError(f"没找到活动“{key}”。可用：{[r['campaign_id'] for r in rows][:6]} …")


def _campaign_rows(campaign_id: str, start: str, end: str) -> list[dict]:
    return db.query(
        "SELECT date, impressions, clicks, spend_usd, orders, units, sales_usd "
        "FROM ad_daily WHERE campaign_id = ? AND date BETWEEN ? AND ? ORDER BY date",
        (campaign_id, start, end),
    )


def _week_metrics(week_start: str) -> dict[str, Any]:
    return ads_domain.weekly_metrics(week_start)


def _campaign_summary(metrics: dict, campaign_id: str) -> dict[str, Any]:
    # 注意：weekly_metrics 的键是 `by_campaign`（不是 campaigns）——
    # 写成 campaigns 会静默返回空字典，ACOS 一栏显示成 "—"，看起来像没有数据。
    for c in metrics.get("by_campaign") or []:
        if c.get("campaign_id") == campaign_id:
            return c
    return {}


def _rule_set(findings: list[dict], campaign_id: str) -> set[str]:
    return {f["rule"] for f in findings if f.get("subject") == campaign_id}


def _apply(campaign_id: str, start: str, end: str, spend: float | None,
           orders: int | None) -> dict[str, Any]:
    """把这一周该活动的花费/订单改成目标值。返回改动前后的对照。"""
    rows = _campaign_rows(campaign_id, start, end)
    if not rows:
        raise ValueError(f"{campaign_id} 在 {start} ~ {end} 没有投放数据")

    before = {
        "spend": round(sum(r["spend_usd"] or 0 for r in rows), 2),
        "clicks": int(sum(r["clicks"] or 0 for r in rows)),
        "orders": int(sum(r["orders"] or 0 for r in rows)),
        "sales": round(sum(r["sales_usd"] or 0 for r in rows), 2),
    }

    # 目标花费：按现有花费比例摊到 7 天；原花费为 0 时平均摊
    target_spend = float(spend) if spend is not None else before["spend"]
    base = before["spend"] or len(rows)
    weights = [(r["spend_usd"] or 0) / base if before["spend"] else 1 / len(rows) for r in rows]
    total_w = sum(weights) or 1
    weights = [w / total_w for w in weights]

    # 订单：给了就用给定值（通常 0），否则保持不变按天摊
    target_orders = int(orders) if orders is not None else before["orders"]
    per_day_orders = [0] * len(rows)
    for i in range(target_orders):
        per_day_orders[i % len(rows)] += 1

    # 点击量：rules 里 `MIN_CLICKS_FOR_JUDGE` 会拦住小样本，
    # 想让"只花钱不出单"这条结论成立，点击必须够。不够就按比例补。
    clicks_now = before["clicks"]
    min_clicks = config.Biz.MIN_CLICKS_FOR_JUDGE + 5
    click_scale = max(1.0, min_clicks / clicks_now) if clicks_now else 1.0

    for i, r in enumerate(rows):
        new_spend = round(target_spend * weights[i], 2)
        new_clicks = max(int(round((r["clicks"] or 0) * click_scale)), 1)
        new_orders = per_day_orders[i]
        # 订单为 0 时广告销售额必须为 0：不然会出现"0 单却有销售额"的不自洽数据
        new_sales = 0.0 if new_orders == 0 else round(
            (r["sales_usd"] or 0) * new_orders / max(r["orders"] or 1, 1), 2)
        new_units = 0 if new_orders == 0 else int(r["units"] or 0)
        db.execute(
            "UPDATE ad_daily SET spend_usd = ?, clicks = ?, orders = ?, sales_usd = ?, units = ? "
            "WHERE campaign_id = ? AND date = ?",
            (new_spend, new_clicks, new_orders, new_sales, new_units, campaign_id, r["date"]),
        )

    after = {
        "spend": round(sum(r["spend_usd"] or 0 for r in _campaign_rows(campaign_id, start, end)), 2),
        "clicks": int(sum(r["clicks"] or 0 for r in _campaign_rows(campaign_id, start, end))),
        "orders": int(sum(r["orders"] or 0 for r in _campaign_rows(campaign_id, start, end))),
        "sales": round(sum(r["sales_usd"] or 0 for r in _campaign_rows(campaign_id, start, end)), 2),
    }
    return {"before": before, "after": after, "days": len(rows)}


def whatif(campaign: str, spend: float | None = None, orders: int | None = None,
           week: str | None = None, keep: bool = False) -> dict[str, Any]:
    """改一处数据 → 重新诊断 → 报告"哪条规则被触发了"。默认自动还原。"""
    camp = _resolve_campaign(campaign)
    week_start = week or ads_domain.default_report_week()
    start, end = ads_domain.week_bounds(week_start)

    m_before = _week_metrics(week_start)
    f_before = ads_domain.diagnose(m_before)
    rules_before = _rule_set(f_before, camp["campaign_id"])
    sum_before = _campaign_summary(m_before, camp["campaign_id"])

    try:
        diff = _apply(camp["campaign_id"], start, end, spend, orders)


        m_after = _week_metrics(week_start)
        f_after = ads_domain.diagnose(m_after)
        rules_after = _rule_set(f_after, camp["campaign_id"])
        sum_after = _campaign_summary(m_after, camp["campaign_id"])
        new_findings = [f for f in f_after
                        if f.get("subject") == camp["campaign_id"]
                        and f["rule"] not in rules_before]
        gone_findings = [f for f in f_before
                         if f.get("subject") == camp["campaign_id"]
                         and f["rule"] not in rules_after]
    finally:
        if not keep:
            seed.reseed_ads()

    return {
        "campaign": camp,
        "week": week_start,
        "week_range": f"{start} ~ {end}",
        "days": diff["days"],
        "before": diff["before"],
        "after": diff["after"],
        "metrics_before": {
            "acos": sum_before.get("acos"), "clicks": sum_before.get("clicks"),
            "orders": sum_before.get("orders"), "cvr": sum_before.get("cvr"),
        },
        "metrics_after": {
            "acos": sum_after.get("acos"), "clicks": sum_after.get("clicks"),
            "orders": sum_after.get("orders"), "cvr": sum_after.get("cvr"),
        },
        "account_acos_before": m_before.get("total", {}).get("acos"),
        "account_acos_after": m_after.get("total", {}).get("acos"),
        "findings_before": len(f_before),
        "findings_after": len(f_after),
        "rules_before": sorted(rules_before),
        "rules_after": sorted(rules_after),
        "new_findings": new_findings,
        "gone_findings": gone_findings,
        "restored": not keep,
    }
