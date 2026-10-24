# -*- coding: utf-8 -*-
"""链路 1：主图 / 卖点文案批量生成。

完整链路（每一步都可单独观察，便于评审时讲清楚）
------------------------------------------------
    1. 取产品主数据（DB）                    -> 结构化产品简报
    2. RAG 检索品牌知识库（底座混合检索）      -> 品牌调性 / 类目卖点 / 合规红线 / 竞品差异
    3. 组 Prompt 调 LLM                      -> 标题 / 主图大字 / 五点描述 / 搜索词
    4. 合规规则引擎二次校验                   -> 违禁词命中即替换，并记录改了什么
    5. 落库成版本（listing_copies）           -> 可回溯、可 A/B、可回滚
    6. Pillow 合成主图 PNG（3 种版式）        -> 可直接上架的图
    7. 写运行档案（generation_runs/artifacts）-> 每条产物都能追到是哪次运行生成的

为什么 RAG 在这一步是必需的
----------------------------
只给模型一个 SKU 和参数，它会写出**通用**文案（"high quality nail lamp"）。
把品牌知识库检索出来的"这个类目的差异化卖点 + 竞品短板 + 平台合规红线"一起喂进去，
它才会写出"36 颗双光源、10 秒单指固化、低热模式不烫手"这种**只有这个品牌能给**的话。
检索命中了几条、命中了哪些片段，都记在返回值里，报告里能看到。
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import config, db, llm, rag_bridge, runs
from ..domain import catalog as catalog_domain
from ..render import hero as hero_render
from . import compliance

# --------------------------------------------------------------------------
# 离线兜底用的英文卖点词库（按类目）
#   有 Key 时走真模型，这份词库是"没网也必须能出可用文案"的保险
# --------------------------------------------------------------------------
CATEGORY_EN: dict[str, dict[str, Any]] = {
    "美甲灯": {
        "noun": "UV LED Nail Lamp",
        "benefits": [
            "Fast, even curing for gel polish in one pass",
            "Low-heat mode protects sensitive nail beds",
            "Salon-level results without a salon appointment",
        ],
        "features": ["UV + LED dual light source", "Multiple timer settings", "Removable bottom tray"],
    },
    "甲油胶": {
        "noun": "Gel Nail Polish",
        "benefits": [
            "Rich, opaque color in two thin coats",
            "Long-lasting shine that resists chipping",
            "Smooth self-levelling formula, easy for beginners",
        ],
        "features": ["High-pigment formula", "Low-odor", "Works with any UV/LED lamp"],
    },
    "美甲工具": {
        "noun": "Nail Art Tool",
        "benefits": [
            "Precise control for detail work and cleanup",
            "Comfortable grip that reduces hand fatigue",
            "Durable build for repeated salon use",
        ],
        "features": ["Ergonomic handle", "Rust-resistant finish", "Easy to clean"],
    },
    "美甲饰品": {
        "noun": "Nail Art Decoration",
        "benefits": [
            "Instantly upgrades a plain manicure",
            "Endless combinations for every season",
            "Easy to apply and seal under top coat",
        ],
        "features": ["Mixed sizes and shapes", "Reusable storage box", "Sparkle that holds its shine"],
    },
    "手足护理": {
        "noun": "Hand & Nail Care",
        "benefits": [
            "Softens dry cuticles and rough edges",
            "Leaves hands and nails feeling conditioned",
            "A quick step that fits into any routine",
        ],
        "features": ["Gentle daily-use formula", "Non-greasy finish", "Light, clean scent"],
    },
    "美甲套装": {
        "noun": "Nail Art Kit",
        "benefits": [
            "Everything needed to start in one box",
            "Step-by-step friendly for first-time users",
            "Grows with you from beginner to confident",
        ],
        "features": ["Complete tool and color set", "Clear starter guidance", "Gift-ready packaging"],
    },
    "卸甲与辅助": {
        "noun": "Nail Prep & Removal",
        "benefits": [
            "Removes gel polish without harsh scraping",
            "Preps the nail surface for better adhesion",
            "Keeps manicures looking fresh for longer",
        ],
        "features": ["Fast-acting formula", "Gentle on natural nails", "Easy soak-off method"],
    },
}
DEFAULT_EN = {
    "noun": "Nail Care Product",
    "benefits": [
        "Salon-quality results from your own bathroom",
        "Designed for easy at-home use",
        "A dependable addition to your nail routine",
    ],
    "features": ["Thoughtful design", "Everyday durability", "Great value"],
}


# --------------------------------------------------------------------------
@dataclass
class ListingResult:
    sku: str
    ok: bool
    copy: dict = field(default_factory=dict)
    copy_id: int | None = None
    version: int | None = None
    engine: str = ""
    engine_label: str = ""
    degraded: bool = False
    rag_hits: int = 0
    rag_citations: list[str] = field(default_factory=list)
    rag_mode: str = ""
    rag_trace: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    hero_images: list[str] = field(default_factory=list)
    compliance: list[dict] = field(default_factory=list)
    run_id: str = ""
    latency_ms: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "sku": self.sku, "ok": self.ok, "version": self.version, "copy_id": self.copy_id,
            "engine": self.engine, "engine_label": self.engine_label, "degraded": self.degraded,
            "rag_hits": self.rag_hits, "rag_mode": self.rag_mode,
            "rag_citations": self.rag_citations, "violations": self.violations,
            "hero_images": self.hero_images, "run_id": self.run_id,
            "latency_ms": self.latency_ms, "note": self.note,
            "copy": self.copy,
        }


# --------------------------------------------------------------------------
# 1) 产品简报
# --------------------------------------------------------------------------
def build_brief(product: dict) -> str:
    """把产品主数据压成一段结构化简报 —— 这是喂给模型的事实底座。"""
    specs = product.get("specs") or {}
    lines = [
        f"SKU: {product.get('sku')}",
        f"品类: {product.get('category')} / {product.get('sub_category') or '-'}",
        f"中文标题: {product.get('title_cn')}",
        f"英文标题: {product.get('title_en') or '(无)'}",
        f"售价: ${product.get('price_usd')}  成本: ${product.get('cost_usd')}  "
        f"毛利率: {catalog_domain.margin_of(product):.0%}",
        f"评分: {product.get('rating')}（{product.get('review_count')} 条评论）",
        f"目标人群: {product.get('target_audience') or '-'}",
    ]
    if specs:
        lines.append("规格参数: " + "；".join(f"{k}={v}" for k, v in specs.items()))
    for label, key in (("现有卖点", "selling_points"), ("五点描述", "five_bullets"),
                       ("买家痛点", "pain_points"), ("使用场景", "use_scenarios"),
                       ("后台关键词", "keywords")):
        values = product.get(key) or []
        if values:
            lines.append(f"{label}: " + "；".join(str(v) for v in values))
    comps = product.get("competitors") or []
    if comps:
        lines.append("竞品: " + "；".join(
            f"{c.get('name')}（${c.get('price_usd')}，短板：{c.get('weakness')}）"
            for c in comps if isinstance(c, dict)))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 2) 品牌知识检索（RAG）
# --------------------------------------------------------------------------
def rag_queries(product: dict) -> list[str]:
    """针对文案生成任务设计的三路检索词。

    不是拿产品名随便搜一下：文案好不好，取决于有没有同时拿到
    「这个类目怎么卖」+「品牌什么调性」+「哪些话不能说」+「这个 SKU 的档案」。
    """
    category = product.get("category") or ""
    return [
        f"{category} 类目卖点 差异化 卖点库",
        "合规与禁用词 绝对化用语 医疗功效 认证宣称 替换",
        f"{product.get('sku')} {product.get('title_cn')} 产品档案 规格 竞品对比",
    ]


def brand_context(product: dict, k: int = 3, rewrite: bool = False) -> rag_bridge.RagResult:
    """多路检索并合并去重，返回一份给 LLM 用的上下文。"""
    merged = rag_bridge.RagResult(query=" | ".join(rag_queries(product)), mode="hybrid")
    seen: set[str] = set()
    for q in rag_queries(product):
        res = rag_bridge.search(q, k=k, rewrite=rewrite)
        if res.mode == "unavailable" and not merged.note:
            merged.note = res.note
            merged.mode = "unavailable"
        if res.mode == "keyword-only":
            merged.mode = "keyword-only"
        for hit in res.hits:
            key = hit.content[:120]
            if key in seen:
                continue
            seen.add(key)
            merged.hits.append(hit)
    merged.hits.sort(key=lambda h: -h.score)
    merged.pool_size = len(merged.hits)
    if merged.mode != "unavailable" and not merged.hits:
        merged.mode = "empty"
    return merged


# --------------------------------------------------------------------------
# 3) Prompt
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """你是 Saviland（美甲美妆品牌）的亚马逊 listing 与主图文案专家。
你的文案要在"有说服力"和"平台合规"之间取得平衡。

硬性要求：
1. 输出**英文**（面向 Amazon US 买家），只输出一个 JSON 对象，不要任何解释文字。
2. 禁止出现以下表述：最好/第一（best, #1, top rated）、保证（guaranteed）、
   100% 有效/安全、治疗或杀菌（cure, treat, heal, antifungal, antibacterial, kills bacteria）、
   FDA approved / medical grade、点名贬低竞品、主图里出现促销信息（free shipping、50% off）。
3. 所有性能表述必须能在"产品规格"里找到依据，不要编造参数（不要写出简报里没有的功率、灯珠数、认证）。
4. 主图文案要短、可读性优先：headline 不超过 42 个字符，subhead 不超过 58 个字符，
   badge 只放可量化的硬指标（如 "48W · 36 LEDs"），不超过 26 个字符。
5. 语气：专业、干净、有品质感，面向 20-40 岁在家做美甲的女性。
6. 五点描述每条独立成立，把买家最关心的三个问题（能不能在家做、伤不伤甲、能用多久）前置。"""

OUTPUT_SCHEMA = """{
  "title": "英文标题，不超过 200 字符，含核心关键词与关键规格",
  "hero_headline": "主图大字，不超过 42 字符",
  "hero_subhead": "主图副标题，不超过 58 字符",
  "hero_badge": "主图角标，可量化硬指标，不超过 26 字符",
  "cta": "行动号召短语，不超过 22 字符",
  "bullets": ["五点描述，恰好 5 条，每条 100-200 字符"],
  "description": "产品描述，300-600 字符，2-3 段",
  "keywords": ["后台搜索词，15 条，英文小写短语"],
  "hero_copy_variants": [
    {"headline": "主图方案 B 大字", "subhead": "方案 B 副标题", "badge": "方案 B 角标"},
    {"headline": "主图方案 C 大字", "subhead": "方案 C 副标题", "badge": "方案 C 角标"}
  ]
}"""


def build_prompt(product: dict, context: rag_bridge.RagResult, feedback: str = "") -> str:
    brief = build_brief(product)
    kb = context.context(max_chars=2600) or "（本次未检索到品牌知识，请仅依据产品规格撰写）"
    extra = f"\n\n【额外要求】\n{feedback}" if feedback else ""
    return f"""请为下面这个产品生成一整套亚马逊 listing 与主图文案。

【产品简报（唯一的事实来源，不得编造简报之外的参数）】
{brief}

【品牌知识库检索结果（用于对齐品牌调性、类目卖点与合规红线）】
{kb}

【检索到的知识来源】{', '.join(context.citations()) or '无'}{extra}

请严格按下面的 JSON 结构输出（只输出 JSON，不要 markdown 代码块）：
{OUTPUT_SCHEMA}"""


# --------------------------------------------------------------------------
# 4) 离线确定性生成器（无 Key / 网络不可用时的兜底，保证一定有产物）
# --------------------------------------------------------------------------
def offline_copy(product: dict, reason: str = "") -> dict:
    """不依赖任何模型的确定性文案生成。

    它不假装自己是大模型：输出结构完整、可上架、可读，但**用词是模板化的**。
    运行档案里会把引擎标成 `offline`，报告里也会注明"本版为降级产物"。
    """
    cat = product.get("category") or ""
    lexicon = CATEGORY_EN.get(cat, DEFAULT_EN)
    specs = product.get("specs") or {}
    spec_values = [str(v) for v in specs.values()]
    specs_text = "；".join(f"{k}: {v}" for k, v in specs.items()) or "见产品详情"
    title_en = (product.get("title_en") or product.get("title_cn") or product["sku"]).strip()
    audience = product.get("target_audience") or "at-home nail enthusiasts"
    pain_points = product.get("pain_points") or []
    scenarios = product.get("use_scenarios") or []

    badge = " · ".join(spec_values[:2])[:26] if spec_values else "SALON QUALITY"
    headline = (title_en if len(title_en) <= 42 else title_en[:39].rstrip() + "…")
    subhead = lexicon["benefits"][0][:58]

    bullets = list(product.get("five_bullets") or [])
    # 五点不足 5 条时，用类目词库补齐（并写入规格事实，保证有依据）
    pool = list(lexicon["benefits"]) + list(lexicon["features"])
    i = 0
    while len(bullets) < 5 and i < len(pool):
        bullets.append(pool[i])
        i += 1
    bullets = bullets[:5]
    bullets = [
        f"{b} — {title_en}. Specs: {specs_text[:90]}" if not any(
            ch in b for ch in ("—", "。" + title_en)) and len(b) < 70 else b
        for b in bullets
    ]

    kw = [str(k).lower() for k in (product.get("keywords") or [])]
    if not kw:
        kw = [f"{lexicon['noun'].lower()} {s}" for s in
              ("for beginners", "professional", "set", "kit", "salon")]
    keywords = []
    for k in kw:
        k = " ".join(k.split())
        if k and k not in keywords:
            keywords.append(k)
    keywords = keywords[:15]

    description = (
        f"{title_en} is built for {audience}. "
        + " ".join(lexicon["benefits"][:2]) + " "
        + f"Key specs: {specs_text}. "
        + ("Perfect for " + "、".join(str(s) for s in scenarios[:2]) + ". " if scenarios else "")
        + ("Designed to solve the common frustrations: "
           + "、".join(str(p) for p in pain_points[:2]) + ". " if pain_points else "")
        + "From Saviland — nail. art. elevated."
    )

    return {
        "sku": product["sku"],
        "title": title_en[:200],
        "hero_headline": headline,
        "hero_subhead": subhead,
        "hero_badge": badge,
        "cta": "Salon Results at Home",
        "bullets": bullets,
        "description": description[:600],
        "keywords": keywords,
        "hero_copy_variants": [
            {"headline": lexicon["features"][0][:42],
             "subhead": lexicon["benefits"][1][:58],
             "badge": (spec_values[0][:26] if spec_values else "PRO QUALITY")},
            {"headline": f"{lexicon['noun']} Set"[:42],
             "subhead": lexicon["benefits"][2][:58],
             "badge": "READY TO USE"},
        ],
        "_offline_reason": reason,
    }


# --------------------------------------------------------------------------
# 5) 校验与归一
# --------------------------------------------------------------------------
REQUIRED_KEYS = ("title", "hero_headline", "hero_subhead", "hero_badge", "cta",
                 "bullets", "description", "keywords")


def _normalize(raw: dict, product: dict) -> dict:
    """把模型输出归一到约定结构上；缺字段用离线值补齐，避免下游 KeyError。"""
    fallback = offline_copy(product, reason="模型输出字段缺失，已用模板补齐")
    out: dict[str, Any] = {"sku": product["sku"]}
    for key in REQUIRED_KEYS:
        value = raw.get(key)
        if key == "bullets":
            if not isinstance(value, list):
                value = fallback["bullets"]
            value = [str(v).strip() for v in value if str(v).strip()]
            while len(value) < 5:
                value.append(fallback["bullets"][len(value) % 5])
            value = value[:5]
        elif key == "keywords":
            if not isinstance(value, list):
                value = fallback["keywords"]
            seen: list[str] = []
            for v in value:
                v = " ".join(str(v).lower().split())
                if v and v not in seen:
                    seen.append(v)
            value = seen[:15] or fallback["keywords"]
        else:
            if not isinstance(value, str) or not value.strip():
                value = fallback[key]
            value = " ".join(value.split())
        out[key] = value

    # 主图 A/B 变体：模型给不够就用主文案派生，保证始终有 3 套方案
    variants = raw.get("hero_copy_variants")
    if not isinstance(variants, list):
        variants = []
    cleaned: list[dict] = []
    for v in variants:
        if isinstance(v, dict) and (v.get("headline") or v.get("subhead")):
            cleaned.append({
                "headline": " ".join(str(v.get("headline") or out["hero_headline"]).split())[:60],
                "subhead": " ".join(str(v.get("subhead") or out["hero_subhead"]).split())[:80],
                "badge": " ".join(str(v.get("badge") or out["hero_badge"]).split())[:30],
            })
    while len(cleaned) < 2:
        cleaned.append(dict(fallback["hero_copy_variants"][len(cleaned)]))
    out["hero_copy_variants"] = cleaned[:2]
    out["_offline_reason"] = raw.get("_offline_reason", "")
    return out


def copy_for_variant(copy: dict, variant: int) -> dict:
    """取某个主图版式对应的文案（variant 0 用主方案，1/2 用 A/B 变体）。"""
    if variant == 0:
        return copy
    variants = copy.get("hero_copy_variants") or []
    idx = variant - 1
    if 0 <= idx < len(variants):
        merged = dict(copy)
        merged.update({k: v for k, v in variants[idx].items() if v})
        return merged
    return copy


# --------------------------------------------------------------------------
# 6) 主流程
# --------------------------------------------------------------------------
def generate_copy(
    sku: str,
    engine: str | None = None,
    use_rag: bool = True,
    rewrite: bool = False,
    feedback: str = "",
    on_event: Callable[[str], None] | None = None,
) -> ListingResult:
    """为单个 SKU 生成一版文案（含合规校验）。"""
    t0 = time.time()
    product = catalog_domain.get_product(sku)
    if not product:
        raise ValueError(f"数据库里没有这个 SKU：{sku}")

    run_id = runs.start_run("listing", target=sku, engine=engine or config.DEFAULT_ENGINE)
    res = ListingResult(sku=sku, ok=False, run_id=run_id)
    _log = on_event or (lambda _m: None)

    # 2) RAG 检索品牌知识
    context = rag_bridge.RagResult(query="", mode="empty")
    if use_rag:
        _log("检索品牌知识库（底座混合检索）…")
        context = brand_context(product, k=3, rewrite=rewrite)
        res.rag_hits = len(context.hits)
        res.rag_mode = context.mode
        res.rag_citations = context.citations()
        res.rag_trace = context.trace()

    # 3) LLM 生成
    _log("调用大模型生成文案…")
    prompt = build_prompt(product, context, feedback=feedback)
    call = llm.chat(
        prompt, system=SYSTEM_PROMPT, engine=engine, temperature=0.5,
        max_tokens=2600, json_mode=True, on_event=_log,
    )
    res.engine = call.engine
    res.engine_label = call.engine_label
    parsed = llm.extract_json(call.text) if call.ok else None
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if isinstance(parsed, dict) and parsed.get("product"):
        parsed = parsed["product"]

    if not call.ok or not isinstance(parsed, dict):
        reason = call.note or ("模型返回内容无法解析为 JSON" if call.ok else "模型调用失败")
        _log(f"降级为离线模板生成：{reason[:80]}")
        parsed = offline_copy(product, reason=reason)
        res.degraded = True

    # 4) 合规校验
    normalized = _normalize(parsed, product)
    cleaned, violations = compliance.sanitize_copy(normalized)
    # 合规替换有可能把某个字段整段改空（例如 CTA 原本只有 "Buy Now"）——
    # 回填一个中性 CTA，否则主图上会缺一块内容，肉眼一看就是"渲染坏了"。
    if not (cleaned.get("cta") or "").strip():
        cleaned["cta"] = "Salon Results at Home"
    for field in ("hero_headline", "hero_subhead", "hero_badge"):
        if not (cleaned.get(field) or "").strip():
            cleaned[field] = normalized.get(field) or ""
    cleaned["sku"] = sku
    cleaned["engine"] = call.engine_label if not res.degraded else "offline:确定性模板"
    cleaned["run_id"] = run_id
    res.copy = cleaned
    res.violations = violations

    # 5) 落库成新版本
    saved = catalog_domain.save_listing_copy(cleaned)
    res.copy_id = saved["id"] if saved else None
    res.version = saved["version"] if saved else None

    res.latency_ms = int((time.time() - t0) * 1000)
    res.ok = True
    res.note = (call.note or "") if res.degraded else ""
    runs.finish_run(
        run_id, engine=res.engine_label, rag_hits=res.rag_hits,
        latency_ms=res.latency_ms, prompt_chars=call.prompt_chars,
        status="degraded" if res.degraded else "ok",
        note=(f"降级原因：{res.note[:160]};" if res.note else "") +
             (f"合规替换 {len(violations)} 处：{', '.join(v['term'] for v in violations[:5])}"
              if violations else "无违禁词"),
    )
    # 6) 文案产物落盘（除了数据库版本，还留一份 JSON，方便直接看/交付）
    copy_dir = config.OUTPUTS_DIR / "listing"
    copy_dir.mkdir(parents=True, exist_ok=True)
    copy_path = copy_dir / f"{sku}_v{res.version}.json"
    copy_path.write_text(
        json.dumps({"copy": cleaned, "violations": violations, "rag": {
            "mode": res.rag_mode, "hits": res.rag_hits, "citations": res.rag_citations,
        }, "engine": res.engine_label, "run_id": run_id},
            ensure_ascii=False, indent=2), encoding="utf-8")
    runs.add_artifact(run_id, "copy", copy_path,
                      {"version": res.version, "violations": len(violations),
                       "rag_hits": res.rag_hits})
    return res


def render_heroes(sku: str, run_id: str = "", variants: int = 3) -> list[Path]:
    """按最新一版文案渲染主图（三种版式）。"""
    product = catalog_domain.get_product(sku)
    copy = catalog_domain.latest_listing_copy(sku)
    if not product or not copy:
        return []
    paths: list[Path] = []
    for v in range(max(1, min(3, variants))):
        payload = copy_for_variant(copy, v)
        try:
            path = hero_render.compose_hero(product, payload, variant=v)
            paths.append(path)
            if run_id:
                runs.add_artifact(run_id, "hero_image", path,
                                  {"variant": v, "sku": sku, "version": copy.get("version")})
        except Exception as e:  # noqa: BLE001 - 单张图失败不应中断整批
            if run_id:
                runs.add_artifact(run_id, "hero_image_failed", f"{sku}:v{v}",
                                  {"error": f"{type(e).__name__}: {e}"})
    return paths


def generate_one(
    sku: str, variants: int = 3, engine: str | None = None,
    use_rag: bool = True, rewrite: bool = False,
    on_event: Callable[[str], None] | None = None,
) -> ListingResult:
    """单 SKU 完整链路：生成文案 + 渲染主图。"""
    res = generate_copy(sku, engine=engine, use_rag=use_rag, rewrite=rewrite, on_event=on_event)
    images = render_heroes(sku, run_id=res.run_id, variants=variants)
    res.hero_images = [str(p) for p in images]
    return res


def generate_batch(
    skus: Sequence[str] | None = None,
    variants: int = 3,
    engine: str | None = None,
    use_rag: bool = True,
    limit: int | None = None,
    on_progress: Callable[[int, int, ListingResult], None] | None = None,
) -> dict:
    """批量生成。单个 SKU 失败不影响其余 —— 批量任务必须容错。"""
    if skus is None:
        skus = [p["sku"] for p in catalog_domain.list_products()]
    skus = list(skus)[: limit or len(skus)]
    results: list[ListingResult] = []
    failures: list[dict] = []
    t0 = time.time()
    for i, sku in enumerate(skus, 1):
        try:
            res = generate_one(sku, variants=variants, engine=engine, use_rag=use_rag)
            results.append(res)
        except Exception as e:  # noqa: BLE001
            failures.append({"sku": sku, "error": f"{type(e).__name__}: {e}"})
            # ★ 必须显式回写失败：generate_copy 已经开了运行档案，
            #   异常被这里吞掉的话，那条记录会永远停在 running。
            runs.fail_current("listing", f"{sku} 生成失败：{type(e).__name__}: {e}")
            res = ListingResult(sku=sku, ok=False, note=str(e))
            results.append(res)
        if on_progress:
            on_progress(i, len(skus), res)

    images = [p for r in results for p in r.hero_images]
    summary = {
        "skus": len(skus),
        "succeeded": sum(1 for r in results if r.ok),
        "degraded": sum(1 for r in results if r.degraded),
        "failed": len(failures),
        "failures": failures,
        "hero_images": len(images),
        "violations": sum(len(r.violations) for r in results),
        "rag_hits": sum(r.rag_hits for r in results),
        "elapsed_s": round(time.time() - t0, 1),
        "results": [r.to_dict() for r in results],
    }
    _export_batch(summary)
    return summary


CSV_HEADER = ["sku", "version", "engine", "degraded", "rag_hits",
              "title", "hero_headline", "hero_subhead", "hero_badge", "cta",
              "bullet1", "bullet2", "bullet3", "bullet4", "bullet5",
              "keywords", "violations", "hero_images"]


def _csv_row(result: dict) -> list:
    c = result.get("copy") or {}
    bullets = (c.get("bullets") or []) + [""] * 5
    return [
        result["sku"], result.get("version"), result.get("engine_label"),
        result.get("degraded"), result.get("rag_hits"),
        c.get("title"), c.get("hero_headline"), c.get("hero_subhead"),
        c.get("hero_badge"), c.get("cta"), *bullets[:5],
        " / ".join(c.get("keywords") or []),
        " ; ".join(v.get("term", "") for v in result.get("violations") or []),
        " | ".join(result.get("hero_images") or []),
    ]


def _export_batch(summary: dict) -> None:
    """导出**本次批量**的产物：JSON（全量）+ CSV（给运营在表格里看）。"""
    out_dir = config.OUTPUTS_DIR / "listing"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "batch_latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (out_dir / "batch_latest.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for r in summary["results"]:
            w.writerow(_csv_row(r))


def _copy_sidecar(sku: str, version: int) -> tuple[int, list[dict]]:
    """取回某一版文案的「检索命中数 / 违禁词替换明细」。

    ★ 为什么不查数据库：`listing_copies` 只存文案本体，检索命中与合规替换
    记在运行档案（`generation_runs.rag_hits` 与 `artifacts` 的 meta）里，
    而这两项在生成时已经写进了同名的侧车 JSON（`{sku}_v{version}.json`）。
    第一版导出时把这两列直接写成 0 和空列表 —— 于是 `all_copies.csv` 里
    「检索命中」永远是 0，看的人会以为 RAG 根本没生效。
    """
    path = config.OUTPUTS_DIR / "listing" / f"{sku}_v{version}.json"
    if not path.is_file():
        return 0, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, []
    hits = int(((data.get("rag") or {}).get("hits")) or 0)
    viol = data.get("violations") or []
    return hits, (viol if isinstance(viol, list) else [])


def export_all_latest() -> Path:
    """导出**全部 SKU 各自最新一版**的文案，写 `outputs/listing/all_copies.csv`。

    ★ 为什么与 batch_latest 分开：批量生成一次通常只覆盖几个 SKU
    （比如"周报判定根因在内容侧"的那 4 个）。如果文档/作品集引用的样例是
    `batch_latest.csv`，它就只有 4 行 —— 观众看不到"20 个 SKU 都跑通了"。
    这个坑和主图总览图被覆盖成 4 个 SKU 是同一类：**"最新一次"不等于"全部现状"**。
    """
    out_dir = config.OUTPUTS_DIR / "listing"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[list] = []
    payload: list[dict] = []
    for p in catalog_domain.list_products():
        copy = catalog_domain.latest_listing_copy(p["sku"])
        if not copy:
            continue
        hero_dir = config.OUTPUTS_DIR / "hero_images" / p["sku"]
        images = sorted(str(q) for q in hero_dir.glob("*.png")) if hero_dir.is_dir() else []
        rag_hits, violations = _copy_sidecar(p["sku"], int(copy["version"]))
        rows.append(_csv_row({
            "sku": p["sku"], "version": copy["version"], "engine_label": copy["engine"],
            "degraded": str(copy["engine"]).startswith("offline"),
            "rag_hits": rag_hits, "copy": copy, "violations": violations,
            "hero_images": images,
        }))
        payload.append({"sku": p["sku"], "version": copy["version"],
                        "engine": copy["engine"], "copy": copy,
                        "rag_hits": rag_hits, "violations": violations,
                        "hero_images": images})

    csv_path = out_dir / "all_copies.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    (out_dir / "all_copies.json").write_text(
        json.dumps({"skus": len(payload), "items": payload},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return csv_path


def contact_sheet_for(skus: Sequence[str] | None = None, variant: int = 0,
                      out_name: str | None = None) -> Path | None:
    """把一批主图拼成总览图（作品集展示用）。"""
    out_dir = config.OUTPUTS_DIR / "hero_images"
    paths: list[Path] = []
    for sku in (skus or [p["sku"] for p in catalog_domain.list_products()]):
        hits = sorted((out_dir / sku).glob("*.png")) if (out_dir / sku).is_dir() else []
        names = {0: "clean", 1: "banner", 2: "split"}
        target = [p for p in hits if names.get(variant, "") in p.stem]
        if target:
            paths.append(target[0])
    if not paths:
        return None
    out = config.OUTPUTS_DIR / "hero_images" / (out_name or f"contact_sheet_v{variant}.png")
    return hero_render.contact_sheet(paths, out, cols=4, cell=330)
