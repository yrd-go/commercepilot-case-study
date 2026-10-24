# -*- coding: utf-8 -*-
"""Starlette 应用：把 URL 映射到 `api.py` 的纯函数 + 提供静态前端与媒体文件。

安全约束（有意做窄）
--------------------
`/media/*` 与 `/files/*` 只允许访问白名单目录里的文件，并且拒绝任何含 `..` 的路径。
它们对外暴露的是"产物"，不是整个磁盘。这一点在本地演示里看起来多余，
但只要这个服务被放到局域网，它就是唯一的边界。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import config, rag_bridge
from . import api, auth, jobs
from .storefront import shop_routes

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 媒体白名单：URL 前缀 -> 磁盘根目录
MEDIA_ROOTS: dict[str, Path] = {
    "product": config.ASSETS_DIR,                                    # 商品图
    "hero": config.OUTPUTS_DIR / "hero_images",                       # 生成的主图
    "chart": config.OUTPUTS_DIR / "weekly_charts",                    # 周报图表
    "listing": config.OUTPUTS_DIR / "listing",                        # 文案 JSON/CSV
    "asset": config.DOCS_DIR / "images",                              # 文档配图
}


class UTF8JSONResponse(JSONResponse):
    """不转义中文的 JSON 响应（默认的 json.dumps 会把中文写成 \\uXXXX，包体大且难查）。"""

    media_type = "application/json; charset=utf-8"

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"), default=str).encode("utf-8")


def _json(data: Any, status: int = 200) -> Response:
    return UTF8JSONResponse(data, status_code=status)


def _safe_path(bucket: str, rel: str) -> Path | None:
    root = MEDIA_ROOTS.get(bucket)
    if root is None:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        return None
    target = (Path(root) / rel).resolve()
    try:
        target.relative_to(Path(root).resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


# --------------------------------------------------------------------------
# 静态与首页
# --------------------------------------------------------------------------
async def index(request: Request) -> Response:
    index_html = STATIC_DIR / "index.html"
    if not index_html.is_file():
        return HTMLResponse("<h1>前端资源缺失</h1><p>找不到 static/index.html</p>", status_code=500)
    return HTMLResponse(index_html.read_text(encoding="utf-8"))


async def media(request: Request) -> Response:
    bucket = request.path_params["bucket"]
    rel = request.path_params["path"]
    target = _safe_path(bucket, rel)
    if target is None:
        return _json({"error": "not found", "bucket": bucket, "path": rel}, 404)
    return FileResponse(target)


async def report_file(request: Request) -> Response:
    label = request.path_params["label"].replace("/", "").replace("..", "")
    kind = request.path_params["kind"]
    ext = {"html": "html", "md": "md", "xlsx": "xlsx"}.get(kind)
    if not ext:
        return _json({"error": "unsupported kind"}, 400)
    path = config.OUTPUTS_DIR / "weekly_reports" / f"weekly_{label}.{ext}"
    if not path.is_file():
        return _json({"error": "not found", "label": label}, 404)
    names = {"html": "text/html; charset=utf-8",
             "md": "text/markdown; charset=utf-8",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    return FileResponse(path, media_type=names[ext], filename=path.name)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
async def api_meta(request: Request) -> Response:
    return _json(api.meta())


async def api_overview(request: Request) -> Response:
    return _json(api.overview())


async def api_products(request: Request) -> Response:
    return _json(api.products())


async def api_product(request: Request) -> Response:
    data = api.product_detail(request.path_params["sku"])
    return _json(data) if data else _json({"error": "sku not found"}, 404)


async def api_copies(request: Request) -> Response:
    return _json(api.copies())


async def api_inventory(request: Request) -> Response:
    return _json(api.inventory())


async def api_orders(request: Request) -> Response:
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    return _json(api.orders(max(7, min(120, days))))


async def api_ads(request: Request) -> Response:
    return _json(api.ads(request.query_params.get("week") or None))


async def api_findings(request: Request) -> Response:
    return _json(api.findings(request.query_params.get("week") or None))


async def api_report(request: Request) -> Response:
    return _json(api.report(request.query_params.get("week") or None))


async def api_runs(request: Request) -> Response:
    return _json(api.runs_view())


async def api_artifacts(request: Request) -> Response:
    return _json(api.artifacts(request.path_params["run_id"]))


async def api_rag_status(request: Request) -> Response:
    return _json({**api.rag_status(), **api.rag_queries_hint()})


async def api_rag_search(request: Request) -> Response:
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return _json({"error": "缺少检索词 q"}, 400)
    try:
        k = int(request.query_params.get("k", "5"))
    except ValueError:
        k = 5
    rewrite = request.query_params.get("rewrite") == "1"
    return _json(api.rag_search(q, k=max(1, min(10, k)), rewrite=rewrite))


async def api_rag_rebuild(request: Request) -> Response:
    info = rag_bridge.reset_index()
    return _json({"chunks": info["chunks"], "ready": info["ready"], "error": info["error"]})


ALLOWED_ENGINES = ("", "zhipu", "deepseek", "offline")


def _engine_of(body: dict) -> str | None:
    """请求里可以指定引擎；`offline` 表示不调用任何在线模型（演示/测试用）。"""
    engine = str(body.get("engine") or "").strip()
    return engine if engine in ALLOWED_ENGINES else None


async def api_generate_listing(request: Request) -> Response:
    body = await request.json() if request.headers.get("content-length") else {}
    skus = body.get("skus") or []
    variants = int(body.get("variants") or 3)
    engine = _engine_of(body)
    if not skus:
        return _json({"error": "请至少选择一个 SKU"}, 400)
    job_id = jobs.start("listing", f"生成 {len(skus)} 个 SKU 的主图与卖点文案",
                        lambda job: api.run_listing_batch(job, skus, variants, engine),
                        total=len(skus))
    return _json({"job_id": job_id})


async def api_generate_weekly(request: Request) -> Response:
    body = await request.json() if request.headers.get("content-length") else {}
    week = body.get("week") or api.meta()["report_week"]
    engine = _engine_of(body)
    job_id = jobs.start("weekly", f"生成 {week} 周广告周报",
                        lambda job: api.run_weekly(job, week, engine), total=3)
    return _json({"job_id": job_id})


async def api_job(request: Request) -> Response:
    job = jobs.get(request.path_params["job_id"])
    if job is None:
        return _json({"error": "job not found"}, 404)
    return _json(job.to_dict())


async def api_jobs(request: Request) -> Response:
    return _json({"items": jobs.recent(10)})


async def api_health(request: Request) -> Response:
    return _json({"ok": True, "products": api.meta()["counts"]["products"]})


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
def create_app() -> Starlette:
    routes = [
        Route("/login", auth.login_page),
        Route("/login", auth.login_submit, methods=["POST"]),
        Route("/logout", auth.logout),
        Route("/api/whoami", auth.whoami),
        Route("/", index),
        Route("/api/health", api_health),
        Route("/api/meta", api_meta),
        Route("/api/overview", api_overview),
        Route("/api/products", api_products),
        Route("/api/products/{sku}", api_product),
        Route("/api/copies", api_copies),
        Route("/api/inventory", api_inventory),
        Route("/api/orders", api_orders),
        Route("/api/ads", api_ads),
        Route("/api/findings", api_findings),
        Route("/api/report", api_report),
        Route("/api/runs", api_runs),
        Route("/api/runs/{run_id}/artifacts", api_artifacts),
        Route("/api/rag/status", api_rag_status),
        Route("/api/rag/search", api_rag_search),
        Route("/api/rag/rebuild", api_rag_rebuild, methods=["POST"]),
        Route("/api/generate/listing", api_generate_listing, methods=["POST"]),
        Route("/api/generate/weekly", api_generate_weekly, methods=["POST"]),
        Route("/api/jobs", api_jobs),
        Route("/api/jobs/{job_id}", api_job),
        Route("/media/{bucket}/{path:path}", media),
        Route("/files/report/{label}/{kind}", report_file),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
        # ── 买家端（前台商城）────────────────────────────────────────────
        # 与后台完全分开：独立模块（storefront.py）、独立样式（shop.css）、
        # 服务端渲染的多页（不是后台那个 SPA），而且**不 import 任何后台域模块**。
        *shop_routes(),
    ]
    # 访问边界：后台与它的 API 需要登录；买家端 / 静态资源 / 探活接口公开。
    # 关掉它的唯一方式是 SAVILAND_NO_AUTH=1（自动化与测试用）。
    return Starlette(routes=routes,
                     middleware=[Middleware(auth.RequireLogin)])


app = create_app()


class _Tee:
    """把 stdout 同时写到控制台和日志文件。

    为什么需要：双击启动器时用户看不到控制台历史（窗口一关就没了），
    而"启动失败"往往只有一行原因。把输出同时落到日志文件，出问题时能直接看文件。
    用 Python 做而不是 cmd 的 `| tee`：管道会吃掉退出码，而且 Windows 上
    多一个依赖。

    ★ 必须实现完整的文件对象协议。第一版只写了 `write` / `flush`，
    结果 uvicorn 初始化日志时调用 `sys.stdout.isatty()` 直接抛
    `AttributeError: '_Tee' object has no attribute 'isatty'` ——
    服务瞬间退出（退出码 1），而启动器看起来只是"闪了一下"。
    凡是替换 `sys.stdout`，就得把 `isatty` / `fileno` / `encoding` 这些补齐或委托。
    """

    def __init__(self, stream, path) -> None:
        self.stream = stream
        self.path = path

    def write(self, text: str) -> int:
        try:
            self.stream.write(text)
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        return len(text)

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass

    # ---- 文件对象的基本协议（缺一个就可能让第三方库崩）----
    def isatty(self) -> bool:
        return False          # 同时在写文件，不该当作终端（避免带颜色控制符）

    def fileno(self) -> int:
        return self.stream.fileno()

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def __getattr__(self, name: str):
        # encoding / errors / closed 等一律委托给被包装的流
        return getattr(self.stream, name)


def _port_free(host: str, port: int) -> bool:
    """严格判断端口是否**真的**可用（能连上或能独占绑定 = 不可用）。

    ★ Windows 上必须小心：`SO_REUSEADDR` 允许绑定一个**已经被监督的端口**，
      于是"探测说空闲 → uvicorn 也绑定成功 → 但流量被旧进程抢走"，
      表现就是*端口在监听却一直不响应*。我第一次就是用它做探测，踩了这个坑。
      所以这里两手都做：
        1) 先 `connect()` —— 能连上就说明有人在听（明确占用）；
        2) 再尝试**独占**绑定（Windows 用 SO_EXCLUSIVEADDRUSE）—— 绑不上就是占用。
      Unix 上 `SO_REUSEADDR` 的语义不同，用它绕开 TIME_WAIT 是对的。
    """
    import os
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.3):
            return False                      # 有人在监听
    except OSError:
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _running_instance(host: str, port: int, tries: int = 12) -> str | None:
    """探测是否已经有一个本项目的实例在跑；有就返回它的 URL。

    为什么需要：双击两次启动器（或者上次的窗口没关干净）会出现**两个服务**。
    第二个会占到 8502，于是"界面打开了但数据不对"；更糟的是两个进程同时
    打开同一个 Chroma / SQLite —— 加载时间从 4 秒变成 19 秒（实测）。
    所以启动前先问一句"有没有人在跑"，有就直接把浏览器指过去。
    """
    import json
    import urllib.request

    for offset in range(tries):
        candidate = port + offset
        try:
            with urllib.request.urlopen(
                    f"http://{host}:{candidate}/api/health", timeout=0.4) as resp:
                if resp.status != 200:
                    continue
                data = json.loads(resp.read().decode("utf-8"))
                # 认准是本项目：/api/health 返回 {"ok": true, "products": 20}
                if data.get("ok") and "products" in data:
                    return f"http://{host}:{candidate}"
        except Exception:  # noqa: BLE001 - 连不上就是没有，正常情况
            continue
    return None


def _pick_port(host: str, port: int, tries: int = 12) -> int:
    """端口被占时自动往后找一个空位，并告诉用户实际用的是哪个。"""
    for offset in range(tries):
        candidate = port + offset
        if _port_free(host, candidate):
            return candidate
    return port


def _warmup() -> None:
    """后台预热：打开向量索引 + 检查演示数据是否过期。

    为什么需要：冷启动时第一次请求要打开 Chroma（1~3 秒），而前端是**拿到
    `/api/meta` 才开始渲染**的 —— 用户看到的是好几秒的白屏，很像"没起来"。
    在 uvicorn 监听的同时用后台线程先做掉这两件事，浏览器打开时已经是热的。
    """
    import time

    try:
        from .. import rag_bridge, seed

        t0 = time.time()
        info = rag_bridge.ensure_loaded()
        print(f"[预热] 向量索引就绪：{info.get('chunks')} 个片段"
              f"（{time.time() - t0:.1f}s）")
        fresh = seed.ensure_fresh_once()      # 与首个 API 请求共用同一把锁
        if fresh.get("action") == "reseeded":
            print(f"[预热] 演示数据保鲜：{fresh.get('reason')}")
    except Exception as e:  # noqa: BLE001 - 预热失败不该影响服务启动
        print(f"[预热] 跳过（{type(e).__name__}: {e}）")


def serve(host: str = "127.0.0.1", port: int = 8501, open_browser: bool = True,
          log_path: str | None = None) -> int:
    """启动服务。返回进程退出码。"""
    import os
    import sys
    import threading

    import uvicorn

    # 环境变量兜底：自动化/提权环境下不希望拉起浏览器（曾因此产生一个
    # 以提升权限运行的浏览器实例，导致用户正常打开浏览器时被弹"请用普通权限重启"）。
    if os.getenv("SAVILAND_NO_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
        open_browser = False

    if log_path:
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("")
            sys.stdout = _Tee(sys.stdout, log_path)   # type: ignore[assignment]
            sys.stderr = _Tee(sys.stderr, log_path)   # type: ignore[assignment]
        except OSError:
            pass

    actual = _pick_port(host, port)

    # 登录信息要**在窗口里说清楚**：默认口令只写在文档里，等于没写
    if auth.auth_disabled():
        print("[认证] 已关闭（SAVILAND_NO_AUTH=1）—— 仅用于自动化/测试")
    else:
        user, pwd, is_demo = auth.credentials()
        print(f"[认证] 后台需要登录：{user} / {pwd}")
        if is_demo:
            print("       这是**演示默认口令**；生产请用 SAVILAND_ADMIN_PASSWORD 覆盖，")
            print("       并叠加 HTTPS / MFA / RBAC —— 本项目只做了会话隔离。")
    print(f"[前台] 买家端商城（无需登录）：http://{host}:{actual}/shop")

    # 已经有实例在跑 → 直接复用，不再起第二个（省得两个进程抢同一个数据库）
    existing = _running_instance(host, port)
    if existing:
        print(f"[已在运行] {existing}")
        print("       直接打开这个地址即可；要重启，请先关掉之前那个服务窗口。")
        if open_browser:
            import webbrowser
            try:
                webbrowser.open(existing)
            except Exception:  # noqa: BLE001
                pass
        return 0

    if actual != port:
        print(f"[提示] 端口 {port} 已被占用，改用 {actual}")
    url = f"http://{host}:{actual}"
    print(f"[就绪] 请在浏览器打开：{url}")
    print("       保持这个窗口开着；按 Ctrl+C 停止服务。")

    # 后台预热（不阻塞监听）
    threading.Thread(target=_warmup, name="warmup", daemon=True).start()

    if open_browser:
        import webbrowser

        def _open() -> None:
            try:
                webbrowser.open(url)
            except Exception as e:  # noqa: BLE001 - 开浏览器失败绝不能影响服务
                print(f"[提示] 自动打开浏览器失败（{type(e).__name__}），请手动访问 {url}")

        threading.Timer(1.5, _open).start()

    # log_level=info：把 "Uvicorn running on ..." 和绑定失败的原因都打出来。
    # 用 warning 的时候这些信息全被吞掉 —— 用户只能看到"闪退"，无从判断。
    reason = "正常退出"
    try:
        uvicorn.run(app, host=host, port=actual, log_level="info")
    except KeyboardInterrupt:
        reason = "收到 Ctrl+C 中断"
    except SystemExit as e:
        reason = f"uvicorn 退出（code={e.code}）"
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {e}"
    print(f"[已停止] 服务已关闭（{reason}）。")
    if "Ctrl" not in reason and reason != "正常退出":
        print(f"[提示] 如果这不是你主动停的，把 {url} 贴到浏览器里试试；"
              f"仍不行请看启动日志。")
    return 0
