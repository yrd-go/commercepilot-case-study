# -*- coding: utf-8 -*-
"""Saviland 电商 AI 系统 —— 全局配置与路径解析。

设计要点
--------
1. **所有路径都是绝对路径**，与运行时的工作目录无关（Streamlit / CLI / 测试
   三种入口的 cwd 各不相同，用相对路径会踩坑）。
2. **密钥加载顺序**：本项目的 `.env` 优先，其次回落到 RAG/Agent 底座
   （`my-agent/.env`）—— 电商系统不复制一份密钥，直接复用底座的环境。
   真实的系统环境变量优先级最高（符合 12-factor，也方便 CI 注入）。
3. 底座位置可用环境变量 `SAVILAND_BASE_AGENT` 覆盖，方便换机器。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 控制台编码守卫：Windows 默认 GBK 终端打印中文/emoji 时会 UnicodeEncodeError
# --------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 某些被重定向的流不支持 reconfigure
        pass


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]          # <repo>/src/saviland/config.py -> <repo>
SRC_DIR = ROOT / "src"
DATA_DIR = ROOT / "data"
CATALOG_DIR = DATA_DIR / "catalog"
KB_DIR = CATALOG_DIR / "brand_kb"
KB_MERGED = CATALOG_DIR / "brand_kb_merged.md"      # 7 个 md 合并成一份，喂给底座切分器
PRODUCTS_JSON = CATALOG_DIR / "products.json"
ADS_DIR = DATA_DIR / "ads"
CHROMA_DIR = DATA_DIR / "chroma_db"
DB_PATH = DATA_DIR / "saviland.db"
ASSETS_DIR = ROOT / "assets" / "products"
OUTPUTS_DIR = ROOT / "outputs"
DOCS_DIR = ROOT / "docs"
TOOLS_DIR = ROOT / "tools"

for _d in (DATA_DIR, CATALOG_DIR, KB_DIR, ADS_DIR, ASSETS_DIR, OUTPUTS_DIR, DOCS_DIR, TOOLS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# RAG/Agent 底座（复用其 embedding / Chroma / 混合检索 / 查询改写）
BASE_AGENT_ROOT = Path(
    os.getenv("SAVILAND_BASE_AGENT", str(Path.home() / "my-agent"))
).expanduser()
BASE_AGENT_SRC = BASE_AGENT_ROOT / "src"
BASE_AGENT_ENV = BASE_AGENT_ROOT / ".env"

# 本项目自己的 .env（可选；不存在也不影响，密钥可全走底座）
OWN_ENV = ROOT / ".env"

# 底座里的品牌知识库集合名（与底座的 emotional_handbook 分开，互不污染）
KB_COLLECTION = "saviland_brand_kb"


# --------------------------------------------------------------------------
# .env 加载（不覆盖已存在的系统环境变量）
# --------------------------------------------------------------------------
def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        # utf-8-sig：底座 .env 的模板注释里明确要求无 BOM，但真出现 BOM 也不能崩
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


ENV_SOURCES: list[str] = []


def load_env() -> dict[str, str]:
    """加载 .env：本项目优先，底座兜底；已存在的系统环境变量不覆盖。"""
    merged: dict[str, str] = {}
    # 底座在前、本项目在后 —— 后写入的同名键覆盖前者，即本项目优先
    for path in (BASE_AGENT_ENV, OWN_ENV):
        values = _parse_env_file(path)
        if values:
            ENV_SOURCES.append(str(path))
        merged.update(values)

    for key, value in merged.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
    return merged


_ENV = load_env()


# --------------------------------------------------------------------------
# 模型配置
# --------------------------------------------------------------------------
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "").strip()
ZHIPU_CHAT_MODEL = os.getenv("ZHIPU_CHAT_MODEL", "glm-4-flash")
ZHIPU_EMBED_MODEL = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_CHAT_MODEL = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-chat")

# 默认引擎顺序：智谱便宜档打底（文案/改写），DeepSeek 用于需要更强推理的周报结论
DEFAULT_ENGINE = os.getenv("SAVILAND_ENGINE", "zhipu")


def has_zhipu() -> bool:
    return bool(ZHIPU_API_KEY)


def has_deepseek() -> bool:
    return bool(DEEPSEEK_API_KEY)


def online_available() -> bool:
    """是否具备真实调用大模型的条件（任一 Key 存在即可）。"""
    return has_zhipu() or has_deepseek()


# --------------------------------------------------------------------------
# 业务参数（集中一处，方便评审时讲"可配置"）
# --------------------------------------------------------------------------
class Biz:
    """电商运营里的硬性阈值。改这里就能改系统行为，不用翻代码。"""

    # 汇率：广告报表按美元口径统计
    CURRENCY = "USD"

    # ---- 广告诊断阈值 ----
    TARGET_ACOS = 0.28           # 目标 ACOS（广告花费 / 广告销售额）
    ACOS_WARN = 0.35             # 超过即预警
    ACOS_CRITICAL = 0.50         # 超过即建议暂停/降价
    TARGET_ROAS = round(1 / 0.28, 2)
    MIN_CLICKS_FOR_JUDGE = 20    # 一周内点击太少就不下转化结论，避免"1 次点击 1 单"的噪声
    HIGH_CTR = 0.004             # 点击率基准（Amazon SP 常见区间 0.2%~0.6%）
    LOW_CTR = 0.0015
    HIGH_CVR = 0.10              # 转化率基准
    LOW_CVR = 0.04
    BUDGET_CAP_RATIO = 0.95      # 花费/预算 超过该比例视为"预算打满"，可能被截流
    ZERO_ORDER_SPEND = 8.0       # 单活动花费超过该值且零订单 → 重点优化对象
    CPC_FLOOR_ZONE = 0.30        # CPC 已接近平台底价（约 $0.15~0.30）的区间。
    #   落在这个区间还超标，说明"客单价撑不住广告"，而不是"竞价买贵了" ——
    #   两者的处置完全相反：前者要改商品结构/价格，后者只需降竞价。

    # ---- 库存阈值 ----
    DEFAULT_LEAD_TIME_DAYS = 25  # 补货在途天数
    STOCKOUT_WARN_DAYS = 21      # 可售天数低于此值预警
    OVERSTOCK_DAYS = 120         # 可售天数高于此值视为压货

    # ---- 文案生成 ----
    HERO_SIZE = 1024             # 主图边长（Amazon 建议 ≥1000px 才支持放大镜）
    HERO_VARIANTS = 3            # 每个 SKU 默认生成几个主图文案变体
