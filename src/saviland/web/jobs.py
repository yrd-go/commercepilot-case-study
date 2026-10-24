# -*- coding: utf-8 -*-
"""后台任务：把"跑一条链路"变成可轮询的 job。

为什么不能用一个同步 POST 了事
------------------------------
链路 1 生成一个 SKU 约 35 秒，20 个约 12 分钟；链路 2 约 100 秒。
如果做成同步请求，浏览器会一直转圈、看不出是"在跑"还是"卡死了"，
用户第一反应是刷新页面 —— 而刷新等于把正在跑的活丢掉。

所以做成 job：POST 立即返回 `job_id`，前端每 700ms 轮询一次拿
`{status, done, total, progress, log}`，进度条和日志都是真的。

实现上刻意保持"够用就好"：进程内字典 + 一个工作线程。
**不引 Celery / Redis** —— 这是一个单用户、单进程的本地应用，
加消息队列只会多两个故障点。真要多用户时，这个模块是唯一需要换掉的地方。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

_MAX_JOBS = 60          # 只保留最近 N 个，避免长时间运行后内存里堆积
_LOCK = threading.RLock()
_JOBS: dict[str, "Job"] = {}
_ORDER: list[str] = []


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "queued"          # queued / running / done / failed
    done: int = 0
    total: int = 0
    progress_text: str = ""
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self, include_result: bool = True) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at
        out = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "ratio": round(self.done / self.total, 4) if self.total else 0.0,
            "progress_text": self.progress_text,
            "log": self.log[-40:],
            "error": self.error,
            "elapsed_s": round(elapsed, 1),
            "result": self.result if include_result else None,
        }
        return out


def _prune() -> None:
    while len(_ORDER) > _MAX_JOBS:
        old = _ORDER.pop(0)
        _JOBS.pop(old, None)


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def recent(limit: int = 10) -> list[dict]:
    with _LOCK:
        ids = _ORDER[-limit:][::-1]
        return [_JOBS[i].to_dict(include_result=False) for i in ids if i in _JOBS]


def _log(job: Job, message: str) -> None:
    with _LOCK:
        job.log.append(f"{time.strftime('%H:%M:%S')}  {message}")
        job.progress_text = message


def start(kind: str, title: str, fn: Callable[[Job], Any], total: int = 1) -> str:
    """起一个后台任务。`fn(job)` 在子线程里执行，返回值会写进 job.result。"""
    job_id = f"{kind}-{uuid.uuid4().hex[:8]}"
    job = Job(id=job_id, kind=kind, title=title, total=max(1, total))
    with _LOCK:
        _JOBS[job_id] = job
        _ORDER.append(job_id)
        _prune()

    def runner() -> None:
        with _LOCK:
            job.status = "running"
        _log(job, f"开始：{title}")
        try:
            job.result = fn(job)
            with _LOCK:
                job.status = "done"
                job.done = job.total
                job.progress_text = "完成"
                job.finished_at = time.time()
            _log(job, "完成")
        except Exception as e:  # noqa: BLE001 - 任何异常都要变成 job 状态，不能崩线程
            detail = f"{type(e).__name__}: {e}"
            with _LOCK:
                job.status = "failed"
                job.error = detail
                job.progress_text = "失败"
                job.finished_at = time.time()
            _log(job, f"失败：{detail}")
            # 详细堆栈也留在日志里，方便在界面上直接看到
            for line in traceback.format_exc(limit=6).splitlines()[-8:]:
                _log(job, line)

    threading.Thread(target=runner, name=f"job-{job_id}", daemon=True).start()
    return job_id


def step(job: Job, index: int, total: int | None = None, message: str = "") -> None:
    """更新进度（在 fn 内部调用）。"""
    with _LOCK:
        job.done = index
        if total:
            job.total = max(1, total)
    if message:
        _log(job, message)
