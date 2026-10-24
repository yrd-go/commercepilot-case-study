# -*- coding: utf-8 -*-
"""链路 2：广告数据周报自动生成。

完整链路
--------
    1. 从 DB 取一周广告事实数据
    2. 算指标（唯一口径在 domain/ads._metrics）→ 与上周环比
    3. 跑规则诊断引擎 → 带证据的 findings（严重度排序）
    4. RAG 检索品牌知识 + 合规/投放方法论 → 让建议符合品牌与平台约束
    5. LLM 写：执行摘要 + 归因 + 下周行动清单（结构化 JSON）
    6. matplotlib 出 5 张图
    7. 产出 Markdown 周报 + 自包含 HTML（图片内嵌，可直接转发）+ Excel 明细
    8. 运行档案留痕

一个刻意的设计：**报告不只有数字，还有"谁该干什么"**
------------------------------------------------------
每条 finding 都带 evidence / diagnosis / action / feeds 四段。
`feeds` 指这条结论该由哪条链路执行 —— 当 feeds=listing 时（比如"点击率崩了，是主图问题"），
链路 2 的结论会变成链路 1 的输入。两条链路就是这样闭合成环的，而不是两个孤立 demo。
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import config, db, llm, rag_bridge, runs
from ..domain import ads as ads_domain
from ..domain import inventory as inv_domain
from ..domain import orders as orders_domain
from ..render import charts as chart_render

SEVERITY_LABEL = {"critical": "严重", "warning": "预警", "opportunity": "机会", "info": "提示"}
SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "opportunity": "🟢", "info": "🔵"}
FEEDS_LABEL = {"listing": "主图/卖点链路（链路 1）", "ads": "投放操作", "inventory": "备货/供应链"}


# --------------------------------------------------------------------------
@dataclass
class WeeklyResult:
    week_start: str = ""
    week_label: str = ""
    ok: bool = False
    metrics: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    ai: dict = field(default_factory=dict)
    engine: str = ""
    degraded: bool = False
    rag_hits: int = 0
    rag_citations: list[str] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)
    report_md: str = ""
    report_html: str = ""
    report_xlsx: str = ""
    run_id: str = ""
    latency_ms: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "week_start": self.week_start, "week_label": self.week_label, "ok": self.ok,
            "engine": self.engine, "degraded": self.degraded, "rag_hits": self.rag_hits,
            "rag_citations": self.rag_citations, "charts": self.charts,
            "report_md": self.report_md, "report_html": self.report_html,
            "report_xlsx": self.report_xlsx, "run_id": self.run_id,
            "latency_ms": self.latency_ms, "note": self.note,
            "findings_count": len(self.findings), "ai": self.ai,
        }


# --------------------------------------------------------------------------
# 1~3) 采集指标 + 诊断
# --------------------------------------------------------------------------
def collect(week_start: str | None = None) -> tuple[dict, list[dict]]:
    start = week_start or ads_domain.default_report_week()
    metrics = ads_domain.weekly_metrics(start)
    findings = ads_domain.diagnose(metrics)
    return metrics, findings


def listing_targets(findings: Sequence[dict]) -> list[str]:
    """从诊断结论里挑出"该回头改内容"的 SKU —— 链路 2 给链路 1 的输入。"""
    out: list[str] = []
    for f in findings:
        if f.get("feeds") != "listing":
            continue
        sku = f.get("subject") if f.get("scope") == "sku" else None
        if not sku:
            sku = db.scalar("SELECT sku FROM ad_campaigns WHERE campaign_id = ?",
                            (f.get("subject"),))
        if sku and sku not in out:
            out.append(sku)
    return out


# --------------------------------------------------------------------------
# 4) 知识检索
# --------------------------------------------------------------------------
def rag_queries(metrics: dict, findings: list[dict]) -> list[str]:
    cats = [c for c in {c.get("category") for c in metrics.get("by_sku", [])} if c]
    queries = [
        "亚马逊广告 优化 ACOS 转化率 点击率 诊断方法",
        "合规与禁用词 绝对化用语 医疗功效 主图规范",
    ]
    for cat in cats[:2]:
        queries.append(f"{cat} 类目卖点 买家痛点 文案切入角度")
    if any(f["rule"] == "auto_vs_manual" for f in findings):
        queries.append("竞品对比 差异化打法 关键词库 搜索意图")
    return queries


def brand_context(metrics: dict, findings: list[dict], k: int = 3) -> rag_bridge.RagResult:
    merged = rag_bridge.RagResult(query=" | ".join(rag_queries(metrics, findings)))
    seen: set[str] = set()
    for q in rag_queries(metrics, findings):
        res = rag_bridge.search(q, k=k)
        if res.mode in ("unavailable", "keyword-only"):
            merged.mode = res.mode
            if res.note and not merged.note:
                merged.note = res.note
        for hit in res.hits:
            key = hit.content[:120]
            if key not in seen:
                seen.add(key)
                merged.hits.append(hit)
    merged.hits.sort(key=lambda h: -h.score)
    merged.pool_size = len(merged.hits)
    if merged.mode not in ("unavailable",) and not merged.hits:
        merged.mode = "empty"
    return merged


# --------------------------------------------------------------------------
# 5) AI 段落
# --------------------------------------------------------------------------
WEEKLY_SYSTEM = """你是跨境电商（亚马逊美国站）广告投放负责人，向老板汇报一周广告表现。
你的报告要满足五条：
1. 先给结论，再给证据。不要复述数字，要解释数字说明什么。
2. 每个判断都必须能对应到给定数据，不得引入数据里没有的事实。
3. 行动项要具体到"改什么、改多少、预期什么结果、谁负责"，不要写"持续优化"这种空话。
4. ★ 执行摘要**不许粉饰**。只要【规则引擎已识别的问题】里存在"严重"级问题，
   摘要就必须点名它们、并说清这周最该管的是什么。
   绝对不要用"整体稳健""表现良好"这类话盖过严重问题 —— 那正是老板最需要知道的部分。
5. ★ attributions 必须**逐条覆盖**提示词里每一个 [#编号]，一条都不能漏，
   数量必须与问题条数完全相等。漏掉的会被判定为不合格。
6. ★ attributions 要给**机制**，不是复述现象。
   反例（绝对不要这样写）："ACOS 超标且转化率低，说明流量不精准或详情页说服力不足。"
   —— 这只是把现象换个说法，没有任何新信息。
   正例："手动广泛匹配把 'nail drill' 这类泛词也放进来了，283 次点击里多数是找电动指甲锉的买家，
   落地页是美甲打磨机就跳走了，所以转化率只有 1.4%、ACOS 冲到 142%。"
   注意正例点出了**具体环节**（哪个匹配方式、哪个词、买家意图错位）并引用了给定数字。
只输出一个 JSON 对象，不要任何解释文字。"""

WEEKLY_SCHEMA = """{
  "executive_summary": "执行摘要，150-260 字，中文。先讲这周整体如何，再点出 2-3 个最该管的问题，最后给一句总体判断。",
  "attributions": [
    {"id": 1,
     "insight": "对应提示词里 [#1] 那条现象的**机制**，45-90 字。禁止复述现象本身；必须点出具体环节（哪种匹配方式/哪个词/主图还是详情页/价格还是评论/库存还是竞价），并引用至少一个给定数字。"}
  ],
  "action_plan": [
    {"priority": "P0/P1/P2", "owner": "投放/内容/供应链", "action": "具体动作，30-60 字",
     "expected_impact": "预期效果，20-40 字"}
  ]
}"""


def _similar(a: str, b: str) -> float:
    """两段文字的相似度，用来识别"归因只是把现象换个说法"。"""
    from difflib import SequenceMatcher
    a = " ".join((a or "").split())
    b = " ".join((b or "").split())
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def attributions_of(ai: dict, findings: list[dict]) -> dict[int, str]:
    """把模型返回的归因按**编号**映射回 findings。

    ★ 这里踩过一个会误导人的坑：最初按 `rule` 字段做键映射，
    但**多条 finding 会共用同一条规则**（比如 `high_acos_low_cvr` 同时命中
    "便携打磨机"和"专业套装"两个活动），字典一合并就只剩最后一条，
    于是"便携打磨机"的归因栏里写的是"专业套装"的原因 ——
    **张冠李戴的归因比没有归因更糟**，老板会照着错的结论去改。
    改成用提示词里的 `[#n]` 编号做唯一键。

    另外顺手过滤掉"只是把现象换个说法"的归因（相似度 > 0.72），
    这种内容占了篇幅但没有信息量，不如不显示。
    """
    raw = ai.get("attributions") or []
    by_id: dict[int, str] = {}
    by_rule: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        insight = " ".join(str(item.get("insight") or "").split())
        if not insight:
            continue
        key = str(item.get("id", "")).strip()
        if key.isdigit():
            by_id[int(key)] = insight
        elif item.get("rule"):
            by_rule[str(item["rule"])] = insight

    out: dict[int, str] = {}
    for idx, f in enumerate(findings, 1):
        insight = by_id.get(idx) or by_rule.get(f["rule"])
        if not insight:
            continue
        # 与"现象"高度重合 → 视为复述，不展示
        if _similar(insight, f["diagnosis"]) > 0.72 or insight in f["diagnosis"]:
            continue
        out[idx] = insight
    return out


def build_weekly_prompt(metrics: dict, findings: list[dict],
                        context: rag_bridge.RagResult, extras: dict) -> str:
    total, prev = metrics["total"], metrics["prev_total"]
    wow = metrics["wow"]

    def pct(v: float | None) -> str:
        return "—" if v is None else f"{v:.1%}"

    def wow_s(key: str) -> str:
        p = wow.get(key, {}).get("pct")
        return "—" if p is None else f"{p:+.0%}"

    lines = [
        f"报告周：{metrics['week_label']}（{metrics['week_start']} ~ {metrics['week_end']}）",
        f"对比周：{metrics['prev_week_start']} ~ {metrics['prev_week_end']}",
        "",
        "【账户总览】",
        f"花费 ${total['spend']:,.2f}（环比 {wow_s('spend')}）｜销售额 ${total['sales']:,.2f}（环比 {wow_s('sales')}）"
        f"｜订单 {total['orders']}（环比 {wow_s('orders')}）",
        f"ACOS {pct(total['acos'])}（目标 {metrics['target_acos']:.0%}）｜ROAS {total['roas']}｜"
        f"CTR {pct(total['ctr'])}（环比 {wow_s('ctr')}）｜CVR {pct(total['cvr'])}（环比 {wow_s('cvr')}）｜"
        f"CPC ${total['cpc']}（环比 {wow_s('cpc')}）",
        "",
        "【活动明细】",
    ]
    for c in metrics["by_campaign"]:
        lines.append(
            f"- {c['campaign_id']}｜{c['name']}｜{c['ad_type']}/{c['targeting']}｜"
            f"曝光 {c['impressions']}｜点击 {c['clicks']}｜花费 ${c['spend']:.2f}｜"
            f"订单 {c['orders']}｜销售 ${c['sales']:.2f}｜"
            f"CTR {pct(c['ctr'])}｜CVR {pct(c['cvr'])}｜CPC ${c['cpc']}｜"
            f"ACOS {pct(c['acos'])}｜预算利用率 {pct(c['budget_utilization'])}"
        )
    lines += ["", "【规则引擎已识别的问题（请为每条写一句归因，用 [#编号] 对应）】"]
    for i, f in enumerate(findings, 1):
        lines.append(
            f"- [#{i}] rule={f['rule']}｜{SEVERITY_LABEL.get(f['severity'], f['severity'])}｜"
            f"{f['subject_name']}｜现象：{f['diagnosis']}｜规则建议：{f['action']}｜"
            f"证据：{json.dumps(f['evidence'], ensure_ascii=False)}"
        )
    lines += [
        "",
        "【库存与广告的联动】",
        f"断货风险 SKU {extras['stockout_risk']} 个｜压货 SKU {extras['overstock']} 个｜"
        f"其中受广告放量影响最大的：{extras['ad_stockout_skus'] or '无'}",
        "",
        "【本周自然单（非广告）表现】",
        f"近 7 天全渠道营收 ${extras['sales7']['revenue']:,.0f}，"
        f"毛利 ${extras['sales7']['gross_profit']:,.0f}（毛利率 {extras['sales7']['gross_margin']:.1%}），"
        f"退款率 {extras['refund_rate']['rate']:.1%}",
        "",
        "【品牌知识与合规约束（检索自品牌知识库）】",
        context.context(max_chars=1800) or "（未检索到，按通用亚马逊广告最佳实践撰写）",
        "",
        f"请严格按此 JSON 结构输出（attributions 必须覆盖上面每一条 [#编号]，"
        f"id 就是方括号里的数字）：\n{WEEKLY_SCHEMA}",
    ]
    return "\n".join(lines)


def offline_sections(metrics: dict, findings: list[dict], extras: dict) -> dict:
    """无模型时的确定性摘要：直接由规则结论拼装，不编造新事实。"""
    total = metrics["total"]
    crit = [f for f in findings if f["severity"] == "critical"]
    warn = [f for f in findings if f["severity"] == "warning"]
    opp = [f for f in findings if f["severity"] == "opportunity"]
    acos_txt = "无销售" if total["acos"] is None else f"{total['acos']:.1%}"
    wow_spend = metrics["wow"]["spend"]["pct"]
    wow_sales = metrics["wow"]["sales"]["pct"]

    def wtxt(v: float | None) -> str:
        return "无对比基准" if v is None else f"环比 {v:+.0%}"

    summary_parts = [
        f"本周（{metrics['week_start']} ~ {metrics['week_end']}）广告花费 ${total['spend']:,.0f}"
        f"（{wtxt(wow_spend)}），广告销售额 ${total['sales']:,.0f}（{wtxt(wow_sales)}），"
        f"ACOS {acos_txt}，目标为 {metrics['target_acos']:.0%}。",
    ]
    if acos_txt != "无销售" and total["acos"] is not None:
        if total["acos"] > config.Biz.ACOS_CRITICAL:
            summary_parts.append("整体 ACOS 已超过红线，广告在亏钱换单，需要立刻处置高浪费活动。")
        elif total["acos"] > config.Biz.ACOS_WARN:
            summary_parts.append("整体 ACOS 高于目标但未失控，重点是把高 ACOS 活动的竞价压回来。")
        elif total["acos"] > config.Biz.TARGET_ACOS:
            # ★ 这里曾经漏了"略高于目标"这一档：只比红线(50%)和预警线(35%),
            #   于是 29.5% 这种"比目标 28% 高一点"的情况被写成"优于目标，重点转向放量" ——
            #   和一个真实的轻微超标说成相反的话。和账户层规则是同一个口径错误。
            summary_parts.append(
                f"整体 ACOS 略高于目标（{total['acos']:.1%} vs {config.Biz.TARGET_ACOS:.0%}），"
                "只需压住浪费最大的 1~2 个活动就能拉回目标线。")
        else:
            summary_parts.append("整体 ACOS 优于目标，本周重点应从省钱转向放量。")
    if crit:
        summary_parts.append("最需要处理的是：" + "；".join(
            f"{f['subject_name']}（{f['diagnosis'][:28]}）" for f in crit[:3]) + "。")
    summary_parts.append(
        f"规则引擎共识别 {len(findings)} 条问题：严重 {len(crit)}、预警 {len(warn)}、"
        f"机会 {len(opp)}；其中 {extras['listing_action_count']} 条需要回头修改主图与卖点文案。")
    if extras["ad_stockout_skus"]:
        summary_parts.append(
            f"另需注意：{extras['ad_stockout_skus']} 的广告在放量但库存即将断档，加预算会直接打成断货。")

    return {
        "executive_summary": " ".join(summary_parts),
        "attributions": [{"id": i, "rule": f["rule"], "insight": f["diagnosis"]}
                         for i, f in enumerate(findings[:10], 1)],
        "action_plan": [
            {"priority": "P0" if f["severity"] == "critical" else "P1",
             "owner": {"listing": "内容", "ads": "投放", "inventory": "供应链"}.get(f["feeds"], "投放"),
             "action": f["action"], "expected_impact": f"预计影响 ${f['impact_usd']:,.0f}"
             if f.get("impact_usd") else "降低浪费、提升转化"}
            for f in findings[:6]
        ],
        "_offline": True,
    }


def ai_sections(metrics: dict, findings: list[dict], context: rag_bridge.RagResult,
                extras: dict, engine: str | None, on_event: Callable[[str], None] | None = None) -> tuple[dict, Any]:
    """让模型写摘要 / 归因 / 行动清单，并对**归因覆盖度**做校验 + 一次补写。"""
    _log = on_event or (lambda _m: None)
    _log("生成执行摘要与行动清单…")
    prompt = build_weekly_prompt(metrics, findings, context, extras)
    call = llm.chat(prompt, system=WEEKLY_SYSTEM, engine=engine, temperature=0.35,
                    max_tokens=4000, json_mode=True, on_event=_log)
    parsed = llm.extract_json(call.text) if call.ok else None

    def _normalize(payload) -> dict | None:
        if not isinstance(payload, dict) or not ("executive_summary" in payload or "action_plan" in payload):
            return None
        return {
            "executive_summary": str(payload.get("executive_summary") or "").strip(),
            "attributions": payload.get("attributions") if isinstance(payload.get("attributions"), list) else [],
            "action_plan": payload.get("action_plan") if isinstance(payload.get("action_plan"), list) else [],
        }

    sections = _normalize(parsed)

    # ★ 覆盖度校验：模型经常只写 1~2 条归因就收工（实测发生过：15 条问题只回了 1 条）。
    #   少于一半时补一次，明确告诉它缺哪些编号。
    if sections and findings:
        have = {int(str(a.get("id")).strip()) for a in sections["attributions"]
                if isinstance(a, dict) and str(a.get("id", "")).strip().isdigit()}
        missing = [i for i in range(1, len(findings) + 1) if i not in have]
        if len(missing) > len(findings) * 0.5:
            _log(f"归因只覆盖 {len(have)}/{len(findings)} 条，补写一次…")
            retry_prompt = (
                prompt
                + "\n\n【上次输出不合格】你只写了 "
                + f"{len(have)} 条归因，必须写满 {len(findings)} 条。"
                + f"缺失的编号是：{', '.join('#%d' % i for i in missing)}。"
                + "请重新输出完整 JSON，attributions 数组必须包含以上每一个编号。"
            )
            call2 = llm.chat(retry_prompt, system=WEEKLY_SYSTEM, engine=engine,
                             temperature=0.3, max_tokens=4000, json_mode=True)
            better = _normalize(llm.extract_json(call2.text) if call2.ok else None)
            if better:
                have2 = {int(str(a.get("id")).strip()) for a in better["attributions"]
                         if isinstance(a, dict) and str(a.get("id", "")).strip().isdigit()}
                if len(have2) > len(have):
                    # 摘要仍优先用第一次的（第二次常因专注补归因而写薄摘要）
                    if not sections["executive_summary"]:
                        sections["executive_summary"] = better["executive_summary"]
                    sections["attributions"] = better["attributions"]
                    if not sections["action_plan"]:
                        sections["action_plan"] = better["action_plan"]
                    call = LLMResultShim(call, call2)
                    _log(f"补写后覆盖 {len(have2)}/{len(findings)} 条")

    if sections:
        if not sections["executive_summary"]:
            sections["executive_summary"] = offline_sections(metrics, findings, extras)["executive_summary"]
        if not sections["action_plan"]:
            sections["action_plan"] = offline_sections(metrics, findings, extras)["action_plan"]
        return sections, call

    _log("AI 段落生成失败，回退到规则驱动的确定性摘要")
    return offline_sections(metrics, findings, extras), call


class LLMResultShim:
    """把两次调用的用量/耗时合起来报，免得运行档案少算一次调用的开销。"""

    def __init__(self, first, second) -> None:
        self.engine = first.engine
        self.engine_label = first.engine_label
        self.ok = first.ok or second.ok
        self.note = first.note
        self.prompt_chars = first.prompt_chars + second.prompt_chars
        self.latency_ms = first.latency_ms + second.latency_ms
        self.tokens_in = first.tokens_in + second.tokens_in
        self.tokens_out = first.tokens_out + second.tokens_out


# --------------------------------------------------------------------------
# 6) 报告渲染
# --------------------------------------------------------------------------
def _fmt_pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _wow_cell(delta: dict) -> str:
    p = delta.get("pct")
    if p is None:
        return "—"
    arrow = "▲" if p > 0 else ("▼" if p < 0 else "＝")
    return f"{arrow} {p:+.1%}"


def render_markdown(metrics: dict, findings: list[dict], ai: dict,
                    chart_paths: dict[str, Path], extras: dict,
                    meta: dict) -> str:
    total, prev = metrics["total"], metrics["prev_total"]
    wow = metrics["wow"]
    L: list[str] = []

    L.append(f"# Saviland 亚马逊广告周报 · {metrics['week_label']}")
    L.append("")
    L.append(f"> 报告周期：**{metrics['week_start']} ~ {metrics['week_end']}**"
             f"　｜　对比周：{metrics['prev_week_start']} ~ {metrics['prev_week_end']}")
    # ★ 正脸只放"这份报告是给谁看的东西"：周期与生成时间。
    #   生成引擎、检索命中条数、耗时这些属于内部实现，读者不需要知道 ——
    #   它们只留在「运行档案」这个技术视图里，报告附录也不再出现。
    L.append(f"> 生成时间：{meta['generated_at']}")
    if meta.get("degraded"):
        L.append("> ⚠️ **本版为降级产物**：在线模型不可用，摘要由确定性规则引擎生成。")
    L.append("")
    L.append("---")
    L.append("")

    # 一、执行摘要
    L.append("## 一、执行摘要")
    L.append("")
    L.append(ai.get("executive_summary") or "（无）")
    L.append("")

    # 二、关键指标
    L.append("## 二、关键指标总览")
    L.append("")
    L.append("| 指标 | 本周 | 上周 | 环比 | 目标 / 基准 |")
    L.append("|---|---:|---:|---:|---|")
    rows = [
        ("广告花费", _fmt_money(total["spend"]), _fmt_money(prev["spend"]), _wow_cell(wow["spend"]), "—"),
        ("广告销售额", _fmt_money(total["sales"]), _fmt_money(prev["sales"]), _wow_cell(wow["sales"]), "—"),
        ("广告订单", f"{total['orders']}", f"{prev['orders']}", _wow_cell(wow["orders"]), "—"),
        ("ACOS", _fmt_pct(total["acos"]), _fmt_pct(prev["acos"]), _wow_cell(wow["acos"]),
         f"目标 ≤ {metrics['target_acos']:.0%}"),
        ("ROAS", f"{total['roas'] or '—'}", f"{prev['roas'] or '—'}", _wow_cell(wow["roas"]),
         f"目标 ≥ {metrics['target_roas']}"),
        ("CTR 点击率", _fmt_pct(total["ctr"], 2), _fmt_pct(prev["ctr"], 2), _wow_cell(wow["ctr"]),
         f"基准 ≥ {config.Biz.LOW_CTR:.2%}"),
        ("CVR 转化率", _fmt_pct(total["cvr"], 2), _fmt_pct(prev["cvr"], 2), _wow_cell(wow["cvr"]),
         f"基准 ≥ {config.Biz.LOW_CVR:.0%}"),
        ("CPC 单次点击成本", _fmt_money(total["cpc"]), _fmt_money(prev["cpc"]), _wow_cell(wow["cpc"]), "—"),
        ("曝光量", f"{total['impressions']:,}", f"{prev['impressions']:,}", _wow_cell(wow["impressions"]), "—"),
        ("点击量", f"{total['clicks']:,}", f"{prev['clicks']:,}", _wow_cell(wow["clicks"]), "—"),
    ]
    for r in rows:
        L.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")
    L.append("")
    if "daily_trend" in chart_paths:
        L.append(f"![日趋势]({chart_paths['daily_trend'].name})")
        L.append("")
        L.append("*图 1　日趋势：柱=广告花费，线=广告销售额。看「钱花出去了，销售有没有跟上」。*")
        L.append("")

    # 三、问题诊断
    L.append("## 三、问题诊断与处置建议")
    L.append("")
    diag = ads_domain.diagnostics_summary(findings)
    L.append(f"规则引擎共识别 **{diag['total']}** 条：🔴 严重 {diag['by_severity'].get('critical', 0)}　"
             f"🟠 预警 {diag['by_severity'].get('warning', 0)}　"
             f"🟢 机会 {diag['by_severity'].get('opportunity', 0)}　"
             f"🔵 提示 {diag['by_severity'].get('info', 0)}")
    L.append("")
    L.append(f"可归因的浪费金额（按目标 ACOS {metrics['target_acos']:.0%} 折算）："
             f"**{_fmt_money(diag['waste_usd'])}**　｜　"
             f"需回头改内容的问题：{diag['listing_actions']} 条　｜　"
             f"需调整投放的问题：{diag['ads_actions']} 条")
    L.append("")

    attr = attributions_of(ai, findings)
    for i, f in enumerate(findings, 1):
        icon = SEVERITY_ICON.get(f["severity"], "")
        L.append(f"### {i}. {icon} {f['subject_name']}　`{f['rule']}`")
        L.append("")
        L.append(f"- **现象**：{f['diagnosis']}")
        insight = attr.get(i)
        if insight:
            L.append(f"- **归因**：{insight}")
        L.append(f"- **证据**：`{json.dumps(f['evidence'], ensure_ascii=False)}`")
        L.append(f"- **建议动作**：{f['action']}")
        L.append(f"- **执行方**：{FEEDS_LABEL.get(f['feeds'], f['feeds'])}")
        if f.get("impact_usd"):
            L.append(f"- **预估影响金额**：{_fmt_money(f['impact_usd'])}")
        L.append("")

    # 四、活动明细
    L.append("## 四、活动明细排名")
    L.append("")
    L.append("| 活动 | 类型 | 曝光 | 点击 | CTR | 花费 | 订单 | 销售 | CVR | CPC | ACOS | 预算利用率 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in metrics["by_campaign"]:
        L.append(
            f"| {c['name']} | {c['ad_type']}/{c['targeting']} | {c['impressions']:,} | {c['clicks']:,} | "
            f"{_fmt_pct(c['ctr'], 2)} | {_fmt_money(c['spend'])} | {c['orders']} | {_fmt_money(c['sales'])} | "
            f"{_fmt_pct(c['cvr'], 2)} | {_fmt_money(c['cpc'])} | {_fmt_pct(c['acos'])} | "
            f"{_fmt_pct(c['budget_utilization'], 0)} |")
    L.append("")
    for key, cap in (("acos_by_campaign", "*图 2　各活动 ACOS 排名：绿=健康，黄=预警，红=超红线或零转化。*"),
                     ("spend_structure", "*图 3　广告花费结构：预算是否过度集中在少数活动上。*")):
        if key in chart_paths:
            L.append(f"![{key}]({chart_paths[key].name})")
            L.append("")
            L.append(cap)
            L.append("")

    # 五、环比与四象限
    L.append("## 五、环比变化与流量质量分诊")
    L.append("")
    for key, cap in (("wow_compare", "*图 4　本周 vs 上周：哪些指标在涨、哪些在跌。*"),
                     ("ctr_cvr_quadrant",
                      "*图 5　CTR × CVR 四象限：把「该改主图」和「该改详情页」分开"
                      "——这张图是链路 2 给链路 1 的分诊台。*")):
        if key in chart_paths:
            L.append(f"![{key}]({chart_paths[key].name})")
            L.append("")
            L.append(cap)
            L.append("")

    # 六、库存联动
    L.append("## 六、库存与广告的联动风险")
    L.append("")
    L.append(f"- 断货风险 SKU：**{extras['stockout_risk']}** 个　｜　压货 SKU：**{extras['overstock']}** 个")
    L.append(f"- 广告在放量但库存吃紧：**{extras['ad_stockout_skus'] or '无'}**")
    if extras["stock_alerts"]:
        L.append("")
        L.append("| SKU | 商品 | 可用库存 | 日均销量 | 可售天数 | 补货周期 | 建议补货 |")
        L.append("|---|---|---:|---:|---:|---:|---:|")
        for a in extras["stock_alerts"][:8]:
            days = "∞" if a["available_days"] == float("inf") else f"{a['available_days']:.0f} 天"
            L.append(f"| {a['sku']} | {a['title_cn']} | {a['available']} | {a['velocity']} | "
                     f"{days} | {a['lead_time_days']} 天 | {a['suggest_qty']} |")
    L.append("")

    # 七、行动清单
    L.append("## 七、下周行动清单")
    L.append("")
    for a in (ai.get("action_plan") or []):
        if not isinstance(a, dict):
            continue
        L.append(f"- [ ] **{a.get('priority', 'P1')}**（{a.get('owner', '投放')}）{a.get('action', '')}"
                 f"　→ *{a.get('expected_impact', '')}*")
    if not (ai.get("action_plan") or []):
        L.append("- （未生成行动项）")
    L.append("")

    # 八、闭环：把结论送回链路 1
    L.append("## 八、链路闭环：本周需要重写文案的 SKU")
    L.append("")
    targets = extras["listing_targets"]
    if targets:
        L.append("以下 SKU 的广告问题**根因在内容侧**（主图点击率或详情页转化率），"
                 "已作为输入交给链路 1（主图/卖点批量生成）重写：")
        L.append("")
        for sku in targets:
            row = db.query_one("SELECT title_cn, category FROM products WHERE sku = ?", (sku,))
            L.append(f"- `{sku}`　{row['title_cn'] if row else ''}　"
                     f"（{row['category'] if row else ''}）")
        L.append("")
        L.append("> 复现命令：`python -m saviland.cli listing --skus " + ",".join(targets) + "`")
    else:
        L.append("本周没有「根因在内容侧」的广告问题，链路 1 无需介入。")
    L.append("")

    # 附录（口径说明）：只写读者核对数字需要的口径，不写实现细节
    L.append("---")
    L.append("")
    L.append("## 附录：数据口径")
    L.append("")
    L.append("| 项目 | 值 |")
    L.append("|---|---|")
    L.append(f"| 广告数据来源 | {meta['data_source']} |")
    L.append(f"| 数据窗口 | {metrics['week_start']} ~ {metrics['week_end']}（{len(metrics['daily'])} 天）|")
    L.append(f"| 参与统计的活动 | {len(metrics['by_campaign'])} 个 |")
    L.append(f"| 指标口径 | ACOS=花费/广告销售额；CVR=订单/点击；CTR=点击/曝光；CPC=花费/点击 |")
    rule_count = len({f["rule"] for f in findings})
    L.append(f"| 诊断规则 | 命中 {rule_count} 条规则；点击量 < "
             f"{config.Biz.MIN_CLICKS_FOR_JUDGE} 不下转化结论（避免小样本噪声）|")
    L.append(f"| 参考的品牌知识 | {', '.join(meta['rag_citations']) or '无'} |")
    L.append(f"| 报告编号 | `{meta['run_id']}` |")
    L.append("")
    L.append("<sub>本报告由 Saviland 电商运营台自动生成；建议与行动项来自规则引擎与品牌知识库，"
             "数字口径见上表。</sub>")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 6) 报告渲染（HTML）
#   具体实现已拆到 `report_html.py`：它只做表现层，不碰指标与规则。
#   这里保留同名入口，调用方（server / cli / 测试）不需要改。
# --------------------------------------------------------------------------
def render_html(md_text: str, chart_paths: dict[str, Path], metrics: dict,
                findings: list[dict], ai: dict, extras: dict, meta: dict,
                embed_images: bool = True) -> str:
    """把周报渲染成自包含 HTML（图片 base64 内嵌，单文件可直接转发）。"""
    from . import report_html
    return report_html.render(md_text, chart_paths, metrics, findings, ai, extras,
                              meta, embed_images=embed_images)


# --------------------------------------------------------------------------
# 7) Excel 导出
# --------------------------------------------------------------------------
def export_xlsx(metrics: dict, findings: list[dict], extras: dict,
                path: Path | None = None) -> Path:
    """导出多 sheet Excel —— 运营真正会拿去用的形态。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="2F4858")
    head_font = Font(color="FFFFFF", bold=True)

    def style(ws, widths: Sequence[int]) -> None:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "账户总览"
    ws.append(["指标", "本周", "上周", "环比"])
    t, p, wow = metrics["total"], metrics["prev_total"], metrics["wow"]
    for label, key, fmt in (("广告花费", "spend", "money"), ("广告销售额", "sales", "money"),
                            ("广告订单", "orders", "int"), ("曝光量", "impressions", "int"),
                            ("点击量", "clicks", "int"), ("CTR", "ctr", "pct"),
                            ("CVR", "cvr", "pct"), ("CPC", "cpc", "money"),
                            ("ACOS", "acos", "pct"), ("ROAS", "roas", "num")):
        fmt_v = (lambda v: (v if v is not None else "—")) if fmt in ("money", "num") else \
                (lambda v: (v if v is not None else "—"))
        ws.append([label, fmt_v(t.get(key)), fmt_v(p.get(key)), _wow_cell(wow.get(key, {}))])
    style(ws, [22, 16, 16, 14])

    ws = wb.create_sheet("活动明细")
    ws.append(["活动ID", "活动名", "SKU", "广告类型", "投放方式", "曝光", "点击", "CTR",
               "花费", "订单", "销售", "CVR", "CPC", "ACOS", "日预算", "预算利用率"])
    for c in metrics["by_campaign"]:
        ws.append([c["campaign_id"], c["name"], c["sku"], c["ad_type"], c["targeting"],
                   c["impressions"], c["clicks"], c["ctr"], c["spend"], c["orders"], c["sales"],
                   c["cvr"], c["cpc"], c["acos"], c["daily_budget_usd"], c["budget_utilization"]])
    style(ws, [16, 40, 16, 10, 16, 10, 10, 10, 12, 8, 12, 10, 10, 10, 12, 12])

    ws = wb.create_sheet("SKU明细")
    ws.append(["SKU", "商品", "类目", "曝光", "点击", "CTR", "花费", "订单", "销售",
               "CVR", "CPC", "ACOS", "ROAS"])
    for s in metrics["by_sku"]:
        ws.append([s["sku"], s.get("title_cn"), s.get("category"), s["impressions"], s["clicks"],
                   s["ctr"], s["spend"], s["orders"], s["sales"], s["cvr"], s["cpc"],
                   s["acos"], s["roas"]])
    style(ws, [16, 34, 14, 10, 10, 10, 12, 8, 12, 10, 10, 10, 10])

    ws = wb.create_sheet("每日趋势")
    ws.append(["日期", "曝光", "点击", "花费", "订单", "销售", "CTR", "CVR", "ACOS"])
    for d in metrics["daily"]:
        ws.append([d["date"], d["impressions"], d["clicks"], d["spend"], d["orders"],
                   d["sales"], d["ctr"], d["cvr"], d["acos"]])
    style(ws, [14, 10, 10, 12, 8, 12, 10, 10, 10])

    ws = wb.create_sheet("诊断清单")
    ws.append(["序号", "严重度", "规则", "对象", "现象", "归因/证据", "建议动作",
               "执行方", "预估影响(美元)"])
    for i, f in enumerate(findings, 1):
        ws.append([i, SEVERITY_LABEL.get(f["severity"], f["severity"]), f["rule"],
                   f["subject_name"], f["diagnosis"],
                   json.dumps(f["evidence"], ensure_ascii=False), f["action"],
                   FEEDS_LABEL.get(f["feeds"], f["feeds"]), f.get("impact_usd")])
    style(ws, [6, 10, 24, 34, 60, 46, 70, 22, 16])

    ws = wb.create_sheet("行动清单")
    ws.append(["优先级", "负责方", "动作", "预期效果"])
    for a in (extras.get("action_plan") or []):
        ws.append([a.get("priority"), a.get("owner"), a.get("action"), a.get("expected_impact")])
    style(ws, [10, 12, 70, 34])

    out = path or (config.OUTPUTS_DIR / "weekly_reports"
                   / f"weekly_{metrics['week_label']}.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def gather_extras(metrics: dict, findings: list[dict]) -> dict:
    advice = inv_domain.restock_advice()
    stockout = [a for a in advice if a["level"] == "stockout_risk"]
    overstock = [a for a in advice if a["level"] == "overstock"]
    targets = listing_targets(findings)
    ad_stockout = [f["subject"] for f in findings if f["rule"] == "ad_scale_vs_stockout"]
    sales7 = orders_domain.sales_summary(7)
    return {
        "stockout_risk": len(stockout),
        "overstock": len(overstock),
        "stock_alerts": sorted(advice, key=lambda a: (a["available_days"] if a["available_days"] != float("inf") else 9e9))[:10],
        "listing_targets": targets,
        "ad_stockout_skus": "、".join(ad_stockout),
        "listing_action_count": sum(1 for f in findings if f["feeds"] == "listing"),
        "sales7": sales7,
        "refund_rate": orders_domain.refund_rate(30),
    }


def generate_weekly(
    week_start: str | None = None,
    engine: str | None = None,
    use_rag: bool = True,
    on_event: Callable[[str], None] | None = None,
) -> WeeklyResult:
    """生成一份完整周报（Markdown + HTML + Excel + 图表）。"""
    t0 = time.time()
    _log = on_event or (lambda _m: None)
    res = WeeklyResult()
    _log("聚合广告指标…")
    metrics, findings = collect(week_start)
    res.metrics, res.findings = metrics, findings
    res.week_start = metrics["week_start"]
    res.week_label = metrics["week_label"]
    if not metrics["by_campaign"]:
        res.note = f"{res.week_label} 没有任何广告数据，无法出报告"
        return res

    run_id = runs.start_run("weekly", target=res.week_label, engine=engine or config.DEFAULT_ENGINE)
    res.run_id = run_id

    _log("汇总库存与销售联动数据…")
    extras = gather_extras(metrics, findings)

    context = rag_bridge.RagResult(query="", mode="empty")
    if use_rag:
        _log("检索品牌知识与投放方法论…")
        context = brand_context(metrics, findings)
        res.rag_hits = len(context.hits)
        res.rag_citations = context.citations()

    ai, call = ai_sections(metrics, findings, context, extras, engine, on_event=on_event)
    res.ai = ai
    res.engine = call.engine_label
    res.degraded = not call.ok
    res.note = call.note

    _log("绘制图表…")
    chart_paths = chart_render.build_all(metrics)
    chart_errors = chart_paths.pop("_errors", {})  # type: ignore[arg-type]
    res.charts = [str(p) for p in chart_paths.values()]

    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "engine": res.engine,
        "rag_hits": res.rag_hits,
        "rag_citations": res.rag_citations,
        "run_id": run_id,
        "degraded": res.degraded,
        "latency_ms": int((time.time() - t0) * 1000),
        "data_source": "本地仿真数据（结构对齐亚马逊广告后台导出；可用 import_ads_csv 换成真实报表）",
    }

    _log("渲染 Markdown / HTML 周报…")
    md = render_markdown(metrics, findings, ai, chart_paths, extras, meta)
    html_out = render_html(md, chart_paths, metrics, findings, ai, extras, meta)

    out_dir = config.OUTPUTS_DIR / "weekly_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"weekly_{res.week_label}.md"
    html_path = out_dir / f"weekly_{res.week_label}.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_out, encoding="utf-8")

    extras["action_plan"] = ai.get("action_plan") or []
    xlsx_path = export_xlsx(metrics, findings, extras, out_dir / f"weekly_{res.week_label}.xlsx")

    # 把 AI 的**原始 JSON** 也存一份：报告里只显示通过质量过滤的归因，
    # 存档原始返回才说得清"哪些被过滤了、为什么"。这在被质疑"归因是不是编的"时很有用。
    ai_path = out_dir / f"ai_{res.week_label}.json"
    ai_path.write_text(json.dumps({
        "week": res.week_label, "engine": res.engine, "degraded": res.degraded,
        "raw": ai,
        "attributions_shown": attributions_of(ai, findings),
        "attributions_filtered_out": [
            i for i in range(1, len(findings) + 1) if i not in attributions_of(ai, findings)
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 图表与诊断清单单独落一份，方便直接引用
    (out_dir / f"findings_{res.week_label}.json").write_text(
        json.dumps({"week": res.week_label, "findings": findings,
                    "summary": ads_domain.diagnostics_summary(findings)},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    res.report_md = str(md_path)
    res.report_html = str(html_path)
    res.report_xlsx = str(xlsx_path)
    res.latency_ms = int((time.time() - t0) * 1000)

    for kind, p in (("weekly_report", md_path), ("weekly_report_html", html_path),
                    ("weekly_export", xlsx_path), ("weekly_ai_raw", ai_path)):
        runs.add_artifact(run_id, kind, p, {"week": res.week_label})
    for name, p in chart_paths.items():
        runs.add_artifact(run_id, "chart", p, {"name": name})

    runs.finish_run(
        run_id, engine=res.engine, rag_hits=res.rag_hits, latency_ms=res.latency_ms,
        prompt_chars=call.prompt_chars,
        status="degraded" if res.degraded else "ok",
        note=(res.note[:200] if res.note else "") +
             (f"；图表失败 {len(chart_errors)} 张" if chart_errors else ""),
    )
    res.ok = True
    return res
