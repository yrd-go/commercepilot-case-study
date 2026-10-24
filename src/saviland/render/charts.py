# -*- coding: utf-8 -*-
"""报表图表：为广告周报生成 PNG 图表。

踩过的坑（写在代码里，评审被问"遇到过什么问题"有具体答案）
----------------------------------------------------------
matplotlib 默认字体不含中文字形，中文标签会渲染成一排方框（"豆腐块"），
而 `plt.rcParams` 必须在**创建 figure 之前**设置，且 Windows 上字体名要用
中文名（"Microsoft YaHei"）而不是文件名。这里统一在 `_setup()` 里做，
并且显式 `axes.unicode_minus=False`，否则负号也会变成方框。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")           # 无 GUI 后端：服务器/CI 上也能出图
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .. import config  # noqa: E402

C_PRIMARY = "#2F4858"
C_ACCENT = "#C9A227"
C_GOOD = "#2E8B6F"
C_WARN = "#E0A030"
C_BAD = "#C0453E"
C_MUTED = "#9AA0AE"
C_BG = "#FFFFFF"


def _setup() -> None:
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "Microsoft JhengHei", "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.facecolor"] = C_BG
    plt.rcParams["axes.facecolor"] = C_BG
    plt.rcParams["axes.edgecolor"] = "#D8DBE3"
    plt.rcParams["axes.grid"] = True
    plt.rcParams["grid.color"] = "#EDEFF4"
    plt.rcParams["grid.linewidth"] = 0.8
    plt.rcParams["axes.axisbelow"] = True
    plt.rcParams["figure.dpi"] = 130


def _out(name: str) -> Path:
    d = config.OUTPUTS_DIR / "weekly_charts"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def _pct(v: float, _pos=None) -> str:
    return f"{v * 100:.0f}%"


def _money(v: float, _pos=None) -> str:
    # ★ 必须转义 $：matplotlib 会把成对的 $ 当成数学模式定界符（mathtext），
    #   一旦标题里写了 "$1,141 / 销售 $4,202"，中间那段就被当公式渲染，
    #   中文会退化成方框并刷 "Font 'rm' does not have a glyph" 警告。
    #   这个坑很隐蔽：图的数字看着"没错"，但中文全变豆腐块。
    return f"\\${v:,.0f}"


def _m(v: float) -> str:
    """给标题/标注用的金额字符串（同样转义 $）。"""
    return f"\\${v:,.0f}"


def _save(fig, path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return path


# --------------------------------------------------------------------------
def chart_daily_trend(metrics: dict, prefix: str = "") -> Path:
    """日趋势：柱=花费，线=销售额（双轴）。看"钱花出去了、销售有没有跟上"。"""
    _setup()
    daily = metrics.get("daily") or []
    if not daily:
        raise ValueError("没有日维度数据")
    dates = [d["date"][5:] for d in daily]
    spend = [d["spend"] for d in daily]
    sales = [d["sales"] for d in daily]

    fig, ax1 = plt.subplots(figsize=(9.2, 3.9))
    ax1.bar(dates, spend, color=C_PRIMARY, alpha=0.85, label="广告花费", width=0.6)
    ax1.set_ylabel("广告花费（美元）", color=C_PRIMARY)
    ax1.yaxis.set_major_formatter(FuncFormatter(_money))

    ax2 = ax1.twinx()
    ax2.plot(dates, sales, color=C_ACCENT, marker="o", linewidth=2.4, label="广告销售额")
    ax2.set_ylabel("广告销售额（美元）", color="#8A6E12")
    ax2.yaxis.set_major_formatter(FuncFormatter(_money))
    ax2.grid(False)

    total = metrics["total"]
    ax1.set_title(
        f"报告周 {metrics['week_label']}（{metrics['week_start']} ~ {metrics['week_end']}）"
        f"  花费 {_m(total['spend'])} / 销售 {_m(total['sales'])}"
        f" / ACOS {'—' if total['acos'] is None else format(total['acos'], '.1%')}",
        fontsize=12, pad=12,
    )
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=9)
    return _save(fig, _out(f"{prefix}daily_trend.png"))


def chart_acos_by_campaign(metrics: dict, prefix: str = "") -> Path:
    """各活动 ACOS 横向条形，带目标线与红线 —— 一眼看出谁在烧钱。"""
    _setup()
    camps = [c for c in (metrics.get("by_campaign") or []) if c.get("spend", 0) > 0]
    if not camps:
        raise ValueError("没有活动数据")
    camps = sorted(camps, key=lambda c: (c["acos"] if c["acos"] is not None else 9.99))
    labels = [c["name"].replace("Saviland ", "")[:22] for c in camps]
    # 无销售（acos=None）的活动画成"超出坐标轴"的红色条，明确标注零转化
    vals = [(c["acos"] if c["acos"] is not None else max(0.75, 1.0)) for c in camps]
    colors = []
    for c in camps:
        if c["acos"] is None:
            colors.append(C_BAD)
        elif c["acos"] > config.Biz.ACOS_CRITICAL:
            colors.append(C_BAD)
        elif c["acos"] > config.Biz.ACOS_WARN:
            colors.append(C_WARN)
        else:
            colors.append(C_GOOD)

    fig, ax = plt.subplots(figsize=(9.2, max(3.4, 0.42 * len(camps) + 1.6)))
    ax.barh(labels, vals, color=colors, height=0.62)
    for i, c in enumerate(camps):
        txt = "无转化" if c["acos"] is None else f"{c['acos']:.1%}"
        ax.text(vals[i] + 0.012, i, f"{txt}  ({_m(c['spend'])})",
                va="center", fontsize=8.6, color="#4A4F5C")
    ax.axvline(config.Biz.TARGET_ACOS, color=C_GOOD, linestyle="--", linewidth=1.6)
    ax.axvline(config.Biz.ACOS_CRITICAL, color=C_BAD, linestyle="--", linewidth=1.6)
    ax.text(config.Biz.TARGET_ACOS, len(camps) - 0.35, f" 目标 {config.Biz.TARGET_ACOS:.0%}",
            color=C_GOOD, fontsize=8.6, va="bottom")
    ax.text(config.Biz.ACOS_CRITICAL, len(camps) - 0.35, f" 红线 {config.Biz.ACOS_CRITICAL:.0%}",
            color=C_BAD, fontsize=8.6, va="bottom")
    ax.xaxis.set_major_formatter(FuncFormatter(_pct))
    ax.set_xlabel("ACOS（广告花费 / 广告销售额）")
    ax.set_title("各广告活动 ACOS 排名：谁在有效花钱，谁在烧钱", fontsize=12, pad=10)
    ax.grid(axis="y", visible=False)
    return _save(fig, _out(f"{prefix}acos_by_campaign.png"))


def chart_wow_compare(metrics: dict, prefix: str = "") -> Path:
    """本周 vs 上周关键指标对比（归一化到上周=100 的指数，量纲不同也能同图比）。"""
    _setup()
    cur, prev = metrics["total"], metrics["prev_total"]
    fields = [("花费", "spend"), ("销售额", "sales"), ("订单", "orders"),
              ("点击", "clicks"), ("曝光", "impressions")]
    labels, cur_idx, prev_idx, raw = [], [], [], []
    for label, key in fields:
        c, p = float(cur.get(key) or 0), float(prev.get(key) or 0)
        if p <= 0:
            continue
        labels.append(label)
        cur_idx.append(c / p * 100)
        prev_idx.append(100.0)
        raw.append((c, p))
    if not labels:
        raise ValueError("缺少环比基准数据")

    fig, ax = plt.subplots(figsize=(9.2, 3.6))
    x = range(len(labels))
    ax.bar([i - 0.19 for i in x], prev_idx, width=0.36, color=C_MUTED, label="上周（基准=100）")
    bars = ax.bar([i + 0.19 for i in x], cur_idx, width=0.36, color=C_PRIMARY, label="本周")
    for i, (bar, (c, p)) in enumerate(zip(bars, raw)):
        delta = (c / p - 1) if p else 0
        color = C_GOOD if delta >= 0 else C_BAD
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.6,
                f"{delta:+.0%}", ha="center", fontsize=9, color=color)
    ax.axhline(100, color="#B8BCC7", linewidth=1.0, linestyle=":")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("相对上周指数")
    ax.set_title("本周 vs 上周：哪些指标在涨、哪些在跌", fontsize=12, pad=10)
    ax.legend(frameon=False, fontsize=9)
    ax.grid(axis="x", visible=False)
    return _save(fig, _out(f"{prefix}wow_compare.png"))


def chart_spend_structure(metrics: dict, prefix: str = "") -> Path:
    """花费结构环形图：预算集中在少数活动上，是风险也是机会。"""
    _setup()
    camps = sorted((metrics.get("by_campaign") or []), key=lambda c: -c.get("spend", 0))
    camps = [c for c in camps if c.get("spend", 0) > 0]
    if not camps:
        raise ValueError("没有活动数据")
    top = camps[:6]
    rest = sum(c["spend"] for c in camps[6:])
    labels = [c["name"].replace("Saviland ", "")[:18] for c in top]
    values = [c["spend"] for c in top]
    if rest > 0:
        labels.append("其他活动")
        values.append(rest)

    palette = [C_PRIMARY, "#4E6E81", "#7A94A6", C_ACCENT, "#D9BE6A", C_GOOD, C_MUTED]
    fig, ax = plt.subplots(figsize=(6.4, 4.6))
    wedges, _texts, autotexts = ax.pie(
        values, labels=None, autopct=lambda p: f"{p:.0f}%" if p >= 4 else "",
        colors=palette[:len(values)], startangle=90,
        wedgeprops=dict(width=0.42, edgecolor="white", linewidth=2),
        textprops=dict(fontsize=9, color="white"),
    )
    ax.legend(wedges, [f"{l}  {_m(v)}" for l, v in zip(labels, values)],
              loc="center left", bbox_to_anchor=(0.98, 0.5), frameon=False, fontsize=8.6)
    total = sum(values)
    ax.text(0, 0.08, _m(total), ha="center", fontsize=17, color=C_PRIMARY, weight="bold")
    ax.text(0, -0.16, "本周广告花费", ha="center", fontsize=9.6, color="#6B7180")
    ax.set_title("广告花费结构", fontsize=12, pad=6)
    return _save(fig, _out(f"{prefix}spend_structure.png"))


def chart_ctr_cvr_scatter(metrics: dict, prefix: str = "") -> Path:
    """CTR × CVR 四象限诊断图：把"该改主图"和"该改详情页"分开。

    这张图是两条 AI 链路的**分诊台**：
        低 CTR → 主图没人点 → 链路 1 改主图
        低 CVR → 点进来不买 → 链路 1 改五点描述/详情页
        双高    → 该放量
        双低    → 该砍
    """
    _setup()
    camps = [c for c in (metrics.get("by_campaign") or []) if c.get("impressions", 0) > 0]
    if not camps:
        raise ValueError("没有活动数据")
    x = [c["ctr"] for c in camps]
    y = [c["cvr"] for c in camps]
    sizes = [max(60, min(1400, c["spend"] * 2.6)) for c in camps]

    fig, ax = plt.subplots(figsize=(8.6, 5.0))
    ax.scatter(x, y, s=sizes, c=C_PRIMARY, alpha=0.68, edgecolors="white", linewidths=1.4)
    for c in camps:
        ax.annotate(c["name"].replace("Saviland ", "")[:16], (c["ctr"], c["cvr"]),
                    fontsize=7.8, xytext=(5, 5), textcoords="offset points", color="#4A4F5C")

    bx, by = config.Biz.LOW_CTR, config.Biz.LOW_CVR
    ax.axvline(bx, color=C_MUTED, linestyle="--", linewidth=1.3)
    ax.axhline(by, color=C_MUTED, linestyle="--", linewidth=1.3)
    ax.set_xlabel("CTR 点击率（曝光 → 点击：主图/标题行不行）")
    ax.set_ylabel("CVR 转化率（点击 → 下单：详情页/价格行不行）")
    ax.xaxis.set_major_formatter(FuncFormatter(_pct))
    ax.yaxis.set_major_formatter(FuncFormatter(_pct))

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    pad_x, pad_y = (xmax - xmin) * 0.02, (ymax - ymin) * 0.04
    ax.text(xmin + pad_x, ymax - pad_y, "主图能点、详情页也接得住 → 可放量",
            fontsize=8.6, color=C_GOOD, va="top")
    ax.text(xmin + pad_x, ymin + pad_y, "没人点、点了也不买 → 优先砍",
            fontsize=8.6, color=C_BAD, va="bottom")
    ax.text(xmax - pad_x, ymax - pad_y, "点得动但不买 → 改五点描述/详情页",
            fontsize=8.6, color=C_WARN, va="top", ha="right")
    ax.text(xmax - pad_x, ymin + pad_y, "曝光多没人点 → 改主图",
            fontsize=8.6, color=C_WARN, va="bottom", ha="right")
    ax.set_title("CTR × CVR 四象限：每个活动该修哪里（气泡大小=花费）", fontsize=12, pad=10)
    return _save(fig, _out(f"{prefix}ctr_cvr_quadrant.png"))


def build_all(metrics: dict, prefix: str = "") -> dict[str, Path]:
    """生成全部图表；单张失败不影响其余（返回成功的那些）。"""
    builders = {
        "daily_trend": chart_daily_trend,
        "acos_by_campaign": chart_acos_by_campaign,
        "wow_compare": chart_wow_compare,
        "spend_structure": chart_spend_structure,
        "ctr_cvr_quadrant": chart_ctr_cvr_scatter,
    }
    out: dict[str, Path] = {}
    errors: dict[str, str] = {}
    for name, fn in builders.items():
        try:
            out[name] = fn(metrics, prefix)
        except Exception as e:  # noqa: BLE001 - 一张图失败不该让周报生成不了
            errors[name] = f"{type(e).__name__}: {e}"
    if errors:
        out["_errors"] = errors  # type: ignore[assignment]
    return out
