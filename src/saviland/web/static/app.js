/* ==========================================================================
   Saviland 电商运营台 —— 前端（零依赖手写 SPA）
   结构：工具函数 → 图形 → 视图 → 路由
   ========================================================================== */
'use strict';

/* ----------------------------- 工具 ----------------------------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const money = (v, d = 2) => (v == null ? '—'
  : '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
const money0 = (v) => (v == null ? '—' : '$' + Math.round(Number(v)).toLocaleString('en-US'));
const pct = (v, d = 1) => (v == null ? '—' : (Number(v) * 100).toFixed(d) + '%');
const num = (v) => (v == null ? '—' : Number(v).toLocaleString('en-US'));
const dec = (v, d = 2) => (v == null ? '—' : Number(v).toFixed(d));
const icon = (id, size = 16) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;

const SEV_LABEL = { critical: '严重', warning: '预警', opportunity: '机会', info: '提示' };
const LEVEL_PILL = { stockout_risk: 'critical', overstock: 'warning', healthy: 'opportunity', no_sales: 'neutral' };

function deltaChip(d, invert = false) {
  if (!d || d.pct == null) return '<span class="delta flat">—</span>';
  const p = d.pct;
  const good = invert ? p < 0 : p > 0;
  const cls = Math.abs(p) < 0.0005 ? 'flat' : (good ? 'up' : 'down');
  const arrow = p > 0 ? '▲' : (p < 0 ? '▼' : '＝');
  return `<span class="delta ${cls}">${arrow} ${(p * 100).toFixed(1)}%</span>`;
}

function pill(sev, text) {
  return `<span class="pill ${sev}"><span class="dot"></span>${esc(text)}</span>`;
}

function toast(msg, isErr) {
  const wrap = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(8px)'; }, 3600);
  setTimeout(() => el.remove(), 4100);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  // 会话过期：接口返回 401。此时不该只弹一个"请求失败"，直接把人送回登录页，
  // 登录后再回到原来的页面（next 带上当前 hash 视图）。
  if (res.status === 401) {
    const next = encodeURIComponent('/' + (location.hash || '#/overview'));
    location.href = `/login?next=${next}`;
    throw new Error('会话已过期，正在返回登录页…');
  }
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) {
    const msg = (body && (body.error || body.detail)) || `请求失败（${res.status}）`;
    throw new Error(msg);
  }
  return body;
}

/* 全局状态 */
const S = {
  meta: null,
  view: 'overview',
  week: null,
  days: 30,
  filters: { category: '全部', q: '', invLevel: '全部' },
  selectedSkus: new Set(),
  jobs: {},
};

/* ----------------------------- 图形 ----------------------------- */
function sparkline(values, { w = 150, h = 34, color = 'currentColor', fill = true } = {}) {
  const vals = (values || []).filter((v) => v != null);
  if (vals.length < 2) return '';
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const step = w / (vals.length - 1);
  const pts = vals.map((v, i) => [i * step, h - 3 - ((v - min) / span) * (h - 6)]);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const area = `${d} L${w} ${h} L0 ${h} Z`;
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    ${fill ? `<path class="area" d="${area}" fill="${color}"/>` : ''}
    <path class="line" d="${d}" stroke="${color}"/>
  </svg>`;
}

function lineChart(series, labels, { w = 660, h = 210, pad = 26 } = {}) {
  const all = series.flatMap((s) => s.points).filter((v) => v != null);
  if (!all.length) return '<div class="empty">暂无数据</div>';
  const max = Math.max(...all, 1);
  const min = 0;
  const n = labels.length;
  const x = (i) => pad + (i * (w - pad * 2)) / Math.max(1, n - 1);
  const y = (v) => h - pad - ((v - min) / (max - min || 1)) * (h - pad * 2);
  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((t) => {
    const gy = pad + t * (h - pad * 2);
    const val = max - t * (max - min);
    return `<line class="grid-line" x1="${pad}" x2="${w - pad}" y1="${gy}" y2="${gy}"/>
      <text class="axis-label" x="${pad - 6}" y="${gy + 3}" text-anchor="end">${val >= 1000 ? Math.round(val / 1000) + 'k' : Math.round(val)}</text>`;
  }).join('');
  const paths = series.map((s) => {
    const pts = s.points.map((v, i) => [x(i), y(v == null ? 0 : v)]);
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
    const area = `${d} L${x(n - 1)} ${h - pad} L${pad} ${h - pad} Z`;
    return `<path d="${area}" fill="${s.color}" opacity=".08"/>
      <path class="series" d="${d}" stroke="${s.color}"/>
      ${pts.map((p) => `<circle class="dot" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.4" stroke="${s.color}"/>`).join('')}`;
  }).join('');
  const xlabels = labels.map((l, i) => (n <= 8 || i % Math.ceil(n / 7) === 0)
    ? `<text class="axis-label" x="${x(i)}" y="${h - 8}" text-anchor="middle">${esc(l)}</text>` : '').join('');
  const legend = series.map((s) => `<span class="row" style="gap:6px">
      <i style="width:9px;height:9px;border-radius:2px;background:${s.color};display:inline-block"></i>
      <span class="small muted">${esc(s.name)}</span></span>`).join('');
  return `<div class="row" style="gap:16px;margin-bottom:6px">${legend}</div>
    <svg class="linechart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${gridLines}${paths}${xlabels}</svg>`;
}

function barList(items, { target = null, max = null } = {}) {
  if (!items.length) return '<div class="empty">暂无数据</div>';
  const top = max || Math.max(...items.map((i) => i.value || 0), 0.0001);
  return `<div class="bars">${items.map((i) => {
    const ratio = Math.max(0, Math.min(1, (i.value || 0) / top));
    const cls = i.tone || '';
    const targetPct = target != null ? Math.min(100, (target / top) * 100) : null;
    return `<div class="bar-row">
      <div class="bl" title="${esc(i.label)}">${esc(i.label)}</div>
      <div class="bt"><div class="bf ${cls}" style="width:${(ratio * 100).toFixed(1)}%"></div>
        ${targetPct != null ? `<div class="bar-target" style="left:${targetPct.toFixed(1)}%"></div>` : ''}</div>
      <div class="bv">${esc(i.display)}</div>
    </div>`;
  }).join('')}</div>`;
}

/* ----------------------------- 布局组件 ----------------------------- */
function kpi({ label, value, note, spark }) {
  return `<div class="kpi">
    <div class="k">${esc(label)}</div>
    <div class="v">${value}</div>
    <div class="d">${note || ''}</div>
    ${spark ? `<div style="margin-top:auto;color:var(--brand)">${spark}</div>` : ''}
  </div>`;
}

function card(title, note, body, { cls = '', flush = false } = {}) {
  return `<section class="card ${cls} ${flush ? 'flush' : ''}">
    ${title ? `<div class="card-head" style="${flush ? 'padding:18px 22px 0' : ''}">
      <div class="card-title">${esc(title)}</div>
      ${note ? `<div class="card-note">${note}</div>` : ''}
    </div>` : ''}
    ${body}
  </section>`;
}

function table(headers, rows, { cls = '', empty = '暂无数据', rowAttr } = {}) {
  if (!rows.length) return `<div class="empty"><div class="big">·</div>${esc(empty)}</div>`;
  const head = headers.map((h) => {
    const isNum = typeof h === 'object' && h.num;
    const label = typeof h === 'object' ? h.label : h;
    return `<th class="${isNum ? 'num' : ''}">${esc(label)}</th>`;
  }).join('');
  const body = rows.map((r, ri) => {
    const cells = r.map((c, ci) => {
      const isNum = typeof headers[ci] === 'object' && headers[ci].num;
      return `<td class="${isNum ? 'num' : ''}">${c}</td>`;
    }).join('');
    const attrs = rowAttr ? rowAttr(ri) : '';
    return `<tr ${attrs}>${cells}</tr>`;
  }).join('');
  return `<div class="tbl-wrap"><table class="tbl ${cls}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function findingCard(f) {
  const ev = JSON.stringify(f.evidence || {});
  return `<div class="finding ${f.severity}">
    <div class="fh">${pill(f.severity, f.severity_label)}
      <span class="ft">${esc(f.subject_name)}</span>
      <span class="fr mono">${esc(f.rule)}</span></div>
    <div class="fl"><b>现象 · </b>${esc(f.diagnosis)}</div>
    <div class="fl"><b>动作 · </b>${esc(f.action)}</div>
    <div class="ev">${esc(ev)}</div>
    <div class="foot">
      <span>执行方 · ${esc(f.feeds_label)}</span>
      ${f.impact_usd ? `<span>可归因金额 · ${money0(f.impact_usd)}</span>` : ''}
    </div>
  </div>`;
}

function gallery(images, { charts = false, small = false } = {}) {
  if (!images || !images.length) return '<div class="empty">还没有生成主图</div>';
  const cls = ['gallery', charts ? 'charts' : '', small && !charts ? 'sm' : ''].filter(Boolean).join(' ');
  // 小尺寸缩略图**不渲染标题条**：130px 宽的格子里放"SKU · 版本名"必然折行溢出，
  // 反而把图压变形。标签放到 title（hover）和灯箱标题里就够了。
  return `<div class="${cls}">${images.map((im) => `
    <figure class="shot" data-src="${esc(im.url)}" data-cap="${esc(im.label)}" title="${esc(im.label)}">
      <img src="${esc(im.url)}" alt="${esc(im.label)}" loading="lazy">
      ${small && !charts ? '' : `<figcaption class="cap"><span>${esc(im.label)}</span>${im.kb ? `<span>${im.kb} KB</span>` : ''}</figcaption>`}
    </figure>`).join('')}</div>`;
}

function loading(text = '加载中…') {
  return `<div class="stack">
    <div class="skeleton" style="height:104px"></div>
    <div class="skeleton" style="height:280px"></div>
    <div class="small muted">${esc(text)}</div></div>`;
}

function emptyState(title, hint, action = '') {
  return `<div class="card"><div class="empty"><div class="big">◍</div>
    <div style="font-size:15px;color:var(--ink);font-weight:600;margin-bottom:6px">${esc(title)}</div>
    <div>${esc(hint)}</div><div style="margin-top:16px">${action}</div></div></div>`;
}

/* ----------------------------- 进度 / 任务 ----------------------------- */
function progressBlock(job) {
  const ratio = job.total ? Math.min(1, job.done / job.total) : 0;
  return `<div class="progress">
    <div class="pl"><span>${esc(job.title)}</span><span class="num">${(ratio * 100).toFixed(0)}%</span></div>
    <div class="pt"><div class="pf" style="width:${(ratio * 100).toFixed(1)}%"></div></div>
    <div class="small muted">${esc(job.progress_text || '')}　·　已用 ${job.elapsed_s}s</div>
  </div>
  ${(job.log && job.log.length) ? `<div class="logbox mt">${esc(job.log.join('\n'))}</div>` : ''}`;
}

function pollJob(jobId, onUpdate) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      onUpdate(job);
      if (job.status === 'done' || job.status === 'failed') { stopped = true; return; }
    } catch (e) {
      stopped = true;
      toast(e.message, true);
      return;
    }
    setTimeout(tick, 700);
  };
  tick();
}

/* ----------------------------- 灯箱 / 抽屉 ----------------------------- */
function openLightbox(src, cap) {
  $('#lbImg').src = src;
  $('#lbCap').textContent = cap || '';
  $('#lightbox').classList.add('on');
}
function closeLightbox() { $('#lightbox').classList.remove('on'); $('#lbImg').src = ''; }

function openDrawer(title, sub, bodyHtml) {
  $('#drawerTitle').textContent = title;
  $('#drawerSub').textContent = sub || '';
  $('#drawerBody').innerHTML = bodyHtml;
  $('#drawer').classList.add('on');
  $('#drawerMask').classList.add('on');
}
function closeDrawer() {
  $('#drawer').classList.remove('on');
  $('#drawerMask').classList.remove('on');
}

/* ============================ 视图：概览 ============================ */
const V = {};

V.overview = {
  title: '运营概览',
  sub: () => '一眼看清：生意、广告、库存，以及这周最该处理的问题',
  actions: () => `<button class="btn btn-sm" data-act="refresh">${icon('refresh')}刷新</button>`,
  async render() {
    const d = await api('/api/overview');
    if (d.empty) {
      return emptyState('还没有业务数据', '先灌入商品、库存、订单与广告数据，这个页面才有内容。',
        `<a class="btn btn-primary" href="#" data-act="seed-help">查看怎么灌数据</a>`);
    }
    const k = d.kpis;
    const chips = Object.entries(d.severity || {})
      .map(([sev, n]) => pill(sev, `${SEV_LABEL[sev] || sev} ${n}`)).join('');

    return `
    <div class="grid c4">
      ${kpi({ label: '近 30 天营收', value: money0(k.revenue30),
        note: `毛利 ${money0(k.gross_profit30)} · 毛利率 ${pct(k.gross_margin)}`,
        spark: sparkline(d.daily.map((x) => x.sales), { w: 180, h: 30 }) })}
      ${kpi({ label: '本周广告 ACOS', value: pct(k.acos),
        note: `目标 ≤ ${pct(k.target_acos, 0)} · 花费 ${money0(k.spend)}` })}
      ${kpi({ label: '商品与内容', value: `${k.products} <small>SKU</small>`,
        note: `${k.categories} 个类目 · 已生成文案 ${k.copies} 版 · 主图 ${k.hero_images} 张` })}
      ${kpi({ label: '库存状态', value: `${k.stockout} <small>断货风险</small>`,
        note: `压货 ${k.overstock} · 健康 ${k.healthy} · 共 ${k.skus} 个 SKU` })}
    </div>

    <div class="grid side mt">
      ${card('本周广告花费与销售', `报告周 ${esc(d.week)}`,
        lineChart([
          { name: '花费', color: 'var(--brand)', points: d.daily.map((x) => x.spend) },
          { name: '销售额', color: 'var(--gold)', points: d.daily.map((x) => x.sales) },
        ], d.daily.map((x) => x.date.slice(5)), { h: 300 }),
        { cls: '' })}
      ${card('本周诊断', `<span class="row" style="gap:6px">${chips}</span>`,
        `<div class="stack" style="gap:10px">
          ${d.top_findings.slice(0, 3).map((f) => `
            <div class="finding ${f.severity}" style="padding:12px 14px">
              <div class="fh">${pill(f.severity, f.severity_label)}
                <span class="ft" style="font-size:13px">${esc(f.subject_name)}</span></div>
              <div class="fl small">${esc(f.diagnosis).slice(0, 96)}…</div>
              <div class="foot"><span>执行方 · ${esc(f.feeds_label)}</span></div>
            </div>`).join('')}
          <div class="row" style="justify-content:space-between">
            <span class="small muted">可归因浪费 ${money0(k.waste)}</span>
            <a class="btn btn-sm" href="#/weekly">看完整周报 ${icon('arrow')}</a>
          </div>
        </div>`)}
    </div>

    <div class="grid c2 mt">
      ${card('渠道结构（近 30 天）', '',
        barList(d.channels.map((c) => ({
          label: c.channel, value: c.revenue, display: money0(c.revenue),
        }))))}
      ${card('两条 AI 链路', '从数据到产出，闭环在一次点击里',
        `<div class="stack" style="gap:14px">
          <div class="row" style="justify-content:space-between;align-items:flex-start">
            <div><div class="strong">主图与卖点生成</div>
              <div class="small muted">检索品牌知识 → 生成中英文案 → 合规校验 → 合成 3 种版式主图</div></div>
            <a class="btn btn-sm" href="#/listing">去生成 ${icon('arrow')}</a>
          </div>
          <div class="hr" style="margin:0"></div>
          <div class="row" style="justify-content:space-between;align-items:flex-start">
            <div><div class="strong">广告周报</div>
              <div class="small muted">统一口径算指标 → 规则诊断 → 归因与行动清单 → 图表与导出</div></div>
            <a class="btn btn-sm" href="#/weekly">去生成 ${icon('arrow')}</a>
          </div>
          <div class="hr" style="margin:0"></div>
          <div class="small muted">闭环：周报判定「根因在内容侧」的 SKU，会直接带进主图与卖点生成。</div>
        </div>`)}
    </div>`;
  },
};

/* ============================ 视图：商品 ============================ */
V.products = {
  title: '商品管理',
  sub: () => '商品主数据、文本资产与已生成的文案版本',
  actions: () => `<input type="search" id="prodQ" placeholder="搜索 SKU 或标题…" style="width:220px" value="${esc(S.filters.q)}">`,
  async render() {
    const d = await api('/api/products');
    const cats = ['全部', ...d.categories];
    const list = d.items.filter((p) =>
      (S.filters.category === '全部' || p.category === S.filters.category)
      && (!S.filters.q || (p.sku + p.title + p.title_en).toLowerCase().includes(S.filters.q.toLowerCase())));

    const chips = `<div class="chips" style="margin-bottom:14px">${cats.map((c) =>
      `<button class="chip ${c === S.filters.category ? 'on' : ''}" data-cat="${esc(c)}">${esc(c)}</button>`).join('')}</div>`;

    const rows = list.map((p) => [
      `<div class="row" style="gap:10px">
        <img class="thumb" src="${esc(p.image)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
        <div><div class="strong">${esc(p.title)}</div>
          <div class="tiny mono faint">${esc(p.sku)}</div></div></div>`,
      esc(p.category),
      money(p.price),
      pct(p.margin, 0),
      num(p.stock),
      `<span class="row" style="gap:6px">${p.rating ? `<b class="num">${dec(p.rating, 1)}</b>` : '—'}
        <span class="tiny faint">${num(p.reviews)} 评</span></span>`,
      p.has_copy ? pill('brand', '已生成') : '<span class="small faint">未生成</span>',
      p.evidence === 'verified' ? '<span class="pill neutral">有实据</span>' : '<span class="pill neutral">推断</span>',
    ]);

    return `${card('', `${list.length} / ${d.total} 个 SKU`, `${chips}
      ${table(['商品', '类目', { label: '售价', num: 1 }, { label: '毛利率', num: 1 },
               { label: '库存', num: 1 }, { label: '评分', num: 1 }, '文案', '数据'],
              rows, { rowAttr: (i) => `data-sku="${esc(list[i].sku)}" class="clickable"` })}`,
      { flush: true })}
      <div class="card-hint">点击任意一行查看详情：规格、卖点、五点描述、竞品与已生成的主图。</div>`;
  },
  bind() {
    $$('[data-cat]').forEach((b) => b.onclick = () => {
      S.filters.category = b.dataset.cat; render();
    });
    const q = $('#prodQ');
    if (q) q.oninput = () => {
      S.filters.q = q.value;
      render({ keepFocus: 'prodQ' });
    };
    $$('.tbl tr.clickable').forEach((tr) => tr.onclick = () => showProduct(tr.dataset.sku));
  },
};

async function showProduct(sku) {
  openDrawer('加载中…', sku, '<div class="skeleton" style="height:200px"></div>');
  try {
    const p = await api('/api/products/' + encodeURIComponent(sku));
    const copy = p.latest_copy;
    const body = `
      <div class="row" style="gap:16px;align-items:flex-start">
        <img src="${esc(p.image)}" alt="" style="width:120px;height:120px;object-fit:contain;border:1px solid var(--line);border-radius:12px;background:#fff">
        <div style="flex:1;min-width:0">
          <div class="row" style="gap:6px;margin-bottom:4px">
            ${pill('brand', p.category)} ${p.evidence === 'verified' ? pill('opportunity', '有公开实据') : pill('neutral', '规格为推断')}
          </div>
          <div class="small muted">${esc(p.title_en)}</div>
        </div>
      </div>
      <dl class="kv mt">
        <dt>售价 / 成本</dt><dd>${money(p.price)} / ${money(p.cost)}　毛利率 <b>${pct(p.margin, 0)}</b></dd>
        <dt>在库</dt><dd>${num(p.stock)} 件　评分 ${dec(p.rating, 1)}（${num(p.reviews)} 评）</dd>
        <dt>目标人群</dt><dd>${esc(p.audience || '—')}</dd>
      </dl>
      ${Object.keys(p.specs).length ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">规格</div>
        <dl class="kv">${Object.entries(p.specs).map(([k2, v]) => `<dt>${esc(k2)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : ''}
      ${p.selling_points.length ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">核心卖点</div>
        <ul class="list-plain">${p.selling_points.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}
      ${p.five_bullets.length ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">五点描述</div>
        <ul class="list-plain">${p.five_bullets.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}
      ${p.keywords.length ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">后台搜索词</div>
        <div class="chips">${p.keywords.map((k2) => `<span class="chip">${esc(k2)}</span>`).join('')}</div>` : ''}
      ${p.hero_images.length ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:10px">已生成主图 · ${p.hero_images.length} 张</div>
        ${gallery(p.hero_images)}` : ''}
      ${copy ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">最新文案 v${copy.version} · ${esc(copy.created_at)}</div>
        <div class="gh">${esc(copy.headline)}</div>
        <div class="small muted" style="margin-bottom:8px">${esc(copy.title)}</div>
        <ul class="list-plain">${copy.bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
      ${p.copies.length > 1 ? `<div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">版本记录</div>
        ${table(['版本', '主图大字', '时间'], p.copies.map((c) => [
          `<b>v${c.version}</b>`, esc(c.headline).slice(0, 40), `<span class="tiny faint">${esc(c.created_at)}</span>`]))}` : ''}
    `;
    openDrawer(p.title, `${p.sku} · ${p.category}`, body);
    bindGallery();
  } catch (e) {
    openDrawer('加载失败', sku, `<div class="empty">${esc(e.message)}</div>`);
  }
}

/* ============================ 视图：库存 ============================ */
V.inventory = {
  title: '库存与补货',
  sub: () => '判断依据不是「库存够不够」，而是「还能卖几天」',
  actions: () => '',
  async render() {
    const d = await api('/api/inventory');
    const levels = ['全部', 'stockout_risk', 'overstock', 'healthy', 'no_sales'];
    const label = { stockout_risk: '断货风险', overstock: '压货', healthy: '健康', no_sales: '无动销' };
    const list = d.items.filter((i) => S.filters.invLevel === '全部' || i.level === S.filters.invLevel);
    const t = d.thresholds;

    const chips = `<div class="chips" style="margin-bottom:14px">${levels.map((l) =>
      `<button class="chip ${S.filters.invLevel === l ? 'on' : ''}" data-lv="${l}">${l === '全部' ? '全部' : label[l]}
        ${l !== '全部' ? `<b>${d.levels[l] || 0}</b>` : ''}</button>`).join('')}</div>`;

    const rows = list.map((i) => [
      `<div><div class="strong">${esc(i.title)}</div><div class="tiny mono faint">${esc(i.sku)}</div></div>`,
      esc(i.category),
      pill(LEVEL_PILL[i.level] || 'neutral', i.level_label),
      num(i.available),
      dec(i.velocity, 1),
      i.days == null ? '<span class="faint">∞</span>' : `<b class="num">${dec(i.days, 0)}</b> 天`,
      `${i.lead_time} 天`,
      i.suggest ? `<b class="num">${num(i.suggest)}</b>` : '<span class="faint">—</span>',
    ]);

    return `
    <div class="grid c4">
      ${kpi({ label: '断货风险', value: String(d.levels.stockout_risk || 0), note: `可售 < ${t.stockout} 天` })}
      ${kpi({ label: '压货', value: String(d.levels.overstock || 0), note: `可售 > ${t.overstock} 天` })}
      ${kpi({ label: '健康', value: String(d.levels.healthy || 0), note: '库存与动销匹配' })}
      ${kpi({ label: '无动销', value: String(d.levels.no_sales || 0), note: `近 ${t.lookback} 天零订单` })}
    </div>
    <div class="mt">${card('', `${list.length} 个 SKU`, `${chips}
      ${table(['商品', '类目', '状态', { label: '可用', num: 1 }, { label: '日均销量', num: 1 },
               { label: '可售天数', num: 1 }, { label: '补货周期', num: 1 }, { label: '建议补货', num: 1 }],
              rows, { rowAttr: (i) => `data-sku="${esc(list[i].sku)}" class="clickable"` })}`, { flush: true })}
      <div class="card-hint">
        可售天数 = 可用库存 ÷ 近 ${t.lookback} 天日均销量；建议补货 = 日销 ×（补货在途 + 15 天安全库存）− 可用库存。
        低于 ${t.stockout} 天预警、高于 ${t.overstock} 天视为压货。点击行看分仓明细。
      </div></div>`;
  },
  bind() {
    $$('[data-lv]').forEach((b) => b.onclick = () => { S.filters.invLevel = b.dataset.lv; render(); });
    $$('.tbl tr.clickable').forEach((tr) => tr.onclick = async () => {
      const sku = tr.dataset.sku;
      const d = await api('/api/inventory');
      const wh = (d.warehouses || {})[sku] || [];
      const item = d.items.find((i) => i.sku === sku) || {};
      openDrawer(item.title || sku, sku, `
        <dl class="kv"><dt>状态</dt><dd>${pill(LEVEL_PILL[item.level] || 'neutral', item.level_label || '')}</dd>
        <dt>可用 / 在库</dt><dd>${num(item.available)} / ${num(item.on_hand)}</dd>
        <dt>日均销量</dt><dd>${dec(item.velocity, 2)} 件/天</dd>
        <dt>可售天数</dt><dd>${item.days == null ? '∞（无动销）' : dec(item.days, 1) + ' 天'}</dd>
        <dt>补货周期</dt><dd>${item.lead_time} 天</dd>
        <dt>建议补货</dt><dd><b>${num(item.suggest)}</b> 件</dd></dl>
        <div class="hr"></div>
        <div class="card-title" style="margin-bottom:8px">分仓库存</div>
        ${table(['仓库', { label: '在库', num: 1 }, { label: '预留', num: 1 }, { label: '安全库存', num: 1 }, { label: '补货周期', num: 1 }],
          wh.map((w) => [esc(w.warehouse), num(w.on_hand), num(w.reserved), num(w.safety), w.lead_time + ' 天']))}`);
    });
  },
};

/* ============================ 视图：订单 ============================ */
V.orders = {
  title: '订单与销售',
  sub: () => '营收剔除退款，毛利 = 营收 − 成本',
  actions: () => `<div class="seg" id="daysSeg">${[7, 30, 60].map((d) =>
    `<button data-days="${d}" class="${S.days === d ? 'on' : ''}">近 ${d} 天</button>`).join('')}</div>`,
  async render() {
    const d = await api('/api/orders?days=' + S.days);
    const s = d.summary;
    return `
    <div class="grid c4">
      ${kpi({ label: `近 ${d.days} 天营收`, value: money0(s.revenue),
        note: `${num(s.units)} 件 · ${num(s.order_rows)} 笔订单行`,
        spark: sparkline(d.daily.map((x) => x.revenue), { w: 180, h: 30 }) })}
      ${kpi({ label: '毛利', value: money0(s.gross_profit), note: `毛利率 ${pct(s.gross_margin)} · 成本 ${money0(s.cogs)}` })}
      ${kpi({ label: '退款', value: `${num(s.refund_units)} <small>件</small>`, note: `退款率 ${pct(s.refund_rate)}` })}
      ${kpi({ label: '客单价', value: money(s.units ? s.revenue / s.units : null), note: '营收 ÷ 销量' })}
    </div>
    <div class="grid side mt">
      ${card('营收趋势', `近 ${d.days} 天`,
        lineChart([{ name: '营收', color: 'var(--brand)', points: d.daily.map((x) => x.revenue) }],
          d.daily.map((x) => x.date.slice(5))))}
      ${card('分渠道', '按营收排序',
        barList(d.by_channel.map((c) => ({ label: c.channel, value: c.revenue, display: money0(c.revenue) }))))}
    </div>
    <div class="grid c2 mt">
      ${card('分类目', '', barList(d.by_category.map((c) => ({
        label: c.category, value: c.revenue, display: money0(c.revenue) }))))}
      ${card('Top 商品', '按营收', table(
        ['商品', { label: '销量', num: 1 }, { label: '营收', num: 1 }, { label: '毛利', num: 1 }],
        d.top_products.map((p) => [
          `<div><div class="strong">${esc(p.title)}</div><div class="tiny mono faint">${esc(p.sku)}</div></div>`,
          num(p.units), money(p.revenue), money(p.gross_profit)]), { flush: true }))}
    </div>`;
  },
  bind() {
    $$('[data-days]').forEach((b) => b.onclick = () => {
      S.days = Number(b.dataset.days); render();
    });
  },
};

/* ============================ 视图：广告 ============================ */
V.ads = {
  title: '广告投放',
  sub: () => '口径统一在一处：ACOS = 花费 / 广告销售额',
  actions: () => weekSelect(),
  async render() {
    const d = await api('/api/ads' + (S.week ? `?week=${S.week}` : ''));
    const t = d.total;
    const camps = d.by_campaign.map((c) => ({
      label: c.name.replace('Saviland ', '').slice(0, 14),
      value: c.spend,
      display: c.acos == null ? '无转化' : pct(c.acos),
      tone: c.acos == null ? 'bad' : (c.acos > 0.5 ? 'bad' : (c.acos > d.target_acos ? 'warn' : 'good')),
    }));

    const rows = d.by_campaign.map((c) => [
      `<div><span class="strong nm" title="${esc(c.name)}">${esc(c.name)}</span>
        <span class="tiny faint">${esc(c.type)}</span></div>`,
      `<span class="mono tiny">${esc(c.sku)}</span>`,
      num(c.impressions), num(c.clicks), pct(c.ctr, 2),
      money(c.spend), num(c.orders), money(c.sales),
      pct(c.cvr, 2), money(c.cpc),
      c.acos == null ? '<span class="pill critical">无转化</span>'
        : `<b class="num" style="color:${c.acos > 0.5 ? 'var(--bad)' : (c.acos > d.target_acos ? 'var(--warn)' : 'var(--good)')}">${pct(c.acos)}</b>`,
      pct(c.budget_use, 0),
    ]);

    // 12 列的表必须占满整行：塞进半宽列里活动名会竖排、右侧列被裁掉。
    // 半宽只留给"排名 + 趋势"这种天然窄的内容。
    return `
    <div class="grid c5">
      ${kpi({ label: '花费', value: money0(t.spend), note: `环比 ${deltaChip(d.wow.spend)}` })}
      ${kpi({ label: '广告销售额', value: money0(t.sales), note: `环比 ${deltaChip(d.wow.sales)}` })}
      ${kpi({ label: 'ACOS', value: pct(t.acos), note: `目标 ≤ ${pct(d.target_acos, 0)}` })}
      ${kpi({ label: 'ROAS', value: dec(t.roas, 2), note: `目标 ≥ ${dec(d.target_roas, 2)}` })}
      ${kpi({ label: 'CTR / CVR', value: `${pct(t.ctr, 2)}`, note: `CVR ${pct(t.cvr, 2)} · CPC ${money(t.cpc)}` })}
    </div>
    <div class="mt">${card('活动明细', `${d.by_campaign.length} 个活动`,
      table(['活动', 'SKU', { label: '曝光', num: 1 }, { label: '点击', num: 1 }, { label: 'CTR', num: 1 },
             { label: '花费', num: 1 }, { label: '订单', num: 1 }, { label: '销售', num: 1 },
             { label: 'CVR', num: 1 }, { label: 'CPC', num: 1 }, { label: 'ACOS', num: 1 },
             { label: '预算用', num: 1 }], rows), { flush: true })}</div>
    <div class="grid side mt">
      ${card('ACOS 排名', '竖线 = 目标线',
        barList(camps, { target: d.target_acos, max: Math.max(...d.by_campaign.map((c) => c.acos || 1), d.target_acos) }))}
      ${card('每日花费与销售', `${d.week_start} ~ ${d.week_end}`,
        lineChart([
          { name: '花费', color: 'var(--brand)', points: d.daily.map((x) => x.spend) },
          { name: '销售额', color: 'var(--gold)', points: d.daily.map((x) => x.sales) },
        ], d.daily.map((x) => x.date.slice(5)), { w: 560, h: 230 }))}
    </div>
    <div class="mt">${card('每日投放事实', '逐日明细', table(
      ['日期', { label: '曝光', num: 1 }, { label: '点击', num: 1 }, { label: '花费', num: 1 },
       { label: '订单', num: 1 }, { label: '销售', num: 1 }, { label: 'ACOS', num: 1 }],
      d.daily.map((x) => [esc(x.date), num(x.impressions), num(x.clicks), money(x.spend),
        num(x.orders), money(x.sales), x.acos == null ? '—' : pct(x.acos)]), { flush: true }))}</div>`;
  },
  bind() { bindWeekSelect(); },
};

function weekSelect() {
  const weeks = (S.meta && S.meta.weeks) || [];
  return `<select id="weekSel">${weeks.map((w) =>
    `<option value="${w}" ${w === (S.week || S.meta.report_week) ? 'selected' : ''}>${w}</option>`).join('')}</select>`;
}
function bindWeekSelect() {
  const sel = $('#weekSel');
  if (sel) sel.onchange = () => { S.week = sel.value; render(); };
}

/* ============================ 视图：主图与卖点生成 ============================ */
V.listing = {
  title: '主图与卖点生成',
  sub: () => '检索品牌知识 → 生成中英文案 → 合规校验 → 合成 3 种版式主图',
  actions: () => `<button class="btn btn-sm" id="selAll">全选</button>
    <button class="btn btn-sm" id="selNone">清空</button>`,
  async render() {
    const [pd, cd, fd] = await Promise.all([
      api('/api/products'), api('/api/copies'), api('/api/findings'),
    ]);
    const targets = fd.listing_targets || [];
    const byCat = {};
    pd.items.forEach((p) => { (byCat[p.category] = byCat[p.category] || []).push(p); });

    const chips = Object.entries(byCat).map(([cat, items]) => `
      <div style="margin-bottom:10px">
        <div class="tiny faint" style="margin-bottom:6px">${esc(cat)}</div>
        <div class="chips">${items.map((p) => {
          const on = S.selectedSkus.has(p.sku);
          return `<button class="chip ${on ? 'on' : ''}" data-sku="${esc(p.sku)}"
            title="${esc(p.title)}">${esc(p.sku.replace('SV-', ''))}${p.has_copy ? ' ·已生成' : ''}</button>`;
        }).join('')}</div>
      </div>`).join('');

    const job = S.jobs.listing ? S.jobs.listing : null;

    // 右侧补一块"最近生成的主图"，否则那半栏下面全是空白
    const recentHeroes = cd.items.flatMap((i) => (i.hero_images || [])
      .filter((h) => h.variant === 'clean')
      .map((h) => ({ url: h.url, label: `${i.sku.replace('SV-', '')} · ${h.label}` }))).slice(0, 6);

    return `
    <div class="grid side">
      ${card('选择商品', `${S.selectedSkus.size} 个已选`, `
        ${targets.length ? `<div class="finding warning" style="margin-bottom:14px;padding:11px 14px">
          <div class="fh">${pill('warning', '建议优先')}<span class="ft" style="font-size:13px">周报判定 ${targets.length} 个 SKU 根因在内容侧</span></div>
          <div class="fl small">${targets.map((t) => esc(t)).join('、')}</div>
          <div class="row mt" style="margin-top:8px"><button class="btn btn-sm" id="pickTargets">选中这些 SKU</button></div>
        </div>` : ''}
        <div style="max-height:360px;overflow-y:auto;padding-right:6px">${chips}</div>
        <div class="hr"></div>
        <div class="row" style="justify-content:space-between">
          <div class="row" style="gap:8px">
            <label class="small muted">主图版式</label>
            <select id="variantSel"><option value="3">3 种（推荐）</option><option value="1">1 种</option></select>
            <select id="engineSel" title="离线模式不调用任何模型，几秒出结果，适合先看效果">
              <option value="">在线模型</option>
              <option value="offline">离线模板（不花钱）</option>
            </select>
          </div>
          <button class="btn btn-primary" id="genBtn" ${S.selectedSkus.size ? '' : 'disabled'}>
            ${icon('play')} 生成文案与主图</button>
        </div>
        <div class="card-hint">每个 SKU 约 35 秒（一次模型调用 + 3 路知识检索 + 3 张主图渲染）；
          选「离线模板」则几秒完成，产物结构完整但用词是模板化的。</div>`)}
      <div class="stack">
        ${card('任务进度', job ? `${job.status}` : '未开始',
          job ? progressBlock(job) : `<div class="empty" style="padding:24px 0">还没有开始生成</div>`)}
        ${card('已生成的文案', `${cd.total} 个 SKU`, cd.total
          ? `<div class="chips">${cd.items.map((i) => `<a class="chip" href="#/listing" data-show="${esc(i.sku)}">${esc(i.sku)}</a>`).join('')}</div>
             <div class="card-hint">点击 SKU 可查看该商品的完整文案与主图。</div>`
          : '<div class="empty">还没有生成任何文案</div>')}
        ${recentHeroes.length ? card('最近生成的主图', `${recentHeroes.length} 张 · 点击放大`,
          gallery(recentHeroes, { small: true })) : ''}
      </div>
    </div>
    <div class="mt" id="genResult"></div>`;
  },
  bind() {
    $$('[data-sku].chip').forEach((b) => b.onclick = () => {
      const sku = b.dataset.sku;
      if (S.selectedSkus.has(sku)) S.selectedSkus.delete(sku); else S.selectedSkus.add(sku);
      render();
    });
    const pick = $('#pickTargets');
    if (pick) pick.onclick = async () => {
      const fd = await api('/api/findings');
      (fd.listing_targets || []).forEach((s) => S.selectedSkus.add(s));
      render();
    };
    const all = $('#selAll');
    if (all) all.onclick = async () => {
      const pd = await api('/api/products');
      pd.items.forEach((p) => S.selectedSkus.add(p.sku));
      render();
    };
    const none = $('#selNone');
    if (none) none.onclick = () => { S.selectedSkus.clear(); render(); };

    const btn = $('#genBtn');
    if (btn) btn.onclick = async () => {
      const skus = Array.from(S.selectedSkus);
      if (!skus.length) return;
      const variants = Number(($('#variantSel') || {}).value || 3);
      const engine = ($('#engineSel') || {}).value || '';
      btn.disabled = true;
      try {
        const { job_id } = await api('/api/generate/listing', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ skus, variants, engine }),
        });
        S.jobs.listing = { id: job_id, status: 'queued', done: 0, total: skus.length, log: [], progress_text: '排队中…', elapsed_s: 0, title: '生成文案与主图' };
        const panel = $('#genResult');
        panel.innerHTML = card('生成进度', '实时', '<div id="liveProgress"></div>');
        const box = $('#liveProgress');
        pollJob(job_id, (job) => {
          S.jobs.listing = job;
          box.innerHTML = progressBlock(job);
          if (job.status === 'done') {
            toast('生成完成');
            S.selectedSkus.clear();
            setTimeout(render, 600);
          } else if (job.status === 'failed') {
            toast('生成失败：' + job.error, true);
            btn.disabled = false;
          }
        });
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
      }
    };
    $$('[data-show]').forEach((a) => a.onclick = (ev) => {
      ev.preventDefault(); showProduct(a.dataset.show);
    });
    bindGallery();
  },
};

/* ============================ 视图：广告周报 ============================ */
V.weekly = {
  title: '广告周报',
  sub: () => '统一口径 → 规则诊断 → 归因与行动清单 → 图表与导出',
  actions: () => weekSelect(),
  async render() {
    const [fd, rd] = await Promise.all([
      api('/api/findings' + (S.week ? `?week=${S.week}` : '')),
      api('/api/report' + (S.week ? `?week=${S.week}` : '')),
    ]);
    const s = fd.summary;
    const job = S.jobs.weekly || null;
    const dls = rd.downloads || {};

    const dlBtns = `
      ${dls.html ? `<a class="btn btn-primary" href="${dls.html}" target="_blank">${icon('external')} 打开完整周报</a>` : ''}
      ${dls.md ? `<a class="btn" href="${dls.md}">${icon('download')} Markdown</a>` : ''}
      ${dls.xlsx ? `<a class="btn" href="${dls.xlsx}">${icon('download')} Excel 明细</a>` : ''}`;

    return `
    <div class="grid c5">
      ${kpi({ label: '诊断问题', value: String(s.total), note: `严重 ${(s.by_severity || {}).critical || 0} · 预警 ${(s.by_severity || {}).warning || 0}` })}
      ${kpi({ label: '可归因浪费', value: money0(s.waste_usd), note: '按目标 ACOS 折算' })}
      ${kpi({ label: '需改内容', value: String(s.listing_actions), note: '交给主图与卖点生成' })}
      ${kpi({ label: '需调投放', value: String(s.ads_actions), note: '竞价 / 预算 / 否定词' })}
      ${kpi({ label: '报告周', value: esc(fd.week), note: rd.meta && rd.meta.generated_at ? `生成于 ${esc(rd.meta.generated_at)}` : '尚未生成' })}
    </div>

    <div class="grid side mt">
      ${card('本周诊断', `${fd.items.length} 条 · 按严重度排序`,
        `<div class="stack" style="gap:10px;max-height:560px;overflow-y:auto;padding-right:4px">
          ${fd.items.length ? fd.items.map(findingCard).join('') : '<div class="empty">本周没有识别到问题</div>'}
        </div>`)}
      <div class="stack">
        ${card('报告与导出', rd.exists ? '可直接打开 / 下载' : '还没生成报告',
          rd.exists ? `
            <div class="row" style="gap:8px;flex-wrap:wrap">${dlBtns}</div>
            <div class="card-hint">HTML 自包含（图片已内嵌），可以直接转发给不看后台的人。</div>`
          : `<div class="empty" style="padding:18px 0">点下面的按钮生成本周报告</div>`)}
        ${card('生成报告', job ? job.status : '未开始',
          job ? progressBlock(job)
            : `<div class="row" style="gap:8px;margin-bottom:10px">
                 <select id="wkEngine" style="flex:1">
                   <option value="">在线模型（推荐）</option>
                   <option value="offline">离线模板（不花钱，几秒）</option>
                 </select>
               </div>
               <button class="btn btn-primary" id="genWeek" style="width:100%">${icon('play')} 生成 ${esc(fd.week)} 周报</button>
               <div class="card-hint">约 100 秒。会重新计算指标、跑诊断规则、出 5 张图并导出三种格式。</div>`)}
        ${card('闭环：需重写文案的 SKU', fd.listing_targets.length ? `${fd.listing_targets.length} 个` : '无',
          fd.listing_targets.length ? `
            <div class="stack" style="gap:6px">
              ${fd.targets_detail.map((t) => `<div class="row" style="justify-content:space-between">
                <span class="small">${esc(t.title)}</span><span class="tiny mono faint">${esc(t.sku)}</span></div>`).join('')}
            </div>
            <button class="btn btn-sm mt" id="toListing">${icon('arrow')} 带去主图与卖点生成</button>`
          : '<div class="small muted">本周没有根因在内容侧的问题。</div>')}
      </div>
    </div>

    ${rd.charts.length ? `<div class="mt">${card('图表', '来自本次报告', gallery(rd.charts.map((c) => ({
      url: c.url, label: c.label })), { charts: true }))}</div>` : ''}`;
  },
  bind() {
    bindWeekSelect();
    bindGallery();
    const btn = $('#genWeek');
    if (btn) btn.onclick = async () => {
      btn.disabled = true;
      try {
        const { job_id } = await api('/api/generate/weekly', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ week: S.week || (S.meta && S.meta.report_week),
                                 engine: ($('#wkEngine') || {}).value || '' }),
        });
        S.jobs.weekly = { id: job_id, status: 'queued', done: 0, total: 3, log: [], progress_text: '排队中…', elapsed_s: 0, title: '生成周报' };
        const panel = $('#genWeek').closest('.card');
        panel.innerHTML = `<div class="card-head"><div class="card-title">生成报告</div><div class="card-note">实时</div></div><div id="wkProgress"></div>`;
        pollJob(job_id, (job) => {
          S.jobs.weekly = job;
          const box = $('#wkProgress');
          if (box) box.innerHTML = progressBlock(job);
          if (job.status === 'done') { toast('周报已生成'); S.jobs.weekly = null; setTimeout(render, 500); }
          if (job.status === 'failed') { toast('生成失败：' + job.error, true); }
        });
      } catch (e) { toast(e.message, true); btn.disabled = false; }
    };
    const to = $('#toListing');
    if (to) to.onclick = async () => {
      const fd = await api('/api/findings' + (S.week ? `?week=${S.week}` : ''));
      (fd.listing_targets || []).forEach((sk) => S.selectedSkus.add(sk));
      location.hash = '#/listing';
    };
  },
};

/* ============================ 视图：知识库检索 ============================ */
V.rag = {
  title: '知识库检索',
  sub: () => '向量 + 关键词混合检索，每一条都能追溯到来源文件',
  actions: () => `<button class="btn btn-sm" id="rebuildIdx">${icon('refresh')}重建索引</button>`,
  async render() {
    const st = await api('/api/rag/status');
    const res = S.ragResult;
    return `
    <div class="grid c4">
      ${kpi({ label: '向量片段', value: num(st.chunks), note: st.ready ? '索引就绪' : '尚未建立' })}
      ${kpi({ label: '知识库文档', value: String(st.docs), note: `${num(st.chars)} 字节（合并后）` })}
      ${kpi({ label: '检索模式', value: res ? esc(res.mode) : '—', note: res ? `${res.latency_ms} ms · 命中 ${res.hits.length} 条` : '输入问题后显示' })}
      ${kpi({ label: '索引状态', value: st.ready ? '就绪' : '待建', note: esc(st.error || '无异常') })}
    </div>
    <div class="mt">${card('检索', '看的是「品牌知识库」里的哪几段被命中',
      `<div class="row" style="gap:10px">
        <input type="search" id="ragQ" placeholder="例如：哪些词不能用，会被平台下架" style="flex:1;min-width:220px"
          value="${esc((res && res.query) || '')}">
        <select id="ragK">${[3, 5, 8].map((k) => `<option value="${k}">Top ${k}</option>`).join('')}</select>
        <button class="btn btn-primary" id="ragGo">${icon('search')} 检索</button>
      </div>
      <div class="chips mt">${(st.samples || []).map((s) => `<button class="chip" data-q="${esc(s)}">${esc(s)}</button>`).join('')}</div>`,
      { flush: false })}</div>
    <div class="mt" id="ragOut">${res ? ragResultHtml(res) : `
      <div class="card"><div class="empty">
        <div class="big">⌕</div>
        <div style="font-size:15px;color:var(--ink);font-weight:600;margin-bottom:6px">输入一个问题试试</div>
        <div>检索会同时走两路：向量语义 + 关键词命中，再按权重融合排序；<br>
        每条结果都能反查到具体是哪个知识库文件、第几行。</div>
      </div></div>`}</div>`;
  },
  bind() {
    const go = $('#ragGo');
    const q = $('#ragQ');
    const run = async (text) => {
      const query = (text || (q && q.value) || '').trim();
      if (!query) return;
      const k = Number(($('#ragK') || {}).value || 5);
      $('#ragOut').innerHTML = '<div class="skeleton" style="height:200px"></div>';
      try {
        S.ragResult = await api(`/api/rag/search?q=${encodeURIComponent(query)}&k=${k}`);
        $('#ragOut').innerHTML = ragResultHtml(S.ragResult);
      } catch (e) { toast(e.message, true); $('#ragOut').innerHTML = ''; }
    };
    if (go) go.onclick = () => run();
    if (q) q.onkeydown = (e) => { if (e.key === 'Enter') run(); };
    $$('[data-q]').forEach((b) => b.onclick = () => { if (q) q.value = b.dataset.q; run(b.dataset.q); });
    const rb = $('#rebuildIdx');
    if (rb) rb.onclick = async () => {
      rb.disabled = true;
      toast('正在重建索引…');
      try { const r = await api('/api/rag/rebuild', { method: 'POST' }); toast(`索引已重建：${r.chunks} 个片段`); render(); }
      catch (e) { toast(e.message, true); rb.disabled = false; }
    };
    // 首次进入自动跑一次示例检索：否则整页只有输入框，看起来像"没做完"。
    if (!S.ragResult && S.meta && S.meta.knowledge && S.meta.knowledge.ready) {
      const first = $('[data-q]');
      if (first) setTimeout(() => run(first.dataset.q), 160);
    }
  },
};

function ragResultHtml(res) {
  if (!res.hits.length) return `<div class="card"><div class="empty">没有命中。${esc(res.note || '')}</div></div>`;
  return `<div class="stack">
    <div class="row" style="gap:10px;flex-wrap:wrap">
      ${pill('brand', `模式 ${res.mode}`)}
      ${res.rewritten ? pill('info', `改写：${res.rewritten}`) : ''}
      ${res.keywords && res.keywords.length ? `<span class="small muted">参与加分的词：${res.keywords.map(esc).join('、')}</span>` : ''}
    </div>
    ${res.hits.map((h, i) => `<div class="finding info">
      <div class="fh">${pill('info', `#${i + 1}`)}<span class="ft">${esc(h.source)}</span>
        <span class="fr">第 ${h.line_start}~${h.line_end} 行</span></div>
      <div class="row" style="gap:14px">
        <span class="small muted">融合分 <b class="num" style="color:var(--ink)">${dec(h.score, 4)}</b></span>
        <span class="small muted">向量 ${dec(h.vec, 4)}</span>
        <span class="small muted">关键词 ${dec(h.kw, 4)}</span>
        ${h.matched && h.matched.length ? `<span class="small muted">命中词：${h.matched.slice(0, 8).map(esc).join('、')}</span>` : ''}
      </div>
      <div class="ev">${esc(h.content.slice(0, 420))}${h.content.length > 420 ? '…' : ''}</div>
    </div>`).join('')}
  </div>`;
}

/* ============================ 视图：运行档案 ============================ */
V.runs = {
  title: '运行档案',
  sub: () => '技术视图：每次链路执行都留痕，含降级与失败记录',
  actions: () => `<a class="btn btn-sm" href="#/overview">${icon('grid')} 回概览</a>`,
  async render() {
    const d = await api('/api/runs');
    const statusPill = (s) => {
      const map = { ok: ['opportunity', '正常'], degraded: ['warning', '降级'],
        failed: ['critical', '失败'], interrupted: ['neutral', '中断'], running: ['info', '进行中'] };
      const [cls, label] = map[s] || ['neutral', s];
      return pill(cls, label);
    };
    return `
    <div class="grid c3">
      ${card('引擎使用', '这里才需要看引擎——它是追溯的一部分',
        table(['引擎', { label: '次数', num: 1 }, { label: '平均耗时', num: 1 }],
          (d.engine_usage || []).map((u) => [`<span class="mono small">${esc(u.engine)}</span>`,
            num(u.runs), `${Math.round((u.avg_ms || 0) / 100) / 10}s`]), { flush: true }))}
      ${card('状态分布', '', Object.entries(d.by_status || {}).map(([pipe, st]) =>
        `<div class="row" style="justify-content:space-between;margin-bottom:6px">
          <span class="small mono">${esc(pipe)}</span>
          <span class="row" style="gap:6px">${Object.entries(st).map(([k, v]) => statusPill(k) + `<b class="num small">${v}</b>`).join('')}</span>
        </div>`).join('') || '<div class="empty">暂无</div>')}
      ${card('产物类型', '', `<div class="chips">${(d.artifact_kinds || []).map((a) =>
        `<span class="chip">${esc(a.kind)} <b>${a.count}</b></span>`).join('')}</div>`)}
    </div>
    <div class="mt">${card('最近运行', `${d.items.length} 条`, table(
      ['运行编号', '链路', '对象', '引擎', '状态', { label: '检索', num: 1 }, { label: '耗时', num: 1 }, '时间', '备注'],
      d.items.map((r) => [
        `<span class="mono tiny">${esc(r.run_id)}</span>`, esc(r.pipeline), esc(r.target || '—'),
        `<span class="mono tiny muted">${esc(r.engine || '—')}</span>`, statusPill(r.status),
        num(r.rag_hits), `${Math.round(r.latency_ms / 100) / 10}s`,
        `<span class="tiny faint">${esc(r.created_at)}</span>`,
        `<span class="tiny muted">${esc((r.note || '').slice(0, 60))}</span>`]),
      { rowAttr: (i) => `data-run="${esc(d.items[i].run_id)}" class="clickable"` }), { flush: true })}</div>`;
  },
  bind() {
    $$('[data-run]').forEach((tr) => tr.onclick = async () => {
      const id = tr.dataset.run;
      openDrawer('产物清单', id, '<div class="skeleton" style="height:160px"></div>');
      try {
        const d = await api(`/api/runs/${encodeURIComponent(id)}/artifacts`);
        openDrawer('产物清单', id, d.items.length ? table(
          ['类型', '路径', '元数据'],
          d.items.map((a) => [`<b class="small">${esc(a.kind)}</b>`,
            a.url ? `<a class="mono tiny" href="${esc(a.url)}" target="_blank">${esc(a.path.split(/[\\/]/).pop())}</a>`
              : `<span class="mono tiny muted">${esc(a.path)}</span>`,
            `<span class="tiny muted">${esc(JSON.stringify(a.meta))}</span>`]))
          : '<div class="empty">该运行没有登记产物</div>');
      } catch (e) { toast(e.message, true); }
    });
  },
};

/* ----------------------------- 路由与渲染 ----------------------------- */
const NAV = [
  { group: '运营', items: [
    { id: 'overview', label: '运营概览', icon: 'grid' },
    { id: 'products', label: '商品管理', icon: 'box' },
    { id: 'inventory', label: '库存与补货', icon: 'layers' },
    { id: 'orders', label: '订单与销售', icon: 'receipt' },
    { id: 'ads', label: '广告投放', icon: 'megaphone' },
  ]},
  { group: 'AI 链路', items: [
    { id: 'listing', label: '主图与卖点生成', icon: 'sparkles' },
    { id: 'weekly', label: '广告周报', icon: 'chart' },
  ]},
  { group: '系统', items: [
    { id: 'rag', label: '知识库检索', icon: 'search' },
    { id: 'runs', label: '运行档案', icon: 'archive' },
  ]},
];

function renderNav() {
  const cur = S.view;
  $('#nav').innerHTML = NAV.map((g) => `<div class="nav-group">
    <div class="nav-title">${esc(g.group)}</div>
    ${g.items.map((it) => `<a href="#/${it.id}" class="nav-item ${it.id === cur ? 'active' : ''}">
      ${icon(it.icon)}<span>${esc(it.label)}</span></a>`).join('')}
  </div>`).join('');
}

function renderFacts() {
  const c = (S.meta && S.meta.counts) || {};
  const k = (S.meta && S.meta.knowledge) || {};
  // 索引还在后台加载时显示"加载中…"而不是 0 —— 0 看起来像知识库坏了
  const chunks = k.ready ? num(k.chunks || 0) : '<span class="tiny faint">…</span>';
  $('#facts').innerHTML = `
    <div class="fact"><b>${num(c.products || 0)}</b>商品 SKU</div>
    <div class="fact"><b>${num(c.orders || 0)}</b>订单</div>
    <div class="fact"><b>${num(c.copies || 0)}</b>文案版本</div>
    <div class="fact"><b>${chunks}</b>知识片段</div>`;
}

/* 首屏不等向量库：/api/meta 立刻返回，索引在后台加载。
   这里轮询几次把"知识片段"补上，避免用户盯着一个 0。 */
function watchKnowledge(tries = 12) {
  const k = (S.meta && S.meta.knowledge) || {};
  if (k.ready) return;
  let n = 0;
  const tick = async () => {
    if (++n > tries) return;
    try {
      const meta = await api('/api/meta');
      S.meta = meta;
      renderFacts();
      if (meta.knowledge && meta.knowledge.ready) return;
    } catch (e) { /* 还没好就再等一轮 */ }
    setTimeout(tick, 1200);
  };
  setTimeout(tick, 1200);
}

function renderThemeBtn() {
  const dark = document.documentElement.dataset.theme === 'dark';
  $('#themeLabel').textContent = dark ? '切换浅色' : '切换深色';
  $('#themeIcon').innerHTML = `<use href="#i-${dark ? 'sun' : 'moon'}"/>`;
}

let renderSeq = 0;
async function render(opts = {}) {
  const view = V[S.view];
  if (!view) { location.hash = '#/overview'; return; }
  const seq = ++renderSeq;
  renderNav();
  $('#pageTitle').textContent = view.title;
  $('#pageSub').textContent = typeof view.sub === 'function' ? view.sub() : (view.sub || '');
  $('#pageActions').innerHTML = typeof view.actions === 'function' ? view.actions() : (view.actions || '');
  const box = $('#view');
  if (!box.dataset.ready) { box.innerHTML = loading(); box.dataset.ready = '1'; }

  try {
    const html = await view.render();
    if (seq !== renderSeq) return;
    box.innerHTML = `<div class="view-enter">${html}</div>`;
    if (view.bind) view.bind();
    if (opts.keepFocus) {
      const el = document.getElementById(opts.keepFocus);
      if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
    }
  } catch (e) {
    if (seq !== renderSeq) return;
    box.innerHTML = `<div class="card"><div class="empty">
      <div class="big">!</div><div>${esc(e.message)}</div>
      <div class="mt"><button class="btn btn-sm" onclick="location.reload()">刷新重试</button></div>
    </div></div>`;
  }
}

function bindGallery() {
  $$('.shot').forEach((f) => f.onclick = () => openLightbox(f.dataset.src, f.dataset.cap));
}

function boot() {
  const saved = localStorage.getItem('sv-theme');
  if (saved) document.documentElement.dataset.theme = saved;
  renderThemeBtn();

  $('#themeBtn').onclick = () => {
    const cur = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = cur;
    localStorage.setItem('sv-theme', cur);
    renderThemeBtn();
  };
  $('#lbClose').onclick = closeLightbox;
  $('#lightbox').onclick = (e) => { if (e.target.id === 'lightbox') closeLightbox(); };
  $('#drawerClose').onclick = closeDrawer;
  $('#drawerMask').onclick = closeDrawer;
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeLightbox(); closeDrawer(); }
  });
  document.addEventListener('click', (e) => {
    if (e.target.matches('[data-act="refresh"]')) render();
  });

  const route = () => {
    const id = (location.hash || '#/overview').replace('#/', '').split('?')[0] || 'overview';
    S.view = V[id] ? id : 'overview';
    render();
  };
  window.addEventListener('hashchange', route);

  api('/api/meta').then((m) => {
    S.meta = m;
    S.week = S.week || m.report_week;
    renderFacts();
    watchKnowledge();          // 索引在后台加载时，稍后把"知识片段"补上
    route();
  }).catch((e) => {
    $('#view').innerHTML = `<div class="card"><div class="empty">
      <div class="big">!</div><div>无法连接后端：${esc(e.message)}</div></div></div>`;
  });
}

boot();
