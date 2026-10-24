# -*- coding: utf-8 -*-
"""命令行入口：把两条链路和后台能力都暴露成可复现的命令。

    python run.py doctor                      自检（环境/密钥/数据库/向量库/字体）
    python run.py seed                        灌入产品/库存/订单/广告数据
    python run.py listing --all               链路 1：全量生成主图与卖点文案
    python run.py listing --skus A,B          链路 1：指定 SKU
    python run.py weekly                      链路 2：生成最近一个完整周的广告周报
    python run.py weekly --week 2026-02-09     链路 2：指定周（周一日期）
    python run.py rag "美甲灯 卖点"            检索控制台：看底座混合检索的打分明细
    python run.py serve                       启动自建 Web 前端（Starlette + Uvicorn）
    python run.py whatif --campaign C002 --orders 0
                                              现场验证：改一处数据，看规则是否立刻改口
    python run.py portfolio                   一键产出作品集所需的全部材料
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import config, db, llm, publish as publish_mod, rag_bridge, runs, seed
from .domain import ads as ads_domain
from .domain import catalog as catalog_domain
from .domain import inventory as inv_domain
from .domain import orders as orders_domain
from .pipelines import listing as listing_pipe
from .pipelines import weekly as weekly_pipe


def _h(title: str) -> None:
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def _kv(pairs: list[tuple[str, object]]) -> None:
    width = max((len(str(k)) for k, _ in pairs), default=0)
    for k, v in pairs:
        print(f"  {str(k).ljust(width)} : {v}")


def _json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    _h("① 环境与路径")
    _kv([
        ("Python", sys.version.split()[0]),
        ("解释器", sys.executable),
        ("项目根目录", config.ROOT),
        (".env 来源", ", ".join(config.ENV_SOURCES) or "（仅系统环境变量）"),
        ("RAG/Agent 底座", config.BASE_AGENT_ROOT),
        ("底座源码存在", config.BASE_AGENT_SRC.is_dir()),
    ])

    _h("② 大模型引擎")
    engines = llm.available_engines()
    _kv([
        ("可用引擎", ", ".join(engines) or "无（将走离线确定性生成）"),
        ("智谱 Key", "已配置" if config.has_zhipu() else "未配置"),
        ("DeepSeek Key", "已配置" if config.has_deepseek() else "未配置"),
        ("默认引擎", config.DEFAULT_ENGINE),
    ])
    if engines and not args.no_network:
        print("\n  正在实测各引擎连通性（--no-network 可跳过）…")
        for name, res in llm.smoke_test().items():
            flag = "OK  " if res.get("ok") else "FAIL"
            print(f"    [{flag}] {name:9} {res.get('latency_ms', '-'):>6} ms  "
                  f"{str(res.get('text') or res.get('note'))[:60]}")

    _h("③ 数据库")
    db.init_db()
    counts = db.table_counts()
    _kv([(k, v) for k, v in counts.items()])
    if counts.get("products", 0) == 0:
        print("\n  ⚠ 还没有业务数据，请先执行：python run.py seed")

    _h("④ 向量库（底座混合检索）")
    info = rag_bridge.ensure_index()
    _kv([
        ("集合名", info["collection"]),
        ("知识库文件", f"{info['kb_files']} 个 md，合并后 {info['kb_chars']} 字节"),
        ("向量片段数", info["chunks"]),
        ("embedding 引擎", info["embedder"]),
        ("就绪", info["ready"]),
        ("错误", info["error"] or "无"),
    ])
    if info["chunks"]:
        res = rag_bridge.search("美甲灯 类目卖点 差异化", k=3)
        print(f"\n  试检索「美甲灯 类目卖点 差异化」→ 模式={res.mode}，命中 {len(res.hits)} 条，"
              f"耗时 {res.latency_ms} ms")
        for h in res.hits:
            print(f"    - [{h.score:.3f}] {h.source}  {h.content[:56].strip()}…")
    print(f"\n  （底座原向量库目录 {config.BASE_AGENT_ROOT / 'data' / 'chroma_db'} 未被改动）")

    _h("⑤ 图片渲染环境")
    try:
        from PIL import Image, ImageFont  # noqa: F401
        from .render import hero as hero_render
        font = hero_render.load_font(40, bold=True)
        _kv([("Pillow", "可用"), ("中文字体", hero_render._first_existing(hero_render.FONT_CANDIDATES_CJK) or "未找到"),
             ("英文字体", hero_render._first_existing(hero_render.FONT_CANDIDATES_BOLD) or "未找到"),
             ("字体加载", type(font).__name__)])
    except Exception as e:  # noqa: BLE001
        _kv([("Pillow", f"异常：{type(e).__name__}: {e}")])
    try:
        import matplotlib
        _kv([("matplotlib", matplotlib.__version__), ("后端", matplotlib.get_backend())])
    except Exception as e:  # noqa: BLE001
        _kv([("matplotlib", f"异常：{type(e).__name__}: {e}")])

    _h("⑥ 运行档案")
    _kv([("累计运行次数", runs.run_counts() or "暂无")])
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_seed(args) -> int:
    only = (getattr(args, "only", "") or "").lower()
    if only in ("inventory", "orders"):
        _h(f"只重灌 {only}（不动商品 / 广告 / 已生成的文案版本）")
        if only == "inventory":
            print(f"  已重建 {seed.reseed_inventory()} 个 SKU 的库存记录")
        else:
            print(f"  已重建 {seed.reseed_orders(days=args.days_orders)} 笔订单")
        advice = inv_domain.restock_advice()
        levels: dict[str, int] = {}
        for r in advice:
            levels[r["level"]] = levels.get(r["level"], 0) + 1
        print(f"  库存健康度：{levels}")
        for r in advice[:8]:
            days = "∞" if r["available_days"] == float("inf") else f"{r['available_days']:.0f}天"
            print(f"    {r['sku']:18} {r['level']:14} 可用 {r['available']:>5}  可售 {days:>7}")
        o = orders_domain.sales_summary(30)
        print(f"  近 30 天：营收 ${o['revenue']:,.2f} / 毛利 ${o['gross_profit']:,.2f} / {o['units']} 件")
        print()
        return 0

    _h("灌入业务数据")
    out = seed.seed_all(force=args.force, days_orders=args.days_orders, days_ads=args.days_ads)
    if out.get("skipped"):
        print(f"  跳过：{out['reason']}（--force 可强制重灌）")
    else:
        _kv([
            ("产品数", out.get("products")),
            ("库存记录", out.get("inventory")),
            ("订单数", out.get("orders")),
            ("广告活动/日数据", f"{out['ads']['campaigns']} 个活动 / {out['ads']['daily_rows']} 行日数据"),
            ("广告数据窗口", out["ads"]["window"]),
        ])
    _h("数据概览")
    _kv([(k, v) for k, v in (out.get("counts") or db.table_counts()).items()])
    preview = out.get("preview_total")
    if preview:
        print(f"\n  预览周 {out['preview_week']}：花费 ${preview['spend']:,.2f} / "
              f"销售 ${preview['sales']:,.2f} / "
              f"ACOS {'—' if preview['acos'] is None else format(preview['acos'], '.1%')}")
        print(f"  规则引擎识别到 {out['preview_findings']} 条问题")
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_listing(args) -> int:
    if args.all:
        skus = None
    elif args.skus:
        skus = [s.strip() for s in args.skus.split(",") if s.strip()]
    else:
        skus = [p["sku"] for p in catalog_domain.list_products()][: args.limit or 1]
    _h(f"链路 1：主图/卖点文案批量生成（{len(skus) if skus else '全部'} 个 SKU，"
       f"{args.variants} 种主图版式）")

    def progress(i: int, total: int, res) -> None:
        flag = "降级" if res.degraded else ("OK" if res.ok else "失败")
        print(f"  [{i}/{total}] {res.sku}  {flag}  引擎={res.engine_label}  "
              f"RAG={res.rag_hits}条  合规替换={len(res.violations)}  图={len(res.hero_images)}张  "
              f"{res.latency_ms}ms")

    out = listing_pipe.generate_batch(
        skus, variants=args.variants, engine=args.engine,
        use_rag=not args.no_rag, limit=args.limit, on_progress=progress,
    )
    _h("链路 1 汇总")
    _kv([
        ("SKU 数", out["skus"]), ("成功", out["succeeded"]),
        ("降级（离线模板）", out["degraded"]), ("失败", out["failed"]),
        ("生成主图", f"{out['hero_images']} 张"),
        ("品牌知识检索命中合计", out["rag_hits"]),
        ("合规拦截替换", out["violations"]),
        ("总耗时", f"{out['elapsed_s']} 秒"),
    ])
    sheet = listing_pipe.contact_sheet_for(skus)
    if sheet:
        print(f"\n  主图总览图：{sheet}")
    print(f"  文案明细：{config.OUTPUTS_DIR / 'listing' / 'batch_latest.csv'}")
    print()
    return 0 if out["failed"] == 0 else 1


# --------------------------------------------------------------------------
def cmd_weekly(args) -> int:
    week = args.week or ads_domain.default_report_week()
    _h(f"链路 2：广告数据周报自动生成（{ads_domain.week_label(week)}）")
    print(f"  报告周：{week}（{ads_domain.week_bounds(week)[1]} 结束）")
    if args.list_weeks:
        print("  可选周：" + ", ".join(ads_domain.available_weeks()))
        return 0

    def on_event(msg: str) -> None:
        print(f"    · {msg}")

    res = weekly_pipe.generate_weekly(week_start=week, engine=args.engine,
                                      use_rag=not args.no_rag, on_event=on_event)
    if not res.ok:
        print(f"\n  生成失败：{res.note}")
        return 1
    total = res.metrics["total"]
    _h("周报已生成")
    _kv([
        ("Markdown 报告", res.report_md),
        ("HTML 报告（图片内嵌，可直接转发）", res.report_html),
        ("Excel 明细", res.report_xlsx),
        ("图表", f"{len(res.charts)} 张"),
        ("生成引擎", res.engine + ("（降级）" if res.degraded else "")),
        ("品牌知识检索命中", f"{res.rag_hits} 条"),
        ("诊断问题数", len(res.findings)),
        ("耗时", f"{res.latency_ms} ms"),
        ("运行编号", res.run_id),
    ])
    print(f"\n  账户：花费 ${total['spend']:,.2f} / 销售 ${total['sales']:,.2f} / "
          f"ACOS {'—' if total['acos'] is None else format(total['acos'], '.1%')}")
    from .domain.ads import diagnostics_summary
    diag = diagnostics_summary(res.findings)
    print(f"  诊断：严重 {diag['by_severity'].get('critical', 0)} / "
          f"预警 {diag['by_severity'].get('warning', 0)} / "
          f"机会 {diag['by_severity'].get('opportunity', 0)}，"
          f"可归因浪费 ${diag['waste_usd']:,.2f}")
    for f in res.findings[:6]:
        print(f"    - [{f['severity']:11}] {f['subject_name'][:30]:32} {f['rule']}")
    targets = weekly_pipe.listing_targets(res.findings)
    if targets:
        print(f"\n  闭环：{len(targets)} 个 SKU 应回到链路 1 重写内容 → {', '.join(targets)}")
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_rag(args) -> int:
    _h(f"RAG 检索控制台：{args.query}")
    info = rag_bridge.ensure_index()
    _kv([("集合", info["collection"]), ("片段数", info["chunks"]), ("引擎", info["embedder"])])
    res = rag_bridge.search(args.query, k=args.k, rewrite=args.rewrite)
    print(f"\n  模式={res.mode}  命中={len(res.hits)}  候选池={res.pool_size}  耗时={res.latency_ms}ms")
    if res.rewritten:
        print(f"  查询改写：{res.rewritten}")
    if res.keywords:
        print(f"  关键词：{', '.join(res.keywords)}")
    if res.note:
        print(f"  备注：{res.note}")
    for i, h in enumerate(res.hits, 1):
        print(f"\n  [{i}] 融合分 {h.score:.4f}（向量 {h.vec_sim:.4f} + 关键词 {h.kw_score:.4f}）"
              f"  来源：{h.source}")
        if h.matched:
            print(f"      命中词：{', '.join(h.matched[:8])}")
        print("      " + h.content[:220].replace("\n", " ") + "…")
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_import_ads(args) -> int:
    _h(f"导入广告报表：{args.path}")
    out = seed.import_ads_csv(args.path)
    _kv([("写入行数", out["rows"]), ("跳过行数", out["skipped"])])
    print("  列映射：" + json.dumps(out["mapped_columns"], ensure_ascii=False))
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_serve(args) -> int:
    """启动自建 Web 前端（Starlette + Uvicorn）。

    为什么不再用 Streamlit：版面基本由它决定，最后得到的是"内部工具"的观感；
    而这个项目要当作品集，第一眼观感就是门槛。现在前端是手写 HTML/CSS/JS，
    后端是 Starlette —— 两者都在底座已装的依赖里，**一个新依赖都不用加**。
    """
    from .web import server as web_server

    _h("启动 Web 前端")
    _kv([("访问地址", f"http://{args.host}:{args.port}（被占用会自动换号）"),
         ("后端", "Starlette + Uvicorn（零新依赖）"),
         ("自动打开浏览器", "否" if args.no_browser else "是"),
         ("启动日志", args.log or "（未开启）")])
    print("\n  按 Ctrl+C 停止。\n")
    return web_server.serve(host=args.host, port=args.port,
                            open_browser=not args.no_browser,
                            log_path=args.log or None)


# --------------------------------------------------------------------------
def cmd_portfolio(args) -> int:
    """一键产出作品集材料：灌数据 → 链路 2 周报 → 链路 1 文案与主图 → 汇总清单。"""
    _h("作品集材料一键生成")
    print("  ① 灌入业务数据 …")
    seed.seed_all(force=args.force)

    print("  ② 构建品牌知识向量库（调用底座）…")
    info = rag_bridge.ensure_index()
    print(f"     → {info['chunks']} 个片段，embedding={info['embedder']}")

    print("  ③ 链路 2：生成广告周报 …")
    wk = weekly_pipe.generate_weekly()
    print(f"     → {wk.report_html}")

    print("  ④ 链路 2 的结论回灌链路 1：重写问题 SKU 的文案与主图 …")
    targets = weekly_pipe.listing_targets(wk.findings) or \
        [p["sku"] for p in catalog_domain.list_products()][:args.listing_limit]
    out = listing_pipe.generate_batch(
        targets, variants=3, on_progress=lambda i, n, r: print(
            f"     [{i}/{n}] {r.sku} {'降级' if r.degraded else 'OK'} 图={len(r.hero_images)}张"))

    print("  ⑤ 重出主图总览图与文案总表（覆盖**全部已有产物的 SKU**，而不是这一轮新生成的几个）…")
    sheet_paths = publish_mod.hero_sheets_for_all(variants=3)
    all_csv = listing_pipe.export_all_latest()
    print(f"  文案总表（全部 SKU 最新版）：{all_csv}")

    print("  ⑥ 发布文档配图到 docs/images …")
    publish_mod.publish_docs_images()

    _h("作品集材料清单")
    items = [
        ("广告周报（HTML，双击即看）", wk.report_html),
        ("广告周报（Markdown）", wk.report_md),
        ("广告周报（Excel 明细）", wk.report_xlsx),
        ("周报图表目录", config.OUTPUTS_DIR / "weekly_charts"),
        ("文案明细（全部 SKU 最新版）", config.OUTPUTS_DIR / "listing" / "all_copies.csv"),
        ("文案明细（最近一次批量）", config.OUTPUTS_DIR / "listing" / "batch_latest.csv"),
        ("主图目录", config.OUTPUTS_DIR / "hero_images"),
        ("主图总览图（全部 SKU）", sheet_paths[0] if sheet_paths else "（无）"),
        ("文档配图目录", config.DOCS_DIR / "images"),
        ("作品集文档", config.ROOT / "README.md"),
    ]
    _kv(items)
    print(f"\n  链路 1：{out['succeeded']} 成功 / {out['degraded']} 降级 / "
          f"{out['hero_images']} 张主图 / 合规替换 {out['violations']} 处")
    print(f"  链路 2：{len(wk.findings)} 条诊断 / 引擎 {wk.engine}"
          f"{'（降级）' if wk.degraded else ''}")
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_runs(args) -> int:
    """运行档案与性能 —— "每次链路执行都留痕"这个承诺的查看入口。"""
    _h("运行档案与性能")
    rows = runs.recent_runs(args.pipeline, limit=args.limit)
    print(f"{'对象':22} {'引擎':24} {'检索':>4} {'耗时':>8}  {'状态':11} 备注")
    print("-" * 96)
    for r in rows:
        note = (r["note"] or "").replace("\n", " ")[:32]
        print(f"{str(r['target'])[:21]:22} {str(r['engine'] or '-')[:23]:24} "
              f"{r['rag_hits']:>4} {r['latency_ms'] / 1000:>7.1f}s  {r['status']:11} {note}")
    if rows:
        lat = [r["latency_ms"] for r in rows]
        print("-" * 96)
        print(f"耗时：平均 {sum(lat) / len(lat) / 1000:.1f}s　最快 {min(lat) / 1000:.1f}s　"
              f"最慢 {max(lat) / 1000:.1f}s")

    usage = runs.engine_usage()
    if usage:
        print("\n按引擎统计：")
        for u in usage:
            print(f"  {str(u['engine']):28} 次数 {u['n']:>3}　"
                  f"平均 {u['avg_latency_ms'] or 0:>7} ms　检索命中合计 {u['total_rag_hits']}")

    counts = runs.run_counts()
    if counts:
        print("\n按链路 / 状态统计：")
        for pipe, statuses in counts.items():
            print(f"  {pipe:10} " + "　".join(f"{k}={v}" for k, v in statuses.items()))

    art = db.query("SELECT kind, COUNT(*) AS n FROM artifacts GROUP BY kind ORDER BY n DESC")
    if art:
        print("\n产物登记：")
        for a in art:
            print(f"  {a['kind']:22} {a['n']}")
    print()
    return 0


# --------------------------------------------------------------------------
def cmd_whatif(args) -> int:
    """现场验证：改一处数据 → 重新诊断 → 指出哪条规则被触发，然后还原。

    这是演示说明里"我可以现场改数据验证规则"那句承诺的**可执行版本**。
    """
    from . import whatif as whatif_mod

    _h("现场验证：改数据 → 看规则是否立刻给出结论")

    if args.list:
        from .domain import ads as ads_domain
        week = args.week or ads_domain.default_report_week()
        m = ads_domain.weekly_metrics(week)
        findings = ads_domain.diagnose(m)
        hit = {}
        for f in findings:
            hit.setdefault(f.get("subject"), set()).add(f["rule"])
        print(f"  报告周 {week}（{ads_domain.week_bounds(week)[0]} ~ {ads_domain.week_bounds(week)[1]}）\n")
        print(f"    {'活动':<26}{'花费':>10}{'点击':>7}{'订单':>6}{'ACOS':>8}  现有结论")
        for c in m["by_campaign"]:
            acos = "—" if c["acos"] is None else f"{c['acos']:.1%}"
            rules = "、".join(sorted(hit.get(c["campaign_id"], []))) or "（无）"
            print(f"    {c['campaign_id']:<26}{c['spend']:>10.2f}{c['clicks']:>7}"
                  f"{c['orders']:>6}{acos:>8}  {rules}")
        print("\n  ★ 挑一个「订单 > 0 且现有结论很少」的活动，把 --orders 改成 0，"
              "新结论才会出现。")
        return 0

    try:
        res = whatif_mod.whatif(
            campaign=args.campaign, spend=args.spend, orders=args.orders,
            week=args.week, keep=args.keep,
        )
    except ValueError as e:
        print(f"  [错误] {e}")
        return 2

    camp = res["campaign"]
    _kv([("活动", f"{camp['campaign_id']}  {camp['name']}"),
         ("SKU", camp["sku"]),
         ("报告周", f"{res['week']}（{res['week_range']}，{res['days']} 天）")])

    def _fmt(v, kind: str) -> str:
        if v is None:
            return "—"
        if kind == "pct":
            return f"{v:.1%}"
        if kind == "money":
            return f"${v:,.2f}"
        return f"{v}"

    print("\n  ── 改动前后 ──")
    print(f"    {'':6}{'花费':>12}{'点击':>8}{'订单':>6}{'广告销售额':>14}")
    for label, key in (("改前", "before"), ("改后", "after")):
        d = res[key]
        print(f"    {label:6}{_fmt(d['spend'], 'money'):>12}{d['clicks']:>8}"
              f"{d['orders']:>6}{_fmt(d['sales'], 'money'):>14}")

    print(f"\n    该活动 ACOS：{_fmt(res['metrics_before']['acos'], 'pct')}"
          f" → {_fmt(res['metrics_after']['acos'], 'pct')}")
    print(f"    账户 ACOS  ：{_fmt(res['account_acos_before'], 'pct')}"
          f" → {_fmt(res['account_acos_after'], 'pct')}")
    print(f"    诊断条数   ：{res['findings_before']} → {res['findings_after']}")

    if res["new_findings"]:
        print("\n  ── 立刻出现的新结论 ──")
        for f in res["new_findings"]:
            print(f"    [{f['severity']:<11}] {f['rule']}")
            print(f"      现象：{f['diagnosis']}")
            ev = " / ".join(f"{k}={v}" for k, v in list(f["evidence"].items())[:4])
            print(f"      证据：{ev}")
            print(f"      执行方：{f['feeds']}")
    else:
        print("\n  ── 没有新规则被触发 ──")
        print("    可能原因：点击量仍低于判断门槛，或这次改动没跨过规则阈值。")
        print("    试试加大 --spend，或把 --orders 设为 0。")

    if res["gone_findings"]:
        print("\n  ── 因为改动而消失的结论 ──")
        for f in res["gone_findings"]:
            print(f"    [{f['severity']:<11}] {f['rule']}  {f['diagnosis'][:60]}")

    if res["restored"]:
        print("\n  [已还原] 广告数据已按固定种子重灌，与改动前一致。")
        print("           想在界面上留着看，加 --keep；想直接出报告，加 --report。")
    else:
        print("\n  [保留改动] 数据仍是改动后的状态。恢复：python run.py seed --only ads")

    if args.report:
        print("\n  重新生成周报 …")
        wk = weekly_pipe.generate_weekly(week_start=res["week"])
        print(f"    → {wk.report_html}")
    return 0


def cmd_backend(args) -> int:
    """不启动界面，直接把后台数据打出来（用于快速核验业务层）。"""
    _h("电商后台数据速览")
    s = catalog_domain.summary()
    _kv([("商品总数", s["total"]), ("平均售价", f"${s['avg_price']:.2f}"),
         ("平均评分", f"{s['avg_rating']:.2f}"), ("库存合计", s["stock_total"])])
    print("\n  分类目：")
    for c in s["by_category"]:
        print(f"    {c['category']:12} {c['n']:>3} 个   均价 ${c['avg_price']:.2f}")

    o = orders_domain.sales_summary(30)
    print(f"\n  近 30 天销售：营收 ${o['revenue']:,.2f} / 毛利 ${o['gross_profit']:,.2f} "
          f"（毛利率 {o['gross_margin']:.1%}）/ 退款 {o['refund_units']} 件")
    print("  分渠道：")
    for c in orders_domain.by_channel(30):
        print(f"    {c['channel']:14} ${c['revenue']:>12,.2f}   {c['units']:>5} 件")

    inv = inv_domain.restock_advice()
    levels: dict[str, int] = {}
    for r in inv:
        levels[r["level"]] = levels.get(r["level"], 0) + 1
    print(f"\n  库存健康度：{levels}")
    for r in inv[:6]:
        days = "∞" if r["available_days"] == float("inf") else f"{r['available_days']:.0f}天"
        print(f"    {r['sku']:18} {r['level']:14} 可用 {r['available']:>5}  可售 {days:>7}")

    week = ads_domain.default_report_week()
    m = ads_domain.weekly_metrics(week)
    t = m["total"]
    print(f"\n  广告周 {m['week_label']}：花费 ${t['spend']:,.2f} / 销售 ${t['sales']:,.2f} / "
          f"ACOS {'—' if t['acos'] is None else format(t['acos'], '.1%')} / "
          f"活动 {len(m['by_campaign'])} 个")
    print()
    return 0


# --------------------------------------------------------------------------
# 交互菜单
#   为什么菜单放在 Python 里而不是写在 .bat 里：
#   ★ 踩过的坑：第一版把中文菜单直接写在 `运行.bat` 里，结果 cmd.exe 把中文
#   当成命令去执行，刷出几万行 `'xx' is not recognized as an internal or
#   external command`，又因为菜单有 `goto menu` 变成死循环。
#   两个原因叠加：① .bat 必须用 CRLF 行尾，LF 行尾时 cmd 会错行解析；
#   ② .bat 里的中文要同时匹配文件的编码和当前控制台代码页（chcp）才不乱码。
#   把菜单搬到 Python（源码是明确的 UTF-8，输出流也显式设成 UTF-8），
#   这一类问题整体消失；.bat 退回成一个纯 ASCII 的启动器。
MENU_ITEMS: list[tuple[str, str, list[str]]] = [
    ("1", "自检：环境 / 密钥 / 数据库 / 向量库 / 字体", ["doctor"]),
    ("2", "灌入业务数据：商品 / 库存 / 订单 / 广告", ["seed"]),
    ("3", "启动 Web 界面（浏览器打开 http://127.0.0.1:8501，被占自动换号）", ["serve"]),
    ("4", "链路 1：批量生成主图与卖点文案（全部 SKU，约 12 分钟）", ["listing", "--all"]),
    ("5", "链路 2：生成广告周报（最近一个完整周）", ["weekly"]),
    ("6", "一键产出作品集材料（推荐先跑这个，约 5 分钟）", ["portfolio"]),
    ("7", "后台数据速览（不开界面）", ["backend"]),
    ("8", "RAG 检索控制台（看底座混合检索的打分明细）", ["__rag_prompt__"]),
    ("9", "运行档案与性能（看引擎 / 检索命中 / 耗时 / 降级）", ["runs"]),
    ("w", "现场验证：把某活动订单清零，看诊断引擎是否立刻改口（改完自动还原）",
     ["__whatif_prompt__"]),
    ("0", "退出", []),
]


def _print_menu() -> None:
    print("\n" + "=" * 74)
    print("  Saviland 电商 AI 系统")
    print("=" * 74)
    for key, label, _argv in MENU_ITEMS:
        print(f"  [{key}] {label}")
    print()


def cmd_menu(args) -> int:
    """中文交互菜单。用 `--choice N` 可以非交互地直接执行某一项（便于自动化验证）。"""
    choice = (getattr(args, "choice", "") or "").strip()

    if choice:
        return _dispatch_menu(choice, interactive=False)

    while True:
        _print_menu()
        try:
            choice = input("  请输入编号后回车：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已退出。")
            return 0
        code = _dispatch_menu(choice, interactive=True)
        if code is None:      # 无效输入
            continue
        if code == "exit":
            return 0


def _dispatch_menu(choice: str, interactive: bool):
    """返回 int（正常）/"exit"（退出）/ None（无效输入）。"""
    entry = next((e for e in MENU_ITEMS if e[0] == choice), None)
    if entry is None:
        print(f"  无效编号：{choice}")
        return None
    _key, label, argv = entry
    if not argv:
        print("  已退出。")
        return "exit"
    print(f"\n  → {label}\n")

    if argv == ["__rag_prompt__"]:
        try:
            query = input("  请输入检索词：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。")
            return 0
        if not query:
            print("  检索词为空，已取消。")
            return 0
        argv = ["rag", query]

    if argv == ["__whatif_prompt__"]:
        print("  现场验证：把某个活动的订单清零，看诊断引擎会不会立刻改口。")
        print("  直接回车 = 用 C002（12 色甲油胶套装，当前 47 单、零结论，最适合演示）。\n")
        try:
            camp = input("  活动 id / 名称片段 [C002]：").strip() or "C002"
            orders_raw = input("  改成多少单 [0]：").strip() or "0"
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。")
            return 0
        try:
            orders = int(orders_raw)
        except ValueError:
            print("  订单数要填整数，已取消。")
            return 0
        argv = ["whatif", "--campaign", camp, "--orders", str(orders)]

    code = main(argv)
    if interactive:
        try:
            input("\n  按回车返回菜单…")
        except (EOFError, KeyboardInterrupt):
            print()
            return "exit"
    return code


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Saviland 电商 AI 系统 —— 命令行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("doctor", help="自检环境/密钥/数据库/向量库")
    d.add_argument("--no-network", action="store_true", help="跳过联网实测")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("seed", help="灌入产品/库存/订单/广告数据")
    s.add_argument("--force", action="store_true", help="清空业务数据后重灌")
    s.add_argument("--only", choices=["inventory", "orders"], default="",
                   help="只重灌某一张表（不会删掉已生成的文案版本）")
    s.add_argument("--days-orders", type=int, default=60)
    s.add_argument("--days-ads", type=int, default=35)
    s.set_defaults(func=cmd_seed)

    l = sub.add_parser("listing", help="链路 1：主图/卖点文案批量生成")
    l.add_argument("--all", action="store_true", help="全部 SKU")
    l.add_argument("--skus", type=str, default="", help="逗号分隔的 SKU")
    l.add_argument("--limit", type=int, default=0, help="限制数量")
    l.add_argument("--variants", type=int, default=3, help="主图版式数（1-3）")
    l.add_argument("--engine", type=str, default=None, help="zhipu / deepseek")
    l.add_argument("--no-rag", action="store_true", help="不做品牌知识检索（对比用）")
    l.set_defaults(func=cmd_listing)

    w = sub.add_parser("weekly", help="链路 2：广告数据周报自动生成")
    w.add_argument("--week", type=str, default=None, help="周一日期 YYYY-MM-DD")
    w.add_argument("--engine", type=str, default=None)
    w.add_argument("--no-rag", action="store_true")
    w.add_argument("--list-weeks", action="store_true", help="列出有数据的周")
    w.set_defaults(func=cmd_weekly)

    r = sub.add_parser("rag", help="RAG 检索控制台")
    r.add_argument("query", type=str)
    r.add_argument("--k", type=int, default=5)
    r.add_argument("--rewrite", action="store_true", help="开启查询改写（多花一次 LLM 调用）")
    r.set_defaults(func=cmd_rag)

    i = sub.add_parser("import-ads", help="导入亚马逊广告报表 CSV")
    i.add_argument("path", type=str)
    i.set_defaults(func=cmd_import_ads)

    v = sub.add_parser("serve", help="启动 Web 前端（Starlette + Uvicorn）")
    v.add_argument("--port", type=int, default=8501)
    v.add_argument("--host", type=str, default="127.0.0.1")
    v.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    v.add_argument("--log", type=str, default="", help="把启动输出同时写入该文件（排障用）")
    v.set_defaults(func=cmd_serve)

    b = sub.add_parser("backend", help="不启界面，直接打印后台数据速览")
    b.set_defaults(func=cmd_backend)

    wi = sub.add_parser("whatif", help="现场验证：改一处广告数据，看规则引擎是否立刻给出结论")
    wi.add_argument("--campaign", type=str, required=True,
                    help="活动 id / id 前缀 / 名称片段，如 C006 或 猫眼")
    wi.add_argument("--spend", type=float, default=None, help="把这一周该活动的花费改成多少美元")
    wi.add_argument("--orders", type=int, default=None, help="把这一周的订单数改成多少（常用 0）")
    wi.add_argument("--week", type=str, default=None, help="报告周（默认：最近一个完整周）")
    wi.add_argument("--keep", action="store_true", help="保留改动（默认会自动还原）")
    wi.add_argument("--report", action="store_true", help="验证后直接重新生成周报")
    wi.add_argument("--list", action="store_true",
                    help="先列出本周各活动的花费/点击/订单/ACOS，挑一个来改")
    wi.set_defaults(func=cmd_whatif)

    rn = sub.add_parser("runs", help="运行档案与性能")
    rn.add_argument("--pipeline", type=str, default=None, help="只看 listing 或 weekly")
    rn.add_argument("--limit", type=int, default=60)
    rn.set_defaults(func=cmd_runs)

    m = sub.add_parser("menu", help="中文交互菜单（双击 运行.bat 就是走这里）")
    m.add_argument("--choice", type=str, default="",
                   help="直接执行某一项编号，用于非交互/自动化验证")
    m.set_defaults(func=cmd_menu)

    pf = sub.add_parser("portfolio", help="一键产出作品集所需全部材料")
    pf.add_argument("--force", action="store_true", help="重灌业务数据")
    pf.add_argument("--listing-limit", type=int, default=6)
    pf.set_defaults(func=cmd_portfolio)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
