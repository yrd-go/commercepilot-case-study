# -*- coding: utf-8 -*-
"""买家端（前台商城）：只给买家看的商品浏览与详情页。

为什么单独一个模块，而不是在后台界面里加一页
--------------------------------------------
后台和前台是**两类东西**，混在一起会同时伤害两边：

| | 后台（`/`，商家侧） | 前台（`/shop`，买家侧） |
|---|---|---|
| 谁看 | 运营 / 我 | 买家 |
| 长什么样 | 稠密的表格、诊断卡片、多页签 | 大图、少字、一个 CTA |
| 怎么渲染 | SPA（手写 JS，切换视图不刷新） | 服务端渲染的多页（浏览器直接拿到 HTML） |
| 能看到什么 | 成本、毛利、ACOS、库存明细、运行档案 | 只看到买家该看到的：图、标题、五点、价格、评分、有货没货 |

所以这里是**独立的模块 + 独立的样式 + 独立的字段白名单**，
`server.py` 只是把它挂到 `/shop` 前缀下。它**不 import** 广告 / 库存 / 订单 / 运行档案
任何一个域模块 —— 这条边界由测试钉住（`test_storefront_cannot_import_backoffice`）。

数据从哪来
----------
- 图：链路 1 生成的主图（`outputs/hero_images/{sku}/`）。`_clean` 白底图做首图，
  `_banner` / `_split` 做后面的营销图 —— 这也正好对应亚马逊"白底主图 + 生活化附图"的要求。
- 文案：链路 1 落库的最新一版 `listing_copies`（标题 / 五点 / 描述）。
- 价格、评分、评价数、类目：商品主数据。
- 有货没货：只用 `products.stock` 这个粗粒度字段，**不碰分仓库存与安全库存**。

也就是说：**这个前台展示的，正是那条 AI 链路的产出**。
不是另写一套假数据 —— 改一句提示词、重跑一次链路，前台看到的商品页就会变。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .. import config
from ..domain import catalog as catalog_domain

# 买家端绝对不允许出现的字段（后台口径）。用于自检与测试。
FORBIDDEN_FIELDS = (
    "cost_usd", "cost", "margin", "gross_profit", "acos", "roas", "cpc", "ctr", "cvr",
    "spend", "campaign", "warehouse", "safety_stock", "lead_time", "reorder",
    "engine", "run_id", "prompt", "zhipu", "glm", "deepseek", "degraded",
)


# --------------------------------------------------------------------------
# 数据（白名单投影：前台只能拿到买家该看的字段）
# --------------------------------------------------------------------------
def _hero_images(sku: str) -> list[str]:
    """该 SKU 的主图，白底 clean 优先，其后是营销版式。"""
    d = config.OUTPUTS_DIR / "hero_images" / sku
    if not d.is_dir():
        return []
    order = {"clean": 0, "banner": 1, "split": 2}

    def rank(p: Path) -> tuple[int, str]:
        for key, i in order.items():
            if p.stem.endswith(f"_{key}"):
                return (i, p.name)
        return (9, p.name)

    return [f"/media/hero/{sku}/{p.name}" for p in sorted(d.glob("*.png"), key=rank)]


def _photo(sku: str) -> str | None:
    p = config.ASSETS_DIR / "products" / f"{sku}.png"
    return f"/media/product/products/{sku}.png" if p.is_file() else None


def _stock_label(stock: int) -> tuple[str, bool]:
    """把库存压成买家该看到的三种状态（不暴露分仓与补货参数）。"""
    if stock <= 0:
        return "Currently unavailable", False
    if stock < 20:
        return f"Only {stock} left in stock", True
    return "In Stock", True


def buyer_item(sku: str) -> dict[str, Any] | None:
    """一个商品的**买家视图**。字段是显式列举的，不整行转发。"""
    p = catalog_domain.get_product(sku)
    if not p:
        return None
    if (p.get("status") or "active") != "active":
        return None

    copy = catalog_domain.latest_listing_copy(sku, "en") or {}
    bullets = [b for b in (copy.get("bullets") or []) if str(b).strip()][:5]
    title = (copy.get("title") or p.get("title_en") or p.get("title_cn") or sku).strip()
    images = _hero_images(sku)
    if not images:
        photo = _photo(sku)
        images = [photo] if photo else []
    stock_label, purchasable = _stock_label(int(p.get("stock") or 0))

    return {
        "sku": sku,
        "title": title,
        "title_cn": p.get("title_cn"),
        "category": p.get("category"),
        "sub_category": p.get("sub_category"),
        "price": float(p.get("price_usd") or 0),
        "rating": p.get("rating"),
        "reviews": int(p.get("review_count") or 0),
        "bullets": bullets,
        "description": (copy.get("description") or "").strip(),
        "images": images,
        "stock_label": stock_label,
        "purchasable": purchasable,
        "has_copy": bool(copy),
    }


def _all_items() -> list[dict[str, Any]]:
    items = [buyer_item(p["sku"]) for p in catalog_domain.list_products()]
    return [i for i in items if i]


def _filter(items: list[dict], q: str, category: str) -> list[dict]:
    out = items
    if category:
        out = [i for i in out if i["category"] == category]
    if q:
        low = q.lower()
        out = [i for i in out
               if low in (i["title"] or "").lower() or low in (i["title_cn"] or "").lower()
               or low in i["sku"].lower()]
    return out


# --------------------------------------------------------------------------
# 渲染（服务端出 HTML：买家端不需要 SPA）
# --------------------------------------------------------------------------
def _esc(v: Any) -> str:
    return (str(v if v is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _stars(rating: Any) -> str:
    try:
        r = float(rating or 0)
    except (TypeError, ValueError):
        r = 0.0
    full = int(round(r))
    return "".join("★" if i < full else "☆" for i in range(5))


def _page(title: str, body: str, *, sku: str = "") -> HTMLResponse:
    nav = (
        '<a class="brand" href="/shop">SAVILAND</a>'
        '<form class="search" action="/shop" method="get" role="search">'
        f'<input name="q" placeholder="搜索商品" value="{_esc(sku)}">'
        '<button type="submit">搜索</button></form>'
        '<span class="cart" title="演示商城：不接真实交易">🛒 Cart <b>0</b></span>'
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="/shop/shop.css">
</head><body>
<header class="top">{nav}</header>
<main class="wrap">
{body}
</main>
<footer class="foot">
  <p>Saviland 演示商城 · 商品与图片来自公开资料，文案与主图由本项目的内容链路生成。</p>
  <p class="muted">This is a demo storefront — no checkout, no payment, no real orders.</p>
</footer>
</body></html>"""
    return HTMLResponse(html)


_CATEGORY_LABEL = {
    "美甲灯": "Nail Lamps", "甲油胶": "Gel Polish", "美甲工具": "Tools",
    "美甲饰品": "Accessories", "手足护理": "Hand & Cuticle Care",
    "美甲套装": "Kits", "卸甲与辅助": "Removal & Prep",
}
# ★ 注意：标签里只写裸 `&`。之前写成 `&amp;` 再套 `_esc()`，页面上就显示成了
#   字面量 "Removal &amp; Prep"（二次转义）。转义只在拼 HTML 的那一步做一次。


def render_home(items: list[dict], categories: list[str], q: str, category: str) -> HTMLResponse:
    chips = ['<a class="chip%s" href="/shop">All</a>' % (" on" if not category else "")]
    for c in categories:
        on = " on" if c == category else ""
        chips.append(f'<a class="chip{on}" href="/shop?c={_esc(c)}">{_esc(_CATEGORY_LABEL.get(c, c))}</a>')

    cards = []
    for it in items:
        img = it["images"][0] if it["images"] else ""
        cards.append(f"""
      <a class="card" href="/shop/p/{_esc(it['sku'])}">
        <div class="card-img">{f'<img src="{_esc(img)}" alt="{_esc(it["title"])}" loading="lazy">' if img else ''}</div>
        <div class="card-body">
          <div class="card-title">{_esc(it['title'])}</div>
          <div class="card-rate"><span class="stars">{_stars(it['rating'])}</span>
            <span class="muted">{_esc(it['rating'])} · {it['reviews']:,} ratings</span></div>
          <div class="card-price">{_money(it['price'])}</div>
          <div class="card-stock{' bad' if not it['purchasable'] else ''}">{_esc(it['stock_label'])}</div>
        </div>
      </a>""")

    head = (f'<h1 class="page-h">Results for “{_esc(q)}”</h1>' if q else
            '<h1 class="page-h">Nail care, made simple</h1>')
    # 这里是**买家**看的地方，所以写营销说明，不写"这份页面是链路的产出" ——
    # 那句说明属于页脚（演示声明），放在首屏会破坏"这是一家店"的感觉。
    sub = ('Everything you need for salon-quality nails at home — '
           'UV/LED lamps, gel polish, tools, and starter kits.')
    if not items:
        body_cards = '<div class="empty">没有找到匹配的商品。</div>'
    else:
        body_cards = f'<div class="grid">{"".join(cards)}</div>'

    body = f"""
<section class="hero-band">
  <div>
    <p class="eyebrow">SAVILAND · NAIL CARE</p>
    {head}
    <p class="sub">{sub}</p>
  </div>
</section>
<nav class="chips">{''.join(chips)}</nav>
<p class="count">{len(items)} products</p>
{body_cards}"""
    return _page("Saviland · Shop", body)


def render_detail(item: dict, related: list[dict]) -> HTMLResponse:
    images = item["images"]
    thumbs = "".join(
        f'<button class="thumb" data-src="{_esc(src)}">'
        f'<img src="{_esc(src)}" alt=""></button>' for src in images)
    main = images[0] if images else ""
    bullets = "".join(f"<li>{_esc(b)}</li>" for b in item["bullets"]) or \
        "<li class='muted'>该商品还没有生成文案。</li>"
    related_cards = "".join(f"""
      <a class="card small" href="/shop/p/{_esc(r['sku'])}">
        <div class="card-img">{f'<img src="{_esc(r["images"][0])}" alt="">' if r['images'] else ''}</div>
        <div class="card-body"><div class="card-title">{_esc(r['title'])}</div>
        <div class="card-price">{_money(r['price'])}</div></div>
      </a>""" for r in related[:4])

    cta = ('<button class="btn-buy">Add to Cart</button>' if item["purchasable"]
           else '<button class="btn-buy" disabled>Currently unavailable</button>')
    badge = ('<span class="badge">Best Seller</span>'
             if item["reviews"] > 8000 else "")

    body = f"""
<nav class="crumb"><a href="/shop">All Products</a> ›
  <a href="/shop?c={_esc(item['category'])}">{_esc(_CATEGORY_LABEL.get(item['category'], item['category']))}</a> ›
  <span>{_esc(item['title_cn'] or item['sku'])}</span></nav>

<div class="detail">
  <div class="gallery">
    <div class="main-img">{f'<img id="mainImg" src="{_esc(main)}" alt="{_esc(item["title"])}">' if main else ''}</div>
    <div class="thumbs">{thumbs}</div>
  </div>

  <div class="info">
    {badge}
    <h1>{_esc(item['title'])}</h1>
    <p class="cn-title">{_esc(item['title_cn'])}</p>
    <p class="rate"><span class="stars">{_stars(item['rating'])}</span>
      <b>{_esc(item['rating'])}</b>
      <span class="muted">{item['reviews']:,} ratings</span></p>
    <hr>
    <p class="price">{_money(item['price'])}</p>
    <p class="ship">FREE delivery · Ships from Saviland</p>
    <p class="stock{' bad' if not item['purchasable'] else ''}">{_esc(item['stock_label'])}</p>
    {cta}
    <p class="fine">演示商城：加入购物车不会产生真实订单，也不接支付。</p>
  </div>
</div>

<section class="block">
  <h2>About this item</h2>
  <ul class="bullets">{bullets}</ul>
  {f'<p class="desc">{_esc(item["description"])}</p>' if item['description'] else ''}
</section>

<section class="block">
  <h2>Product details</h2>
  <table class="spec">
    <tr><th>SKU</th><td>{_esc(item['sku'])}</td></tr>
    <tr><th>Category</th><td>{_esc(_CATEGORY_LABEL.get(item['category'], item['category']))}</td></tr>
    <tr><th>Rating</th><td>{_esc(item['rating'])} ({item['reviews']:,} ratings)</td></tr>
  </table>
</section>

{f'<section class="block"><h2>You may also like</h2><div class="grid four">{related_cards}</div></section>' if related_cards else ''}

<script>
// 图片切换（买家端只需要这一点交互，所以只用十几行原生 JS）
const img = document.getElementById('mainImg');
document.querySelectorAll('.thumb').forEach(b => b.addEventListener('click', () => {{
  if (!img) return;
  img.src = b.dataset.src;
  document.querySelectorAll('.thumb').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
}}));
</script>"""
    return _page(f"{item['title']} · Saviland", body)


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
async def shop_home(request: Request) -> Response:
    q = (request.query_params.get("q") or "").strip()
    category = (request.query_params.get("c") or "").strip()
    items = _all_items()
    cats = sorted({i["category"] for i in items if i["category"]})
    return render_home(_filter(items, q, category), cats, q, category)


async def shop_product(request: Request) -> Response:
    sku = (request.path_params.get("sku") or "").strip()
    item = buyer_item(sku)
    if not item:
        return RedirectResponse("/shop", status_code=303)
    same_cat = [i for i in _all_items()
                if i["category"] == item["category"] and i["sku"] != sku]
    return render_detail(item, same_cat)


async def shop_css(request: Request) -> Response:
    path = Path(__file__).resolve().parent / "static" / "shop.css"
    return Response(path.read_bytes(), media_type="text/css; charset=utf-8")


def shop_routes() -> list:
    """挂在 `/shop` 前缀下的路由（与后台的 `/api/*` 完全分开）。"""
    from starlette.routing import Route

    return [
        Route("/shop", shop_home),
        Route("/shop/", shop_home),
        Route("/shop/shop.css", shop_css),
        Route("/shop/p/{sku}", shop_product),
    ]
