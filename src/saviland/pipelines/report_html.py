# -*- coding: utf-8 -*-
"""周报的自包含 HTML 渲染。

从 `weekly.py` 里拆出来的原因：那个文件已经一千多行，混着指标、诊断、提示词和
两套渲染（Markdown / HTML）。渲染是**纯表现层**，与业务逻辑无关，独立出来之后
两边都能单独改：会改样式的人不需要读诊断规则。

设计上与运营台（`web/static`）保持同一套视觉语言：暖纸底、深墨字、深玫瑰 + 香槟金，
卡片靠 1px 细边而不是阴影堆叠。报告要能直接转发给不看后台的人，所以：

  · 图片全部 base64 内嵌 —— 单个 .html 文件发过去就能看，不会图裂
  · 自带 `@media print` —— 打印/导出 PDF 时去掉背景与圆角，A4 下不出血
  · **不出现任何实现细节** —— 引擎名、耗时、prompt 长度都不在报告里，
    读者只看到业务结论与口径
"""
from __future__ import annotations

import base64
import html as _html
import json
from pathlib import Path
from typing import Any

SEVERITY_LABEL = {"critical": "严重", "warning": "预警", "opportunity": "机会", "info": "提示"}
FEEDS_LABEL = {"listing": "内容侧（改主图与五点描述）", "ads": "投放侧（竞价与预算）",
               "inventory": "供应链（补货）"}


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _money0(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _wow(delta: dict) -> str:
    p = (delta or {}).get("pct")
    if p is None:
        return '<span class="d flat">—</span>'
    cls = "up" if p > 0 else ("down" if p < 0 else "flat")
    arrow = "▲" if p > 0 else ("▼" if p < 0 else "＝")
    return f'<span class="d {cls}">{arrow} {p:+.1%}</span>'


CSS = """
:root{
  --bg:#F7F4F1; --surface:#fff; --surface-2:#FBF9F7; --ink:#17151A; --ink-2:#3D3A42;
  --muted:#7A7480; --faint:#A39CA6; --line:#E8E2DC; --brand:#8E2F4F; --brand-2:#B24B6B;
  --brand-soft:#F6E7EC; --gold:#9A7B3F; --gold-soft:#F5EEDF;
  --good:#2E7D5B; --good-soft:#E7F3EC; --warn:#A96A12; --warn-soft:#FBF0DC;
  --bad:#AE2A22; --bad-soft:#FAE7E5; --info:#3F5D8A; --info-soft:#E9EEF7;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.72 "Segoe UI",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:34px 26px 90px}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 10px;line-height:1.22}
h2{font-size:19px;margin:0 0 16px;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
h2::before{content:"";width:3px;height:18px;background:linear-gradient(var(--brand),var(--gold));border-radius:2px}
h3{font-size:15px;margin:0 0 8px}
a{color:var(--brand);text-decoration:none}
code{background:var(--surface-2);border:1px solid var(--line);border-radius:5px;padding:1px 6px;
  font-family:"Cascadia Mono",Consolas,monospace;font-size:12.5px;color:var(--ink-2)}

/* 头部：品牌带 + 标题 + 元信息 */
.hero{background:linear-gradient(135deg,#2B1F24 0%,#4A2735 52%,#7C3A51 100%);
  color:#fff;border-radius:18px;padding:30px 34px 26px;margin-bottom:22px;
  box-shadow:0 18px 44px -28px rgba(43,31,36,.9)}
.hero .mark{font-size:12px;letter-spacing:.32em;text-transform:uppercase;color:#E6CBD4;margin-bottom:14px}
.hero h1{color:#fff}
.hero .chip{display:inline-block;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.24);
  border-radius:999px;padding:3px 12px;font-size:12.5px;margin-left:10px;vertical-align:4px;
  letter-spacing:.02em;color:#F6E7EC}
.hero .meta{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:14px;font-size:13px;color:#DCC3CC}
.hero .meta b{color:#fff;font-weight:600}

/* 区块 */
section{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:24px 26px;margin-bottom:18px}
.lede{font-size:15px;color:var(--ink-2);margin:0}
.lede strong{color:var(--ink)}

/* KPI 条 */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:12px;margin-bottom:18px}
.kpi{border:1px solid var(--line);border-radius:12px;padding:13px 15px;background:var(--surface-2);
  position:relative;overflow:hidden}
.kpi::after{content:"";position:absolute;inset:0 0 auto 0;height:2px;
  background:linear-gradient(90deg,var(--brand),var(--gold));opacity:.6}
.kpi .k{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);font-weight:600}
.kpi .v{font-size:22px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums;margin-top:3px}
.kpi .m{font-size:11.5px;color:var(--muted);margin-top:2px}
.d{font-weight:600;font-variant-numeric:tabular-nums}
.d.up{color:var(--good)} .d.down{color:var(--bad)} .d.flat{color:var(--muted)}

/* 表格 */
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:4px 0}
th{text-align:left;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);
  font-weight:600;padding:9px 10px;background:var(--surface-2);border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover{background:var(--surface-2)}

/* 诊断卡片 */
.f{border:1px solid var(--line);border-left:3px solid var(--line);border-radius:11px;
  padding:15px 18px;margin-bottom:12px;background:var(--surface)}
.f.critical{border-left-color:var(--bad)} .f.warning{border-left-color:var(--warn)}
.f.opportunity{border-left-color:var(--good)} .f.info{border-left-color:var(--info)}
.f .h{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:7px}
.f .t{font-weight:650;font-size:14.5px}
.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;
  font-size:11.5px;font-weight:600;border:1px solid transparent;white-space:nowrap}
.pill.critical{background:var(--bad-soft);color:var(--bad);border-color:#E7C3C0}
.pill.warning{background:var(--warn-soft);color:var(--warn);border-color:#EBD5AC}
.pill.opportunity{background:var(--good-soft);color:var(--good);border-color:#BEDCCB}
.pill.info{background:var(--info-soft);color:var(--info);border-color:#C6D3E6}
.pill.neutral{background:var(--surface-2);color:var(--muted);border-color:var(--line)}
.f .line{font-size:13.5px;color:var(--ink-2);margin:3px 0}
.f .line b{color:var(--ink);font-weight:650}
.f .ev{font-family:"Cascadia Mono",Consolas,monospace;font-size:11.5px;color:var(--muted);
  background:var(--surface-2);border:1px solid var(--line);border-radius:7px;padding:7px 10px;
  margin:7px 0;overflow-x:auto;white-space:pre-wrap;word-break:break-word}
.f .foot{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}

/* 图 */
figure{margin:14px 0 6px}
figure img{width:100%;border:1px solid var(--line);border-radius:10px;display:block}
figcaption{font-size:12px;color:var(--muted);margin-top:8px}

/* 行动清单 */
ul.plan{list-style:none;padding:0;margin:0}
ul.plan li{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px dashed var(--line)}
ul.plan li:last-child{border-bottom:none}
.pri{flex:none;min-width:36px;text-align:center;border-radius:5px;padding:1px 8px;font-size:11.5px;
  font-weight:700;background:var(--surface-2);color:var(--muted);border:1px solid var(--line)}
.pri.P0{background:var(--bad-soft);color:var(--bad);border-color:#E7C3C0}
.pri.P1{background:var(--warn-soft);color:var(--warn);border-color:#EBD5AC}
.plan .who{color:var(--muted);font-size:12.5px}
.plan .imp{color:var(--good);font-size:12.5px}

.note{font-size:12.5px;color:var(--muted);margin-top:10px;line-height:1.7}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
footer{text-align:center;color:var(--faint);font-size:12px;padding:22px 0 0}
details{margin-top:12px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
pre{white-space:pre-wrap;font-size:12px;background:var(--surface-2);border:1px solid var(--line);
  border-radius:10px;padding:16px;overflow-x:auto}

@media (max-width:760px){.two{grid-template-columns:1fr}.wrap{padding:20px 14px 60px}
  .hero{padding:22px 20px}section{padding:18px 16px}h1{font-size:23px}}
@media print{
  body{background:#fff}
  .wrap{max-width:none;padding:0}
  section,.kpi,.f{box-shadow:none;break-inside:avoid}
  .hero{box-shadow:none;border-radius:0}
  figure img{border:none}
  section{page-break-inside:avoid}
}
"""


def _img(key: str, chart_paths: dict[str, Path], caption: str, embed: bool) -> str:
    path = chart_paths.get(key)
    if not path or not Path(path).is_file():
        return ""
    if embed:
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        src = f"data:image/png;base64,{data}"
    else:
        src = Path(path).name
    return f'<figure><img src="{src}" alt="{_html.escape(caption)}">'\
           f'<figcaption>{_html.escape(caption)}</figcaption></figure>'


def render(md_text: str, chart_paths: dict[str, Path], metrics: dict, findings: list[dict],
           ai: dict, extras: dict, meta: dict, embed_images: bool = True) -> str:
    total, prev = metrics["total"], metrics["prev_total"]
    wow = metrics["wow"]
    attributions = {a.get("rule"): a.get("insight") for a in (ai.get("attributions") or [])
                    if isinstance(a, dict)}
    diag_waste = sum(f.get("impact_usd") or 0 for f in findings
                     if f["severity"] in ("critical", "warning"))
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1

    kpis = "".join([
        f'<div class="kpi"><div class="k">广告花费</div><div class="v">{_money0(total["spend"])}</div>'
        f'<div class="m">{_wow(wow["spend"])} 环比</div></div>',
        f'<div class="kpi"><div class="k">广告销售额</div><div class="v">{_money0(total["sales"])}</div>'
        f'<div class="m">{_wow(wow["sales"])} 环比</div></div>',
        f'<div class="kpi"><div class="k">ACOS</div><div class="v">{_pct(total["acos"])}</div>'
        f'<div class="m">目标 ≤ {metrics["target_acos"]:.0%}</div></div>',
        f'<div class="kpi"><div class="k">ROAS</div><div class="v">{total["roas"] or "—"}</div>'
        f'<div class="m">目标 ≥ {metrics["target_roas"]}</div></div>',
        f'<div class="kpi"><div class="k">CTR 点击率</div><div class="v">{_pct(total["ctr"], 2)}</div>'
        f'<div class="m">{_wow(wow["ctr"])} 环比</div></div>',
        f'<div class="kpi"><div class="k">CVR 转化率</div><div class="v">{_pct(total["cvr"], 2)}</div>'
        f'<div class="m">{_wow(wow["cvr"])} 环比</div></div>',
    ])

    metric_rows = [
        ("广告花费", _money(total["spend"]), _money(prev["spend"]), _wow(wow["spend"]), "—"),
        ("广告销售额", _money(total["sales"]), _money(prev["sales"]), _wow(wow["sales"]), "—"),
        ("广告订单", f"{total['orders']}", f"{prev['orders']}", _wow(wow["orders"]), "—"),
        ("ACOS", _pct(total["acos"]), _pct(prev["acos"]), _wow(wow["acos"]),
         f"目标 ≤ {metrics['target_acos']:.0%}"),
        ("ROAS", f"{total['roas'] or '—'}", f"{prev['roas'] or '—'}", _wow(wow["roas"]),
         f"目标 ≥ {metrics['target_roas']}"),
        ("CTR 点击率", _pct(total["ctr"], 2), _pct(prev["ctr"], 2), _wow(wow["ctr"]),
         f"基准 ≥ {0.0015:.2%}"),
        ("CVR 转化率", _pct(total["cvr"], 2), _pct(prev["cvr"], 2), _wow(wow["cvr"]),
         "基准 ≥ 4.0%"),
        ("CPC 单次点击成本", _money(total["cpc"]), _money(prev["cpc"]), _wow(wow["cpc"]), "—"),
        ("曝光量", f"{total['impressions']:,}", f"{prev['impressions']:,}", _wow(wow["impressions"]), "—"),
        ("点击量", f"{total['clicks']:,}", f"{prev['clicks']:,}", _wow(wow["clicks"]), "—"),
    ]
    metric_table = ("<table><thead><tr><th>指标</th><th class='num'>本周</th><th class='num'>上周</th>"
                    "<th class='num'>环比</th><th>目标 / 基准</th></tr></thead><tbody>"
                    + "".join(f"<tr><td>{a}</td><td class='num'>{b}</td><td class='num'>{c}</td>"
                              f"<td class='num'>{d}</td><td>{e}</td></tr>"
                              for a, b, c, d, e in metric_rows) + "</tbody></table>")

    camp_table = ("<table><thead><tr><th>活动</th><th>类型</th><th class='num'>曝光</th>"
                  "<th class='num'>点击</th><th class='num'>CTR</th><th class='num'>花费</th>"
                  "<th class='num'>订单</th><th class='num'>销售</th><th class='num'>CVR</th>"
                  "<th class='num'>CPC</th><th class='num'>ACOS</th><th class='num'>预算用</th>"
                  "</tr></thead><tbody>"
                  + "".join(
                      f"<tr><td>{_html.escape(c['name'])}</td>"
                      f"<td class='muted'>{c['ad_type']}/{_html.escape(str(c['targeting']))}</td>"
                      f"<td class='num'>{c['impressions']:,}</td><td class='num'>{c['clicks']:,}</td>"
                      f"<td class='num'>{_pct(c['ctr'], 2)}</td><td class='num'>{_money(c['spend'])}</td>"
                      f"<td class='num'>{c['orders']}</td><td class='num'>{_money(c['sales'])}</td>"
                      f"<td class='num'>{_pct(c['cvr'], 2)}</td><td class='num'>{_money(c['cpc'])}</td>"
                      f"<td class='num'>{_pct(c['acos'])}</td>"
                      f"<td class='num'>{_pct(c['budget_utilization'], 0)}</td></tr>"
                      for c in metrics["by_campaign"]) + "</tbody></table>")

    cards = []
    for i, f in enumerate(findings, 1):
        insight = attributions.get(f["rule"])
        extra = (f'<div class="line"><b>归因 · </b>{_html.escape(str(insight))}</div>'
                 if insight and insight != f["diagnosis"] else "")
        impact = (f'<span>可归因金额 · {_money0(f["impact_usd"])}</span>'
                  if f.get("impact_usd") else "")
        cards.append(
            f'<div class="f {f["severity"]}">'
            f'<div class="h"><span class="pill {f["severity"]}">{SEVERITY_LABEL.get(f["severity"], f["severity"])}</span>'
            f'<span class="t">{_html.escape(f["subject_name"])}</span>'
            f'<code>{f["rule"]}</code></div>'
            f'<div class="line"><b>现象 · </b>{_html.escape(f["diagnosis"])}</div>'
            f'{extra}'
            f'<div class="line"><b>动作 · </b>{_html.escape(f["action"])}</div>'
            f'<div class="ev">{_html.escape(json.dumps(f["evidence"], ensure_ascii=False))}</div>'
            f'<div class="foot"><span>执行方 · {FEEDS_LABEL.get(f["feeds"], f["feeds"])}</span>{impact}</div>'
            f'</div>')

    plan = "".join(
        f'<li><span class="pri {a.get("priority", "P1")}">{a.get("priority", "P1")}</span>'
        f'<div style="flex:1"><div>{_html.escape(str(a.get("action", "")))}</div>'
        f'<div class="who">{_html.escape(str(a.get("owner", "投放")))}　'
        f'<span class="imp">→ {_html.escape(str(a.get("expected_impact", "")))}</span></div></div></li>'
        for a in (ai.get("action_plan") or []) if isinstance(a, dict)) \
        or "<li><span class='who'>本次未生成行动项</span></li>"

    def _days(a: dict) -> str:
        d = a["available_days"]
        return "∞（无动销）" if d == float("inf") else f"{d:.0f} 天"

    stock_rows = "".join(
        f"<tr><td>{a['sku']}</td><td>{_html.escape(a['title_cn'])}</td>"
        f"<td class='num'>{a['available']}</td><td class='num'>{a['velocity']}</td>"
        f"<td class='num'>{_days(a)}</td>"
        f"<td class='num'>{a['lead_time_days']} 天</td><td class='num'>{a['suggest_qty']}</td></tr>"
        for a in extras["stock_alerts"][:8])

    targets = extras["listing_targets"]
    targets_html = ("<p class='note'>以下 SKU 的广告问题<b>根因在内容侧</b>"
                    "（主图点击率或详情页转化率），已作为输入交给主图与卖点生成：</p><ul>"
                    + "".join(f"<li><code>{s}</code></li>" for s in targets) + "</ul>") \
        if targets else "<p class='note'>本周没有「根因在内容侧」的问题，内容侧无需介入。</p>"

    hero_meta = (
        f'<div class="meta">'
        f'<span>报告周期 <b>{metrics["week_start"]} ~ {metrics["week_end"]}</b></span>'
        f'<span>对比周 <b>{metrics["prev_week_start"]} ~ {metrics["prev_week_end"]}</b></span>'
        f'<span>生成时间 <b>{_html.escape(str(meta["generated_at"]))}</b></span>'
        f'</div>')

    warn = ('<div class="kpi" style="grid-column:1/-1;border-color:#EBD5AC;background:var(--warn-soft)">'
            '<div class="m" style="color:var(--warn);font-size:12.5px">本版为降级产物：'
            '在线模型不可用，摘要由确定性规则引擎生成。</div></div>') if meta.get("degraded") else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Saviland 广告周报 · {metrics['week_label']}</title>
<style>{CSS}</style></head>
<body><div class="wrap">

<div class="hero">
  <div class="mark">Saviland · Nail Art · Weekly Ads Review</div>
  <h1>亚马逊广告周报<span class="chip">{metrics['week_label']}</span></h1>
  {hero_meta}
</div>

<div class="kpis">{kpis}{warn}</div>

<section>
  <h2>一、执行摘要</h2>
  <p class="lede">{_html.escape(ai.get('executive_summary') or '（本次未生成摘要）')}</p>
</section>

<section>
  <h2>二、关键指标总览</h2>
  {metric_table}
  {_img('daily_trend', chart_paths, '图 1　日趋势：柱=广告花费，线=广告销售额。看「钱花出去了，销售有没有跟上」。', embed_images)}
</section>

<section>
  <h2>三、问题诊断与处置建议</h2>
  <p class="note">规则引擎共识别 <b>{len(findings)}</b> 条问题：
    🔴 严重 {by_sev.get('critical', 0)}　🟠 预警 {by_sev.get('warning', 0)}　
    🟢 机会 {by_sev.get('opportunity', 0)}　🔵 提示 {by_sev.get('info', 0)}；
    按目标 ACOS {metrics['target_acos']:.0%} 折算，可归因浪费约 <b>{_money0(diag_waste)}</b>。</p>
  {''.join(cards)}
</section>

<section>
  <h2>四、活动明细</h2>
  {camp_table}
  {_img('acos_by_campaign', chart_paths, '图 2　各活动 ACOS 排名：绿=健康，黄=预警，红=超红线或零转化。', embed_images)}
  {_img('spend_structure', chart_paths, '图 3　广告花费结构：预算是否过度集中在少数活动上。', embed_images)}
</section>

<section>
  <h2>五、环比变化与流量质量分诊</h2>
  {_img('wow_compare', chart_paths, '图 4　本周 vs 上周：哪些指标在涨、哪些在跌。', embed_images)}
  {_img('ctr_cvr_quadrant', chart_paths, '图 5　CTR × CVR 四象限：把「该改主图」和「该改详情页」分开——'
         '这张图是内容侧与投放侧的分诊台。', embed_images)}
</section>

<section>
  <h2>六、库存与广告的联动风险</h2>
  <p class="note">断货风险 SKU：<b>{extras['stockout_risk']}</b> 个　｜　
    压货 SKU：<b>{extras['overstock']}</b> 个　｜　
    广告在放量但库存吃紧：<b>{_html.escape(str(extras['ad_stockout_skus'] or '无'))}</b></p>
  <table><thead><tr><th>SKU</th><th>商品</th><th class="num">可用库存</th><th class="num">日均销量</th>
    <th class="num">可售天数</th><th class="num">补货周期</th><th class="num">建议补货</th></tr></thead>
    <tbody>{stock_rows}</tbody></table>
  <p class="note">加预算前先看这张表：可售天数低于补货周期的 SKU，加预算等于把自己的 listing 打成断货。</p>
</section>

<section>
  <h2>七、下周行动清单</h2>
  <ul class="plan">{plan}</ul>
</section>

<section>
  <h2>八、闭环：本周需要重写文案的 SKU</h2>
  {targets_html}
</section>

<section>
  <h2>附录：数据口径</h2>
  <table>
    <tr><th>项目</th><th>值</th></tr>
    <tr><td>广告数据来源</td><td>{_html.escape(str(meta['data_source']))}</td></tr>
    <tr><td>数据窗口</td><td>{metrics['week_start']} ~ {metrics['week_end']}（{len(metrics['daily'])} 天）</td></tr>
    <tr><td>参与统计的活动</td><td>{len(metrics['by_campaign'])} 个</td></tr>
    <tr><td>指标口径</td><td>ACOS=花费/广告销售额；CVR=订单/点击；CTR=点击/曝光；CPC=花费/点击</td></tr>
    <tr><td>诊断规则</td><td>命中 {len({f['rule'] for f in findings})} 条规则；点击量不足不下转化结论</td></tr>
    <tr><td>参考的品牌知识</td><td>{_html.escape(', '.join(meta.get('rag_citations') or []) or '无')}</td></tr>
    <tr><td>报告编号</td><td><code>{_html.escape(str(meta['run_id']))}</code></td></tr>
  </table>
  <details><summary>查看 Markdown 原文（可复制）</summary><pre>{_html.escape(md_text)}</pre></details>
</section>

<footer>Saviland 电商运营台 · 广告周报自动生成 · 数字口径见附录</footer>
</div></body></html>"""
