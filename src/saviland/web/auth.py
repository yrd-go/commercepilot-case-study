# -*- coding: utf-8 -*-
"""后台身份认证：登录、会话、访问边界。

为什么后台必须登录
------------------
"前台看不到后台入口"**不是防护**，只是没放链接。真实世界里后台的边界是三层：
**身份（登录 + 会话）+ 网络（VPN / 白名单）+ 权限（RBAC）**。
这个模块只做第一层里最小但真实的那部分：

    · 账密校验（`hmac.compare_digest` 常量时间比较，避免时序侧信道）
    · 服务端会话（内存表 + 随机 token），Cookie 是 HttpOnly + SameSite=Lax
    · 访问边界：买家端 / 静态资源 / 探活接口公开，其余一律要求登录
    · API 未登录返回 **401**（不是重定向，也不是假装成 200 空数据）

刻意没做的（不假装有）
----------------------
· RBAC 角色分级：现在是"登录 = 全权限"。真上线要按角色拆模块。
· 口令哈希存储 / 改密 / 锁定：默认口令来自环境变量，属于**演示级**。
· HTTPS / 反代 / 限流：这些应该在部署层做，代码里写一个假的没有意义。

所以对外说明要准确：**"有登录与会话隔离，但只是演示级认证"**，
不要说成"做到了生产级权限体系"。
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# 演示用默认口令。**故意写在代码里并大声提示**：
# 悄悄用一个弱口令假装安全，比明说"这是演示口令"更糟。
DEMO_USER = "admin"
DEMO_PASSWORD = "saviland-demo"

SESSION_COOKIE = "sv_session"
SESSION_TTL = 12 * 3600          # 12 小时，每次请求滑动续期
MAX_SESSIONS = 256

# 不需要登录的路径前缀。买家端公开，静态资源与探活公开 ——
# 否则浏览器拿不到 CSS、启动器的"是否已在运行"探测也会被拦。
PUBLIC_PREFIXES = (
    "/shop",
    "/static",
    "/media",
    "/login",
    "/favicon.ico",
    "/api/health",
)

_SESSIONS: dict[str, dict[str, Any]] = {}


def auth_disabled() -> bool:
    """`SAVILAND_NO_AUTH=1` 用于自动化与测试；默认**开启**认证。"""
    return os.getenv("SAVILAND_NO_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


def credentials() -> tuple[str, str, bool]:
    """返回 (用户名, 口令, 是否仍是演示默认值)。"""
    user = os.getenv("SAVILAND_ADMIN_USER", "").strip() or DEMO_USER
    pwd = os.getenv("SAVILAND_ADMIN_PASSWORD", "")
    if pwd:
        return user, pwd, False
    return user, DEMO_PASSWORD, True


def is_public(path: str) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") or path == p
               for p in PUBLIC_PREFIXES) or path in PUBLIC_PREFIXES


def _purge() -> None:
    now = time.time()
    for token, sess in list(_SESSIONS.items()):
        if sess["expires"] < now:
            _SESSIONS.pop(token, None)
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.pop(next(iter(_SESSIONS)), None)


def create_session(user: str) -> str:
    _purge()
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"user": user, "expires": time.time() + SESSION_TTL}
    return token


def drop_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def check_credentials(user: str, password: str) -> bool:
    real_user, real_pwd, _ = credentials()
    # 两个比较都要做，且都用常量时间 —— 不要因为用户名不对就提前返回
    user_ok = hmac.compare_digest(user.strip().lower(), real_user.lower())
    pwd_ok = hmac.compare_digest(password, real_pwd)
    return user_ok and pwd_ok


def current_user(request: Request) -> str | None:
    """读 Cookie → 校验会话；命中则滑动续期。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = _SESSIONS.get(token)
    if not sess:
        return None
    if sess["expires"] < time.time():
        _SESSIONS.pop(token, None)
        return None
    sess["expires"] = time.time() + SESSION_TTL      # 滑动续期
    return sess["user"]


class RequireLogin(BaseHTTPMiddleware):
    """访问边界：公开路径放行，其余必须已登录。

    API 返回 401（前端据此提示"会话过期"），页面 303 跳登录页并带上 `next`。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if auth_disabled():
            return await call_next(request)
        path = request.url.path
        if is_public(path):
            return await call_next(request)
        if current_user(request):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"error": "未登录或会话已过期", "login": "/login"},
                                status_code=401)
        nxt = quote(path + (("?" + request.url.query) if request.url.query else ""))
        return RedirectResponse(f"/login?next={nxt}", status_code=303)


# --------------------------------------------------------------------------
# 登录页（自包含样式：登录页不该依赖后台那套资源）
# --------------------------------------------------------------------------
def _login_html(*, error: str = "", next_path: str = "/", demo: bool = False) -> str:
    _, pwd, is_demo = credentials()
    hint = (f'<p class="hint">本地演示口令：<b>{DEMO_USER}</b> / <b>{pwd}</b>'
            f'（生产请用环境变量 <code>SAVILAND_ADMIN_PASSWORD</code> 覆盖）</p>'
            if is_demo else "")
    err = f'<p class="err">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · Saviland 运营台</title>
<style>
  :root {{ --brand:#8e2f4f; --ink:#1c1917; --line:#e4e0dc; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; min-height:100vh; display:grid; place-items:center;
    background: radial-gradient(1200px 600px at 50% -10%, #faf5f2, #f4efe9 60%, #efe8e1);
    font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink);
  }}
  .card {{
    width:min(92vw, 400px); background:#fff; border:1px solid var(--line);
    border-radius:16px; padding:34px 32px 28px;
    box-shadow:0 18px 60px rgba(60, 40, 30, .12);
  }}
  .brand {{ font-weight:800; letter-spacing:4px; color:var(--brand); font-size:15px; }}
  h1 {{ font-size:21px; margin:10px 0 4px; }}
  .sub {{ color:#6b6560; margin:0 0 22px; font-size:13px; }}
  label {{ display:block; font-size:12.5px; color:#57534e; margin:14px 0 6px; }}
  input {{
    width:100%; padding:11px 13px; font-size:14px; border:1px solid var(--line);
    border-radius:9px; outline:none; background:#fdfdfc;
  }}
  input:focus {{ border-color:var(--brand); box-shadow:0 0 0 3px rgba(142,47,79,.10); }}
  button {{
    width:100%; margin-top:22px; padding:12px; border:none; border-radius:9px;
    background:var(--brand); color:#fff; font-size:15px; font-weight:600; cursor:pointer;
  }}
  button:hover {{ background:#772643; }}
  .err {{ background:#fdeceb; color:#a3261c; border:1px solid #f6c9c4;
          padding:9px 12px; border-radius:9px; font-size:12.5px; margin:0 0 4px; }}
  .hint {{ margin:16px 0 0; font-size:12px; color:#78716c; line-height:1.7;
           border-top:1px solid var(--line); padding-top:14px; }}
  .hint code {{ background:#f5f2ef; padding:1px 5px; border-radius:4px; font-size:11.5px; }}
  .foot {{ margin:18px 0 0; font-size:11.5px; color:#a8a29e; text-align:center; }}
</style></head>
<body>
<form class="card" method="post" action="/login">
  <div class="brand">SAVILAND</div>
  <h1>运营台登录</h1>
  <p class="sub">这里是商家后台。买家看到的商城在 <a href="/shop">/shop</a>。</p>
  {err}
  <input type="hidden" name="next" value="{next_path}">
  <label for="u">账号</label>
  <input id="u" name="username" autocomplete="username" autofocus required>
  <label for="p">口令</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">登录</button>
  {hint}
  <p class="foot">演示系统 · 认证为演示级，未启用 MFA / RBAC</p>
</form>
</body></html>"""


async def login_page(request: Request) -> Response:
    nxt = request.query_params.get("next") or "/"
    token = request.cookies.get(SESSION_COOKIE)
    if token and _SESSIONS.get(token):
        return RedirectResponse(nxt, status_code=303)
    return HTMLResponse(_login_html(next_path=nxt))


async def login_submit(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    nxt = str(form.get("next") or "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"                                   # 防开放重定向

    if not check_credentials(username, password):
        return HTMLResponse(_login_html(error="账号或口令不对。", next_path=nxt),
                            status_code=401)

    token = create_session(credentials()[0])
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                    samesite="lax", path="/")
    return resp


async def logout(request: Request) -> Response:
    drop_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


async def whoami(request: Request) -> Response:
    user = current_user(request)
    return JSONResponse({"user": user, "auth": not auth_disabled(),
                         "logged_in": bool(user) or auth_disabled()})
