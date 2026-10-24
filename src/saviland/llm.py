# -*- coding: utf-8 -*-
"""统一 LLM 客户端：多引擎 + 自动降级 + 全程可观测。

为什么自己写这一层，而不是直接用 langchain
------------------------------------------
底座的 `rag_demo.py` 已经有一套引擎路由，但它绑定在"检索问答"这个场景上。
电商的两条链路需要的是一份**通用的、带降级链和用量统计的调用器**：
    - 链路 1 要批量生成几十条文案（要快、要便宜 → 智谱 glm-4-flash）
    - 链路 2 要对一份周报做归因总结（要更强的推理 → DeepSeek）
    - 两条链路都**必须保证有产物**（演示环境网络不可控 → 离线确定性兜底）

降级链：请求的引擎 → 另一个在线引擎 → 离线模板引擎。
任何一环失败都会记进 `generation_runs.note`，报告里能看出来"这一版是降级产物"，
**不假装成功**。这是这个模块最重要的设计约束。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config

# 引擎名 -> (base_url, api_key, model, 中文说明)
ENGINES: dict[str, tuple[str, str, str, str]] = {
    "zhipu": (config.ZHIPU_BASE_URL, config.ZHIPU_API_KEY, config.ZHIPU_CHAT_MODEL,
              "智谱 GLM-4-Flash（便宜档，适合批量文案）"),
    "deepseek": (config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY, config.DEEPSEEK_CHAT_MODEL,
                 "DeepSeek（更强推理，适合周报归因）"),
}
OFFLINE = "offline"


@dataclass
class LLMResult:
    """一次模型调用的结果。ok=False 时 text 为空，由调用方走离线兜底。"""
    text: str = ""
    engine: str = OFFLINE
    ok: bool = False
    latency_ms: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    note: str = ""
    attempts: list[str] = field(default_factory=list)

    @property
    def engine_label(self) -> str:
        if self.engine == OFFLINE:
            return "offline:确定性模板"
        return f"{self.engine}:{ENGINES.get(self.engine, ('', '', '?', ''))[2]}"


def available_engines() -> list[str]:
    return [name for name, (_, key, _, _) in ENGINES.items() if key]


def _post_json(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_chat_response(body: dict) -> tuple[str, int, int]:
    choices = body.get("choices") or []
    if not choices:
        raise ValueError(f"响应里没有 choices：{str(body)[:200]}")
    message = choices[0].get("message") or {}
    # 智谱的思考型模型会把思维链放在 reasoning_content，正文仍是 content
    text = (message.get("content") or "").strip()
    usage = body.get("usage") or {}
    return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def chat(
    prompt: str,
    system: str | None = None,
    engine: str | None = None,
    temperature: float = 0.4,
    max_tokens: int = 2048,
    timeout: float = 90.0,
    json_mode: bool = False,
    fallback: bool = True,
    on_event: Callable[[str], None] | None = None,
) -> LLMResult:
    """调用大模型。失败时按降级链重试，全失败则返回 ok=False（由调用方兜底）。"""
    result = LLMResult(prompt_chars=len(prompt))
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    chain: list[str] = []
    preferred = (engine or config.DEFAULT_ENGINE).strip()
    if preferred == OFFLINE:
        # 显式要求离线：直接返回失败结果，由调用方走确定性生成器。
        # 用途：演示时不烧额度、CI 里跑确定性快照测试。
        result.latency_ms = 0
        result.engine = OFFLINE
        result.note = "已按要求使用离线确定性引擎（不调用在线模型）"
        result.attempts.append("offline:按要求跳过在线引擎")
        return result
    if preferred in ENGINES:
        chain.append(preferred)
    for name in available_engines():
        if name not in chain:
            chain.append(name)
    if not fallback:
        chain = chain[:1]

    t0 = time.time()
    for name in chain:
        base_url, api_key, model, _desc = ENGINES[name]
        if not api_key:
            result.attempts.append(f"{name}:无 Key，跳过")
            continue
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            # 两个引擎都支持 OpenAI 兼容的 response_format，失败时靠 prompt 约束兜底
            payload["response_format"] = {"type": "json_object"}
        try:
            if on_event:
                on_event(f"调用 {name}:{model} …")
            body = _post_json(f"{base_url}/chat/completions", payload, api_key, timeout)
            text, tin, tout = _parse_chat_response(body)
            if not text:
                raise ValueError("模型返回了空内容")
            result.text = text
            result.engine = name
            result.ok = True
            result.tokens_in = tin
            result.tokens_out = tout
            result.completion_chars = len(text)
            result.latency_ms = int((time.time() - t0) * 1000)
            result.attempts.append(f"{name}:成功 {result.latency_ms}ms")
            return result
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:  # noqa: BLE001
                pass
            result.attempts.append(f"{name}:HTTP {e.code} {detail}")
        except Exception as e:  # noqa: BLE001 - 网络层任何异常都要降级，不能崩
            result.attempts.append(f"{name}:{type(e).__name__} {str(e)[:120]}")

    result.latency_ms = int((time.time() - t0) * 1000)
    result.engine = OFFLINE
    result.note = "在线引擎全部不可用，已降级为离线确定性生成；" + " | ".join(result.attempts)
    return result


# --------------------------------------------------------------------------
# JSON 解析助手：大模型输出 JSON 的三种常见脏数据都要能接住
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any | None:
    """从模型输出里尽量抠出 JSON。

    依次尝试：整段直接 parse → 去代码围栏 → 取第一个 { 到最后一个 } 的切片。
    全部失败返回 None（调用方走兜底），**不抛异常**。
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def smoke_test() -> dict:
    """自检用：对每个可用引擎发一次最小请求。"""
    out: dict[str, Any] = {}
    for name in available_engines() or [OFFLINE]:
        if name == OFFLINE:
            out[OFFLINE] = {"ok": True, "note": "离线引擎无需联网"}
            continue
        res = chat("只回两个字：收到", engine=name, temperature=0.0, max_tokens=16, fallback=False)
        out[name] = {"ok": res.ok, "text": res.text[:20], "latency_ms": res.latency_ms,
                     "note": res.note or (res.attempts[-1] if res.attempts else "")}
    return out
