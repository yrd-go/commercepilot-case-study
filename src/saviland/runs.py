# -*- coding: utf-8 -*-
"""AI 运行档案：每一次链路执行都留痕。

这个模块解决的是一个很实际的问题：**演示时被问"这版文案是模型生成的吗？"**
如果只有产物文件，你只能回答"是"。有了运行档案，可以当场打开后台的"运行记录"，
指出：用的哪个引擎、检索命中几条品牌知识、耗时多少、有没有降级、产物落在哪个路径。

所以每次链路执行都必须：
    run_id = start_run(...)  →  ...  →  finish_run(...) + add_artifact(...)
失败也要 finish_run(status='failed')，**不允许悄悄吞掉异常** ——
"失败也留痕"是这套东西能不能被信任的分界线。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import db


def start_run(pipeline: str, target: str = "", engine: str = "") -> str:
    sweep_stale()          # 先清理上一轮崩溃留下的僵尸记录
    run_id = f"{pipeline}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    db.execute(
        "INSERT INTO generation_runs (run_id, pipeline, target, engine, status) VALUES (?,?,?,?,?)",
        (run_id, pipeline, target, engine, "running"),
    )
    _LAST_RUN[pipeline] = run_id
    return run_id


# 每个链路"最近一次开跑"的编号。链路的执行是顺序的（批量也是 for 循环），
# 所以这个记录是可靠的。用途见 fail_current()。
_LAST_RUN: dict[str, str] = {}


def fail_current(pipeline: str, note: str) -> str | None:
    """把该链路最近一次开跑但没写回的运行标记为 failed。

    为什么需要它：`generate_batch` 会逐个 SKU 捕获异常（批量任务必须容错），
    但如果异常发生在 `start_run()` **之后**，那条记录就会永远停在 running ——
    批量任务"看起来正常完成"，档案里却躺着几条永不结束的记录。
    这个函数用来补上那一笔。
    """
    run_id = _LAST_RUN.get(pipeline)
    if not run_id:
        return None
    finish_run(run_id, status="failed", note=note[:400])
    return run_id


def finish_run(run_id: str, engine: str = "", rag_hits: int = 0, latency_ms: int = 0,
               prompt_chars: int = 0, status: str = "ok", note: str = "") -> None:
    db.execute(
        "UPDATE generation_runs SET engine=?, rag_hits=?, latency_ms=?, prompt_chars=?, "
        "status=?, note=? WHERE run_id=?",
        (engine, int(rag_hits), int(latency_ms), int(prompt_chars), status, note, run_id),
    )


def add_artifact(run_id: str, kind: str, path: Path | str, meta: dict | None = None) -> None:
    db.execute(
        "INSERT INTO artifacts (run_id, kind, path, meta_json) VALUES (?,?,?,?)",
        (run_id, kind, str(path), json.dumps(meta or {}, ensure_ascii=False)),
    )


def recent_runs(pipeline: str | None = None, limit: int = 30) -> list[dict]:
    if pipeline:
        return db.query(
            "SELECT * FROM generation_runs WHERE pipeline = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (pipeline, limit),
        )
    return db.query(
        "SELECT * FROM generation_runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
    )


def sweep_stale(minutes: int = 30) -> int:
    """把卡在 running 太久的运行标成 interrupted。

    为什么需要：如果进程在链路跑到一半时崩了（或被杀掉），`finish_run` 根本没机会执行，
    这条记录就**永远停在 running**。运行档案里躺着一条永不结束的执行记录，
    看档案的人会以为程序还在跑，或者以为系统坏了。
    每次开跑新任务前扫一遍，把明显过期的标记出来 —— 这是"失败也留痕"的补丁。
    """
    rows = db.query(
        "SELECT run_id FROM generation_runs "
        "WHERE status = 'running' AND created_at < datetime('now', ?)",
        (f"-{int(minutes)} minutes",),
    )
    for r in rows:
        finish_run(r["run_id"], status="interrupted",
                   note=f"进程在 {minutes} 分钟内未回写状态，判定为中断（可能崩溃或被杀）")
    return len(rows)


def artifacts_of(run_id: str) -> list[dict]:
    rows = db.query("SELECT * FROM artifacts WHERE run_id = ? ORDER BY id", (run_id,))
    for r in rows:
        try:
            r["meta"] = json.loads(r.pop("meta_json") or "{}")
        except json.JSONDecodeError:
            r["meta"] = {}
    return rows


def run_counts() -> dict:
    rows = db.query(
        "SELECT pipeline, status, COUNT(*) AS n FROM generation_runs GROUP BY pipeline, status"
    )
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["pipeline"], {})[r["status"]] = r["n"]
    return out


def engine_usage() -> list[dict]:
    """引擎使用分布 —— 报告附录里用来说明"哪几版是真模型生成、哪几版降级了"。"""
    return db.query(
        "SELECT engine, COUNT(*) AS n, "
        "       ROUND(AVG(latency_ms)) AS avg_latency_ms, "
        "       SUM(rag_hits) AS total_rag_hits "
        "FROM generation_runs WHERE engine IS NOT NULL AND engine != '' "
        "GROUP BY engine ORDER BY n DESC"
    )
