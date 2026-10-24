# -*- coding: utf-8 -*-
"""文案合规校验：把平台红线做成可执行的规则，而不是靠人记。

为什么这一步必须与 LLM 分开
--------------------------
"不要让模型说违禁词"这件事**不能交给模型自己保证**。大模型在"把卖点写得更有力"
的指令下，天然倾向于生成 `best`、`#1`、`FDA approved`、`cures fungal infections`
这类表述 —— 而这些恰好是亚马逊美妆类目最容易被下架/冻结的雷区。
所以生成完之后必须过一遍**确定性规则**，命中的词一律替换，并把命中记录进产物，
让人知道"这版文案被规则改过哪里"。模型负责好听，规则负责不出事。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Violation", "RULES", "check", "sanitize", "BANNED_SUMMARY"]


@dataclass
class Violation:
    term: str
    category: str
    reason: str
    replacement: str
    severity: str      # high / medium

    def to_dict(self) -> dict:
        return {
            "term": self.term, "category": self.category, "reason": self.reason,
            "replacement": self.replacement, "severity": self.severity,
        }


# (正则, 类别, 原因, 替换建议, 严重度)
RULES: list[tuple[str, str, str, str, str]] = [
    # ---- 绝对化用语：亚马逊明确禁止无依据的"最好/第一" ----
    (r"\bthe\s+best\b", "绝对化用语", "无依据的最高级表述，属于亚马逊禁止的夸大宣传", "a great", "high"),
    (r"\bbest[- ]selling\b", "绝对化用语", "销量第一的声称需要平台数据支撑", "popular", "high"),
    (r"\bworld'?s\s+best\b", "绝对化用语", "全球最佳属无法证实的绝对化表述", "excellent", "high"),
    (r"#\s?1\b", "绝对化用语", "排名第一的声称需可验证依据", "a top choice", "high"),
    (r"\bnumber\s+one\b", "绝对化用语", "排名第一的声称需可验证依据", "a leading", "high"),
    (r"\bno\.?\s?1\b", "绝对化用语", "排名第一的声称需可验证依据", "a leading", "high"),
    (r"\btop\s+rated\b", "绝对化用语", "需平台徽章支持，自述不成立", "highly rated", "medium"),
    (r"\bguaranteed\b", "绝对化用语", "保证性承诺无法兑现，易触发纠纷", "designed to", "high"),
    (r"\b100\s?%\s*(?:safe|effective|natural|cure)\b", "绝对化用语", "100% 承诺不可证实", "gentle", "high"),
    (r"\bnever\s+fails?\b", "绝对化用语", "绝对化承诺", "reliable", "medium"),
    (r"\bperfect\s+for\s+everyone\b", "绝对化用语", "面向所有人属夸大适用范围", "great for most", "medium"),
    # ★ 兜底的裸词规则，必须放在上面几条"更具体"的规则**之后**：
    #   `sanitize()` 是按列表顺序做替换的，顺序反了会把 "the best" 先改成 "the great"，
    #   虽然结果也安全，但就丢掉了更贴切的替换词。
    #   踩过的坑：第一版只有 `\bthe\s+best\b` / `\bbest[- ]selling\b`，
    #   于是后台搜索词里的裸 "best nail lamp" 一路放行 —— 测试直接把它抓出来了。
    (r"\bbest\b", "绝对化用语", "无依据的最高级表述，标题/五点/后台搜索词都不允许", "great", "medium"),

    # ---- 医疗功效：美甲/美妆产品不得宣称治疗、杀菌 ----
    #
    # ★ 这里有一条血泪教训：`\bcures?\b` 这种"见词就杀"的规则在美甲品类里是**错的**。
    #   "cure" 在这个行业里最常用的意思是「固化」（UV/LED 灯固化甲油胶），
    #   属于完全合法的产品功能描述。第一版规则把它当医疗宣称一律替换，
    #   于是模型写出的 "60-second cure time" 被改成了
    #   "60-second helps improve the look of time" —— 违禁词是没了，文案也毁了。
    #   修法：违规判定必须带**上下文**，只拦"cure + 疾病"这种真正的医疗宣称组合。
    (r"\bcures?\s+(?:fungal|fungus|infections?|nail\s+disease|athlete'?s\s+foot|psoriasis|eczema)\b",
     "医疗功效宣称", "宣称治愈真菌/感染属医疗器械宣称，美妆类目禁止",
     "helps keep nails looking clean", "high"),
    (r"\bcuring\s+(?:fungal|fungus|infections?|nail\s+disease)\b",
     "医疗功效宣称", "治愈类宣称属医疗范畴", "helping keep nails clean", "high"),
    (r"\bcures?\s+(?:your\s+)?(?:nail\s+)?(?:fungus|infection)\b",
     "医疗功效宣称", "治愈指甲真菌属医疗宣称", "helps keep nails looking clean", "high"),
    (r"\btreats?\s+(?:fungal|fungus|infection|infections|nail\s+disease)\b", "医疗功效宣称",
     "宣称治疗真菌/感染属医疗器械宣称，非美妆类目可宣称范围", "helps keep nails looking clean", "high"),
    (r"\bheals?\b", "医疗功效宣称", "愈合属医疗功效", "helps condition", "high"),
    (r"\bantifungal\b", "医疗功效宣称", "抗真菌属药品宣称", "hygienic", "high"),
    (r"\bantibacterial\b", "医疗功效宣称", "抗菌属功效宣称，需资质支撑", "clean", "high"),
    (r"\bkills?\s+(?:bacteria|germs|fungus)\b", "医疗功效宣称", "杀菌属医疗/消毒产品宣称", "helps keep tools clean", "high"),
    (r"\bsterili[sz]es?\b", "医疗功效宣称", "灭菌属医疗器械宣称，UV 灯不得如此表述", "sanitizes", "high"),
    (r"\bfda\s+approved\b", "认证宣称", "FDA 不批准美甲灯具/甲油胶，此表述属虚假宣称", "meets US safety requirements", "high"),
    (r"\bce\s+approved\b", "认证宣称", "CE 是厂商自我声明、并非官方批准，表述不准确", "CE marked", "medium"),
    (r"\bmedical\s+grade\b", "认证宣称", "医用级需资质，美甲产品不适用", "salon quality", "high"),

    # ---- 侵权与比较：点名竞品/贬低对手 ----
    (r"\bbetter\s+than\s+(?:sunuv|melodysusie|modelones|beetles|opi)\b", "侵权与不当比较",
     "点名比较竞品易触发商标投诉", "a strong alternative in its class", "high"),
    (r"\bcheaper\s+than\b", "侵权与不当比较", "贬低式比较易被投诉", "great value compared with salon visits", "medium"),
    (r"\bknock[- ]?off\b", "侵权与不当比较", "暗示仿制品", "style", "high"),

    # ---- 促销与诱导：主图/标题不得出现促销信息 ----
    #   替换词要"有话说"而不是删空：删空会让 CTA 字段变成空字符串，
    #   主图上就少一块内容。给一个中性且合规的替代说法。
    (r"\bfree\s+shipping\b", "促销信息", "促销信息不得出现在主图与标题，属 listing 规范问题", "ready to ship", "medium"),
    (r"\blimited\s+time\s+offer\b", "促销信息", "限时促销不得出现在主图/标题", "shop the collection", "medium"),
    (r"\b50%\s*off\b", "促销信息", "折扣信息不得出现在主图/标题", "great value", "medium"),
    (r"\bbuy\s+now\b", "促销信息", "诱导性行动号召不得出现在主图", "shop the set", "medium"),
    (r"\bbest\s+price\b", "促销信息", "价格最优的声称不可验证且属促销表述", "great value", "medium"),
]

_COMPILED = [(re.compile(p, re.I), cat, reason, repl, sev) for p, cat, reason, repl, sev in RULES]

BANNED_SUMMARY = "、".join(sorted({cat for _p, cat, _r, _repl, _s in RULES}))


def check(text: str) -> list[Violation]:
    """检查一段文案，返回全部命中的违规项。

    按**命中位置**去重：`the best` 会同时被"the best"和兜底的裸词"best"两条规则命中，
    如果不去重，同一个词会在报告里出现两次，看起来像两个问题。
    规则表里越靠前的越具体，因此保留先命中的那一条。
    """
    if not text:
        return []
    found: list[tuple[int, Violation]] = []
    accepted: list[tuple[int, int]] = []
    for pattern, category, reason, repl, sev in _COMPILED:
        for m in pattern.finditer(text):
            start, end = m.start(), m.end()
            # 重叠去重（不只是"位置完全相同"）：
            # "The best lamp" 里 `\bthe\s+best\b` 命中 (0,8)，`\bbest\b` 命中 (4,8)，
            # 两者区间不同但重叠 —— 按完全相同去重是漏的，会重复报同一个词。
            if any(not (end <= s or start >= e) for s, e in accepted):
                continue
            accepted.append((start, end))
            found.append((start, Violation(
                term=m.group(0), category=category, reason=reason,
                replacement=repl, severity=sev)))
    found.sort(key=lambda pair: pair[0])
    return [v for _pos, v in found]


def sanitize(text: str) -> tuple[str, list[Violation]]:
    """替换全部违规词，返回 (改写后文本, 被改掉的项)。

    ReDoS 安全性：所有正则都是"字面量 + \\b + 可选空格"的形态，没有嵌套量词，
    对超长文本也不会指数回溯。
    """
    violations = check(text)
    fixed = text or ""
    for pattern, _cat, _reason, repl, _sev in _COMPILED:
        fixed = pattern.sub(repl, fixed)
    fixed = re.sub(r"\s{2,}", " ", fixed).strip()
    fixed = re.sub(r"\s+([,.;:!?])", r"\1", fixed)
    return fixed, violations


def sanitize_copy(copy: dict) -> tuple[dict, list[dict]]:
    """对整份文案（标题/主图文案/五点/描述/关键词）跑合规替换。

    返回 (清洗后的 copy, 全部违规记录)。违禁词在**关键词里也不允许存在** ——
    很多人只检查标题正文，忘了后台搜索词同样会被平台审核。
    """
    records: list[dict] = []
    out = dict(copy)

    def _clean(value: str, field: str) -> str:
        new, vs = sanitize(value)
        for v in vs:
            records.append({**v.to_dict(), "field": field})
        return new

    for field in ("title", "hero_headline", "hero_subhead", "hero_badge", "cta", "description"):
        if isinstance(out.get(field), str):
            out[field] = _clean(out[field], field)
    if isinstance(out.get("bullets"), list):
        out["bullets"] = [_clean(str(b), "bullets") for b in out["bullets"]]
    if isinstance(out.get("keywords"), list):
        cleaned: list[str] = []
        for kw in out["keywords"]:
            text, vs = sanitize(str(kw))
            for v in vs:
                records.append({**v.to_dict(), "field": "keywords"})
            if text and text not in cleaned:
                cleaned.append(text)
        out["keywords"] = cleaned
    return out, records
