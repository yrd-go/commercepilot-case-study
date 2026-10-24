# -*- coding: utf-8 -*-
"""RAG 桥接层：把电商知识库接到 my-agent 的 RAG/Agent 底座上。

复用关系（不是"重新实现了一遍"，是真的在调底座的函数）
------------------------------------------------------
    rag_demo.build_embeddings()            -> 智谱 embedding-3 / Ollama 双引擎向量化
    rag_demo.build_or_load_vectorstore()   -> Chroma 持久化向量库 + 维度一致性校验
    rag_demo.split_knowledge_base()        -> 中文感知切分（chunk 500 / overlap 50）
    rag_demo.extract_keywords/build_terms  -> 关键词抽取（CJK bigram + 拉丁词，带停用词）
    rag_demo.rewrite_query()               -> 查询改写（LLM 把口语问题改写成检索词）
    rag_demo.retrieve_hybrid()             -> 双路向量召回 ∪ 关键词 IDF-lite 加分，融合排序
    rag_demo.Hit                           -> 检索结果结构

做法是**改底座模块的 3 个全局变量**（`KB_FILE` / `COLLECTION` / `CHROMA_DIR`）
再调它的函数，让底座的引擎为电商语料服务；底座的 `emotional_handbook` 集合与
`data/chroma_db` 目录**完全不受影响**，两边向量库物理隔离。

为什么不直接改底座源码
----------------------
底座是另一个项目的线上代码，改它会把情绪陪伴那条链路一起改坏。桥接层只做
"注入配置 + 调用"，底座升级时这里不用跟着改。
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

_LOCK = threading.RLock()
_STATE: dict[str, Any] = {"module": None, "db": None, "engine": None, "error": None}
_RANGES: list[dict] = []

FILE_MARK_RE = re.compile(r"【知识库文件】\s*(.+?\.md)")
SECTION_MARK = "# ============================================================"

# 合并格式版本号。
#   ★ 教训：合并逻辑（`merge_kb`）的缓存签名原本只看源文件的 mtime + 大小。
#   改了合并**代码**但源文件没变时，缓存判定"没变化"直接返回旧文件，于是
#   行号区间索引一直是错的，而表面上看不出任何异常（检索能出结果，只是来源标错）。
#   把格式版本号并进签名，改一次代码就自动失效一次缓存。
MERGE_FORMAT_VERSION = 3

# --------------------------------------------------------------------------
# 查询改写的提示词：底座的 REWRITE_PROMPT 是**IT 服务台**专用的
#   （"企业内部 IT 服务台的检索助手"、"《IT 运维手册》"、"错误码 Q2-05"）。
#   直接拿来做电商检索，模型会把"美甲灯卖点"往"故障排查"方向改写，命中率反而下降。
#   所以桥接层替换掉这一段提示词，但**保留底座配套的 JSON 契约与四级容错解析**
#   （parse_rewrite_payload / _sanitize_rewrite）—— 那才是真正难写、真正值钱的部分。
#   契约必须完全一致：一行 JSON，字段名 query / keywords。
# --------------------------------------------------------------------------
ECOM_REWRITE_PROMPT = (
    "你是 Saviland（美甲美妆品牌）电商知识库的检索助手。请把运营同学的口语化提问"
    "改写成适合在《品牌知识库》中检索的查询词，并抽取关键词。\n"
    "《品牌知识库》包含：品牌定位与产品线、各类目卖点库、美甲灯技术要点、亚马逊关键词库、"
    "合规与禁用词、竞品对比、买家痛点与使用场景、20 个 SKU 的产品档案。\n"
    "【要求】\n"
    "1. 只做改写与抽词，不要回答问题，不要输出运营建议。\n"
    "2. 不得编造提问里没有的 SKU、参数、价格或数字；可以补充同义与上位词，"
    "例如“灯”可补成“美甲灯 固化灯 UV LED 功率”。\n"
    "3. 检索词不超过 30 个字，多个词之间用空格分隔。\n"
    "4. 关键词 3~8 个，优先品牌名、类目名、SKU 编码、合规术语"
    "（禁用词/绝对化用语/医疗功效/认证宣称）、平台规则词与竞品名。\n"
    "5. 只输出一行 JSON，不要解释，不要 Markdown 代码块，格式严格为：\n"
    '{"query": "改写后的检索词", "keywords": ["关键词1", "关键词2"]}\n'
    "【运营提问】\n{question}\n"
)


# --------------------------------------------------------------------------
# 1) 知识库合并：底座的切分器只吃单个文件，所以先把多份 md 合成一份
# --------------------------------------------------------------------------
def _product_sections() -> str:
    """把产品主数据渲染成一节知识库文本。

    这一步很关键：品牌知识库讲的是"品类怎么卖"，产品档案讲的是"这一个 SKU
    到底有什么参数"。文案生成时这两类信息缺一不可，所以都进同一个向量库。
    """
    path = config.PRODUCTS_JSON
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    products = data.get("products") or []
    if not products:
        return ""
    parts = [SECTION_MARK, "# 【知识库文件】08-产品档案.md", SECTION_MARK, ""]
    for p in products:
        specs = p.get("specs") or {}
        specs_txt = "；".join(f"{k}={v}" for k, v in specs.items()) if isinstance(specs, dict) else str(specs)
        parts += [
            f"## {p.get('sku')} · {p.get('title_cn')}（{p.get('title_en') or ''}）",
            f"- 品类：{p.get('category')} / {p.get('sub_category') or '-'}",
            f"- 售价：{p.get('price_usd')} 美元；成本：{p.get('cost_usd')} 美元",
            f"- 评分：{p.get('rating')}（{p.get('review_count')} 条评论）",
            f"- 规格：{specs_txt}",
            f"- 目标人群：{p.get('target_audience') or '-'}",
        ]
        for label, key in (("核心卖点", "selling_points"), ("五点描述", "five_bullets"),
                           ("买家痛点", "pain_points"), ("使用场景", "use_scenarios"),
                           ("投放关键词", "keywords")):
            values = p.get(key) or []
            if values:
                parts.append(f"- {label}：" + "；".join(str(v) for v in values))
        comps = p.get("competitors") or []
        if comps:
            comp = "；".join(
                f"{c.get('name')}（{c.get('price_usd')} 美元，短板：{c.get('weakness')}）"
                for c in comps if isinstance(c, dict)
            )
            if comp:
                parts.append(f"- 竞品对比：{comp}")
        parts.append("")
    return "\n".join(parts)


def merge_kb(force: bool = False) -> tuple[Path, int]:
    """把 brand_kb/*.md + 产品档案合并成一份给底座切分器吃。返回 (路径, 源文件数)。

    同时写一份 `kb_merged.index.json`，记录每个源文件在合并文件里的**行号区间**。
    为什么需要它：底座的切分器把长文切成 500 字的片段，一个片段通常不包含文件头，
    所以没法靠"片段里有没有文件标记"来判断来源。有了行号区间，就能用片段自带的
    `line_start` 反查它属于哪个文件 —— 报告里的引用来源才不会退化成"未知来源"。
    """
    sources: list[Path] = sorted(config.KB_DIR.glob("*.md"))
    signature = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sources]
    products_sig = (config.PRODUCTS_JSON.stat().st_mtime_ns
                    if config.PRODUCTS_JSON.is_file() else 0)
    cache = config.KB_MERGED.with_suffix(".sig")
    index_path = config.KB_MERGED.with_suffix(".index.json")
    sig_text = f"v{MERGE_FORMAT_VERSION}|" + repr(signature) + f"|products:{products_sig}"

    if not force and config.KB_MERGED.is_file() and cache.is_file() and index_path.is_file():
        try:
            if cache.read_text(encoding="utf-8") == sig_text:
                return config.KB_MERGED, len(sources)
        except OSError:
            pass

    lines: list[str] = []
    ranges: list[dict] = []

    def _line_no() -> int:
        """当前已写入内容的总行数（1-based）。"""
        text = "\n".join(lines)
        return text.count("\n") + 1 if text else 0

    def _add_block(file_name: str, body: str) -> None:
        # ★ 必须按**真实换行符个数**计数，不能按列表元素个数。
        #   一个 markdown 文件的正文是 1 个含大量 \n 的字符串，用 len(lines) 数
        #   会把 1000 行的知识库算成"每个文件 6 行"，区间索引全部错位 ——
        #   症状是检索结果的来源全部显示"未定位到具体文件"。真实事故，已修。
        start = _line_no() + 1
        lines.extend([SECTION_MARK, f"# 【知识库文件】{file_name}", SECTION_MARK, "", body, ""])
        ranges.append({"file": file_name, "start": start, "end": _line_no()})

    for p in sources:
        try:
            body = p.read_text(encoding="utf-8-sig", errors="replace").strip()
        except OSError:
            continue
        _add_block(p.name, body)

    product_text = _product_sections()
    if product_text:
        _add_block("08-产品档案.md", product_text)

    config.KB_MERGED.write_text("\n".join(lines), encoding="utf-8")
    try:
        index_path.write_text(json.dumps({"ranges": ranges}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError:
        pass
    try:
        cache.write_text(sig_text, encoding="utf-8")
    except OSError:
        pass
    return config.KB_MERGED, len(ranges)


# --------------------------------------------------------------------------
# 2) 桥接到底座模块
# --------------------------------------------------------------------------
def _load_module():
    """导入底座 rag_demo，并把它指向电商的知识库与向量库目录。"""
    global _RANGES
    with _LOCK:
        if _STATE["module"] is not None:
            return _STATE["module"]
        base_src = str(config.BASE_AGENT_SRC)
        if not Path(base_src).is_dir():
            raise RuntimeError(
                f"找不到 RAG/Agent 底座：{base_src}\n"
                "       可用环境变量 SAVILAND_BASE_AGENT 指定底座仓库根目录。"
            )
        if base_src not in sys.path:
            sys.path.insert(0, base_src)
        module = importlib.import_module("rag_demo")

        merged, _n = merge_kb()
        _RANGES = _load_ranges()
        # ★ 桥接的核心：只改配置，不改底座源码
        module.KB_FILE = str(merged)
        module.COLLECTION = config.KB_COLLECTION
        module.CHROMA_DIR = Path(config.CHROMA_DIR)
        module.REWRITE_PROMPT = ECOM_REWRITE_PROMPT   # 域适配：IT 服务台 -> 电商运营

        _STATE["module"] = module
        return module


def _kb_fingerprint() -> str:
    """知识库内容指纹。用来判断向量库是不是"用旧知识库建的"。"""
    import hashlib
    if not config.KB_MERGED.is_file():
        return ""
    return hashlib.sha1(config.KB_MERGED.read_bytes()).hexdigest()


def _index_fingerprint_path() -> Path:
    return Path(config.CHROMA_DIR) / ".kb_fingerprint"


@dataclass
class RagHit:
    """一条检索结果（对底座 Hit 的轻量投影，附带来源文件名）。"""
    content: str
    score: float
    vec_sim: float
    kw_score: float
    line_start: int
    line_end: int
    source: str
    matched: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "vec_sim": round(self.vec_sim, 4),
            "kw_score": round(self.kw_score, 4),
            "source": self.source,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "matched": self.matched,
            "recall": self.sources,
            "preview": self.content[:160].replace("\n", " "),
        }


@dataclass
class RagResult:
    query: str
    hits: list[RagHit] = field(default_factory=list)
    mode: str = "empty"          # hybrid / keyword-only / empty / unavailable
    rewritten: str = ""
    keywords: list[str] = field(default_factory=list)
    engine: str = ""
    latency_ms: int = 0
    note: str = ""
    pool_size: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.hits)

    def context(self, limit: int | None = None, max_chars: int = 2600) -> str:
        """拼成喂给 LLM 的上下文；带来源标注，方便人工核对引用。"""
        parts: list[str] = []
        used = 0
        for i, hit in enumerate(self.hits[: (limit or len(self.hits))], 1):
            block = f"[片段{i}｜来源：{hit.source}｜相关度{hit.score:.3f}]\n{hit.content.strip()}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)

    def citations(self) -> list[str]:
        seen: list[str] = []
        for hit in self.hits:
            if hit.source not in seen:
                seen.append(hit.source)
        return seen

    def trace(self) -> list[dict]:
        return [h.to_dict() for h in self.hits]


def _load_ranges() -> list[dict]:
    """读合并文件的行号区间索引（用于把片段反查回源文件）。"""
    index_path = config.KB_MERGED.with_suffix(".index.json")
    if not index_path.is_file():
        return []
    try:
        return json.loads(index_path.read_text(encoding="utf-8")).get("ranges", [])
    except (OSError, json.JSONDecodeError):
        return []


def _source_of(content: str, line_start: int = -1) -> str:
    """判断一个片段来自哪个知识库文件。

    优先用行号区间反查（准确），退化时用片段里的文件标记，最后兜底为"品牌知识库"。
    """
    for r in _RANGES:
        if line_start >= 0 and r["start"] <= line_start <= r["end"]:
            return r["file"]
    m = FILE_MARK_RE.search(content)
    if m:
        return m.group(1)
    return "品牌知识库（未定位到具体文件）"


def _term_label(term: Any) -> str:
    """底座的关键词结构是 (词, 权重) 二元组；这里统一成可展示的字符串。

    不做这层归一就会踩到 `TypeError: sequence item 0: expected str instance, tuple found`
    —— 底座的 `extract_keywords()` 返回 `[(词, 权重)]`，直接 join 会崩。
    """
    if isinstance(term, (tuple, list)) and term:
        return str(term[0])
    return str(term)


def _project(hits: list) -> list[RagHit]:
    out: list[RagHit] = []
    for h in hits:
        doc = getattr(h, "doc", None)
        content = (getattr(doc, "page_content", "") or "")
        line_start = int(getattr(h, "line_start", -1) or -1)
        out.append(RagHit(
            content=content,
            score=float(getattr(h, "final_score", 0.0) or 0.0),
            vec_sim=float(getattr(h, "vec_sim", 0.0) or 0.0),
            kw_score=float(getattr(h, "kw_score", 0.0) or 0.0),
            line_start=line_start,
            line_end=int(getattr(h, "line_end", -1) or -1),
            source=_source_of(content, line_start),
            matched=[_term_label(t) for t in (getattr(h, "matched", []) or [])],
            sources=list(getattr(h, "sources", []) or []),
        ))
    return out


# --------------------------------------------------------------------------
# 3) 建库 / 检索
# --------------------------------------------------------------------------
def ensure_index(rebuild: bool = False, embedder: str = "zhipu") -> dict:
    """确保向量库就绪。返回索引信息（不抛异常，失败信息在 error 里）。

    自带**自愈**：如果知识库内容变了（改了 md、加了产品档案），而向量库还是旧的，
    这里会自动重建。否则会出现"检索结果和文档对不上"这种最难查的问题 ——
    向量库里存的是旧文本，但人看的是新文档。
    """
    with _LOCK:
        try:
            module = _load_module()
            merge_kb()
            global _RANGES
            _RANGES = _load_ranges()

            fingerprint = _kb_fingerprint()
            marker = _index_fingerprint_path()
            stale = False
            if Path(config.CHROMA_DIR).is_dir() and fingerprint:
                if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != fingerprint:
                    stale = True
            if stale:
                print("[信息] 知识库内容已变化，自动重建向量库 …")
                rebuild = True

            try:
                db = module.build_or_load_vectorstore(embedder, rebuild)
            except SystemExit as e:      # 底座的 die() 会 sys.exit，这里必须接住
                _STATE["error"] = f"底座建库中止（exit {e.code}），通常是 Key 缺失或网络不可用"
                _STATE["db"] = None
                return stats()
            _STATE["db"] = db
            _STATE["engine"] = embedder
            _STATE["error"] = None
            if fingerprint:
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(fingerprint, encoding="utf-8")
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001
            _STATE["error"] = f"{type(e).__name__}: {e}"
            _STATE["db"] = None
        return stats()


def _get_db():
    if _STATE["db"] is None:
        ensure_index()
    return _STATE["db"]


def search(
    query: str,
    k: int = 5,
    keyword_weight: float | None = None,
    rewrite: bool = False,
    allow_fallback: bool = True,
) -> RagResult:
    """混合检索。底座的向量召回失败时自动降级为关键词召回（不抛异常）。"""
    module = None
    try:
        module = _load_module()
    except Exception as e:  # noqa: BLE001
        return RagResult(query=query, mode="unavailable", note=f"底座不可用：{e}")

    kw = module.DEFAULT_KEYWORD_WEIGHT if keyword_weight is None else keyword_weight
    t0 = time.time()
    result = RagResult(query=query, engine=_STATE.get("engine") or "zhipu")

    # 查询改写（可选，会多花一次 LLM 调用）
    rewritten = ""
    llm_keywords: list[str] = []
    if rewrite:
        try:
            rw = module.rewrite_query(query)
            rewritten = (getattr(rw, "rewritten_query", "") or "").strip()
            llm_keywords = list(getattr(rw, "keywords", []) or [])
            result.rewritten = rewritten
        except Exception as e:  # noqa: BLE001
            result.note = f"查询改写失败（{type(e).__name__}），已用原问题检索；"

    try:
        terms = module.build_terms(query, llm_keywords or None)
    except Exception:  # noqa: BLE001
        terms = []
    result.keywords = [_term_label(t) for t in list(terms)[:12]]

    db = _get_db()
    if db is None:
        result.mode = "unavailable"
        result.note += _STATE.get("error") or "向量库未就绪"
        result.latency_ms = int((time.time() - t0) * 1000)
        return result

    candidate_k = max(k * module.CANDIDATE_K_FACTOR, module.CANDIDATE_K_MIN)
    try:
        top, pool = module.retrieve_hybrid(db, query, rewritten, terms, k, kw, candidate_k)
        result.hits = _project(top)
        result.pool_size = len(pool)
        result.mode = "hybrid"
    except Exception as e:  # noqa: BLE001
        if not allow_fallback:
            result.mode = "unavailable"
            result.note += f"混合检索失败：{type(e).__name__}: {str(e)[:150]}"
            result.latency_ms = int((time.time() - t0) * 1000)
            return result
        # 底座的降级路径：本地关键词召回
        try:
            hits, pool, _mode, _note = module.retrieve_with_fallback(
                db, query, rewritten, terms, k, kw, candidate_k
            )
            result.hits = _project(hits)
            result.pool_size = len(pool)
            result.mode = "keyword-only"
            result.note += f"向量检索不可用（{type(e).__name__}），已降级为关键词召回；"
        except Exception as e2:  # noqa: BLE001
            result.mode = "unavailable"
            result.note += f"关键词召回同样失败：{type(e2).__name__}: {str(e2)[:150]}"
    result.latency_ms = int((time.time() - t0) * 1000)
    return result


def ensure_loaded() -> dict:
    """只把**已存在的**索引加载进来，不重建。

    为什么需要：`stats()` 在冷进程里返回 0 个片段（Chroma 还没打开），
    于是界面上会显示「知识片段 0」—— 看起来像知识库没建好。
    索引其实一直躺在 `data/chroma_db` 里，只是这个进程还没去开它。
    这个函数把"打开已存在的库"和"重建库"区分开。

    ★ 必须是**双重检查**：启动预热线程和第一个 API 请求会同时进来。
    只判断一次的话，第二个线程会在锁里把整个库**再加载一遍**
    （日志里会出现两行「复用已有向量库」），实测把首屏从 4 秒拖到 13 秒。
    """
    if _STATE["db"] is not None or _STATE.get("error") is not None:
        return stats()
    with _LOCK:                     # 与 ensure_index 同一把 RLock，可重入
        if _STATE["db"] is None and _STATE.get("error") is None:
            ensure_index(rebuild=False)
    return stats()


def stats() -> dict:
    """向量库现状 —— 后台首页和自检脚本都用它。"""
    info: dict[str, Any] = {
        "collection": config.KB_COLLECTION,
        "chroma_dir": str(config.CHROMA_DIR),
        "kb_merged": str(config.KB_MERGED),
        "kb_files": len(list(config.KB_DIR.glob("*.md"))),
        "kb_chars": config.KB_MERGED.stat().st_size if config.KB_MERGED.is_file() else 0,
        "chunks": 0,
        "embedder": _STATE.get("engine"),
        "ready": False,
        "error": _STATE.get("error"),
        "base_agent": str(config.BASE_AGENT_ROOT),
        "base_agent_src": str(config.BASE_AGENT_SRC),
    }
    db = _STATE.get("db")
    if db is None:
        return info
    try:
        module = _STATE["module"]
        count, stored = module._index_info(db)
        info["chunks"] = int(count or 0)
        info["embedder"] = stored or _STATE.get("engine")
        info["ready"] = info["chunks"] > 0
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def reset_index() -> dict:
    """删掉电商向量库并重建（不动底座的 emotional_handbook 目录）。"""
    with _LOCK:
        _STATE["db"] = None
        return ensure_index(rebuild=True)
