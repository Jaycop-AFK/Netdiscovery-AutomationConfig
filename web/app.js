'use strict';
/* NetScope front-end - vanilla JS, no build step. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const state = { devices: [], topology: null, activity: [] };

async function api(path, body, method) {
  const opt = { method: method || (body === undefined ? 'GET' : 'POST'), headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.login) { location.href = '/login'; throw new Error('login required'); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 8000 : 4000);
}

/** Start a background job and stream its log lines. Resolves with the final job view. */
async function runJob(start, onLine) {
  const { job } = await start;
  let since = 0;
  for (;;) {
    const j = await api(`/api/jobs/${job}?since=${since}`);
    j.lines.forEach((l) => onLine && onLine(l));
    since = j.next;
    if (j.status !== 'running') return j;
    await sleep(500);
  }
}
const appendLog = (el) => (line) => { el.textContent += line + '\n'; el.scrollTop = el.scrollHeight; };

async function refresh() {
  try {
    Object.assign(state, await api('/api/state'));
  } catch (e) { toast('เชื่อมต่อ backend ไม่ได้: ' + e.message, true); return; }
  renderAll();
}

/* ------------------------------------------------------------------ tabs */
$$('#tabs button').forEach((b) => b.addEventListener('click', () => {
  $$('#tabs button').forEach((x) => x.classList.toggle('on', x === b));
  $$('.view').forEach((v) => (v.hidden = v.id !== 'view-' + b.dataset.view));
  if (b.dataset.view === 'dash') renderTopology();
}));

function renderAll() {
  const nl = state.topology ? state.topology.links.length : 0;
  $('#chipDev').textContent = state.topology?.source === 'console_cdp'
    ? `${state.devices.length} ลงทะเบียน · ${state.topology.nodes.length} CDP nodes`
    : `${state.devices.length} อุปกรณ์`;
  $('#chipLink').textContent = `${nl} links`;
  document.body.classList.toggle('share', !!state.share); $('#demoBar').hidden = !state.demo; $('#chipDev').textContent += state.demo ? ' · DEMO' : '';
  renderDevices(); renderTopology(); renderActivity(); fillPingFrom(); renderSim();
}

function renderDevices() {
  for (const sel of ['#devList', '#devList2']) {
    const box = $(sel); box.innerHTML = '';
    if (!state.devices.length) box.innerHTML = '<div class="hint">ยังไม่มีอุปกรณ์ — ไปที่ Initial Config</div>';
    state.devices.forEach((d) => {
      const el = document.createElement('div'); el.className = 'dev';
      el.innerHTML = `<i class="dot ${d.status === 'online' ? 'ok' : 'bad'}"></i><div class="sp"><b>${esc(d.name)}</b><small>${esc(d.mgmt_ip)} · ${esc(d.kind)}${d.model ? ' · ' + esc(d.model) : ''}</small></div><button class="x" title="ลบ">✕</button>`;
      el.addEventListener('click', () => openDeviceByName(d.name));
      $('.x', el).addEventListener('click', async (ev) => {
        ev.stopPropagation();
        if (!confirm(`ลบ ${d.name} ออกจากรายการ? (ไม่ได้ลบ config บนอุปกรณ์)`)) return;
        await api('/api/devices/' + d.id, undefined, 'DELETE'); refresh();
      });
      box.appendChild(el);
    });
  }
}

function renderActivity() {
  $('#activity').innerHTML = state.activity.map((a) => `<div class="act ${esc(a.level)}"><time>${esc(a.t)}</time><div><b>${esc(a.title)}</b> <span class="muted">${esc(a.detail)}</span></div></div>`).join('') || '<div class="hint">ยังไม่มีกิจกรรม</div>';
}

/* ------------------------------------------------------------------ modal */
function openModal(title, { narrow = false } = {}) {
  const root = $('#modalRoot');
  const ov = document.createElement('div'); ov.className = 'overlay';
  ov.innerHTML = `<div class="modal ${narrow ? 'narrow' : ''}"><div class="modal-head"><h2>${esc(title)}</h2><button class="x-btn" aria-label="ปิด">×</button></div><div class="mbody"></div></div>`;
  const close = () => { ov.remove(); ov.onclose && ov.onclose(); };
  $('.x-btn', ov).onclick = close;
  ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
  root.appendChild(ov);
  return { el: $('.mbody', ov), close, overlay: ov };
}

/* ------------------------------------------------------------------ topology */
const SVGNS = 'http://www.w3.org/2000/svg';
const svgEl = (name, attrs = {}, parent) => { const e = document.createElementNS(SVGNS, name); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const shortIf = (n) => n.replace('GigabitEthernet', 'Gi').replace('FastEthernet', 'Fa').replace('TenGigabitEthernet', 'Te').replace('Ethernet', 'Et').replace('Loopback', 'Lo');

function layout(nodes, links) {
  const W = 900, H = 520;
  let saved = {}; try { saved = JSON.parse(localStorage.getItem('ns_pos') || '{}'); } catch (e) { /* ignore */ }
  const pos = {}, fixed = new Set();
  nodes.forEach((n, i) => {
    if (saved[n.id]) { pos[n.id] = { ...saved[n.id] }; fixed.add(n.id); } else {
      const a = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
      pos[n.id] = { x: W / 2 + Math.cos(a) * 230, y: H / 2 + Math.sin(a) * 170 };
    }
  });
  if (fixed.size < nodes.length) {
    const k = Math.sqrt((W * H) / Math.max(nodes.length, 1)) * 0.75;
    for (let it = 0; it < 300; it++) {
      const disp = {}; nodes.forEach((n) => (disp[n.id] = { x: 0, y: 0 }));
      for (const a of nodes) for (const b of nodes) {
        if (a === b) continue;
        let dx = pos[a.id].x - pos[b.id].x, dy = pos[a.id].y - pos[b.id].y; const d = Math.max(Math.hypot(dx, dy), 1);
        const f = (k * k) / d; disp[a.id].x += (dx / d) * f; disp[a.id].y += (dy / d) * f;
      }
      for (const l of links) {
        const a = pos[l.a.node], b = pos[l.b.node]; if (!a || !b) continue;
        const dx = a.x - b.x, dy = a.y - b.y, d = Math.max(Math.hypot(dx, dy), 1), f = (d * d) / k;
        disp[l.a.node].x -= (dx / d) * f; disp[l.a.node].y -= (dy / d) * f; disp[l.b.node].x += (dx / d) * f; disp[l.b.node].y += (dy / d) * f;
      }
      const t = 12 * (1 - it / 300);
      for (const n of nodes) {
        if (fixed.has(n.id)) continue;
        const d = disp[n.id], len = Math.max(Math.hypot(d.x, d.y), 1);
        pos[n.id].x = Math.min(W - 70, Math.max(70, pos[n.id].x + (d.x / len) * Math.min(len, t) + (W / 2 - pos[n.id].x) * 0.01));
        pos[n.id].y = Math.min(H - 60, Math.max(60, pos[n.id].y + (d.y / len) * Math.min(len, t) + (H / 2 - pos[n.id].y) * 0.01));
      }
    }
  }
  return pos;
}

function renderTopology() {
  const svg = $('#topo'); svg.innerHTML = '';
  const topo = state.topology;
  $('#topoEmpty').hidden = !!(topo && topo.nodes.length);
  $('#topoStamp').textContent = topo
    ? `${topo.source === 'console_cdp' ? 'อ่านจาก Console CDP · ' : ''}อัปเดตล่าสุด ${topo.collected_at}` : '';
  if (!topo || !topo.nodes.length) return;
  const pos = layout(topo.nodes, topo.links);
  const linkLayer = svgEl('g', {}, svg), nodeLayer = svgEl('g', {}, svg);
  // parallel-link offsets
  const pairCount = {}, pairIdx = {};
  topo.links.forEach((l) => { const k = [l.a.node, l.b.node].sort().join('|'); pairCount[k] = (pairCount[k] || 0) + 1; });
  const linkEls = topo.links.map((l) => {
    const k = [l.a.node, l.b.node].sort().join('|'); pairIdx[k] = (pairIdx[k] || 0) + 1;
    const g = svgEl('g', {}, linkLayer);
    const line = svgEl('line', { class: 'link ' + l.state }, g);
    svgEl('title', {}, line).textContent = `${l.a.node} ${l.a.port} ↔ ${l.b.node} ${l.b.port} (${l.state})`;
    const ta = svgEl('text', { class: 'plabel' }, g), tb = svgEl('text', { class: 'plabel' }, g);
    ta.textContent = shortIf(l.a.port); tb.textContent = shortIf(l.b.port);
    return { l, line, ta, tb, off: (pairIdx[k] - 1 - (pairCount[k] - 1) / 2) * 16 };
  });
  const updateLinks = () => linkEls.forEach(({ l, line, ta, tb, off }) => {
    const a = pos[l.a.node], b = pos[l.b.node]; if (!a || !b) return;
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(Math.hypot(dx, dy), 1), nx = -dy / d * off, ny = dx / d * off;
    line.setAttribute('x1', a.x + nx); line.setAttribute('y1', a.y + ny); line.setAttribute('x2', b.x + nx); line.setAttribute('y2', b.y + ny);
    const at = (dist) => [a.x + (dx / d) * dist + nx - (dy / d) * 9, a.y + (dy / d) * dist + ny + (dx / d) * 9 + 3];
    const [x1, y1] = at(Math.min(84, d * 0.38));
    const [x2, y2] = at(d - Math.min(84, d * 0.38));
    ta.setAttribute('x', x1); ta.setAttribute('y', y1); tb.setAttribute('x', x2); tb.setAttribute('y', y2);
  });
  topo.nodes.forEach((n) => {
    const g = svgEl('g', { class: 'node', transform: `translate(${pos[n.id].x},${pos[n.id].y})` }, nodeLayer);
    const ring = n.status === 'online' ? 'var(--ok)' : n.status === 'unmanaged' ? 'var(--warn)' : 'var(--bad)';
    const dash = n.status === 'unmanaged' ? '6 4' : 'none';
    if (n.kind === 'switch') svgEl('rect', { class: 'nbody', x: -32, y: -22, width: 64, height: 44, rx: 8, stroke: ring, 'stroke-dasharray': dash }, g);
    else svgEl('circle', { class: 'nbody', r: 28, stroke: ring, 'stroke-dasharray': dash }, g);
    const ic = n.kind === 'switch' ? 'M-18 -6h30m-6 -6l6 6l-6 6M18 6h-30m6 -6l-6 6l6 6' : 'M-14 0h28m-6 -7l6 7l-6 7M0 -14v28m-7 -6l7 -8l7 8';
    svgEl('path', { class: 'nicon', d: ic, transform: n.kind === 'switch' ? '' : 'scale(.75)' }, g);
    svgEl('text', { class: 'nlabel', y: 46 }, g).textContent = n.name;
    const up = n.interfaces.filter((i) => i.state === 'up').length;
    svgEl('text', { class: 'nsub', y: 60 }, g).textContent = (n.mgmt_ip || 'no IP') + (n.managed && n.interfaces.length ? ` · ${up}/${n.interfaces.length} up` : '');
    let moved = 0, drag = null;
    g.addEventListener('pointerdown', (e) => { g.setPointerCapture(e.pointerId); drag = { x: e.clientX, y: e.clientY }; moved = 0; });
    g.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const p = svg.createSVGPoint(); p.x = e.clientX; p.y = e.clientY; const q = p.matrixTransform(svg.getScreenCTM().inverse());
      moved += Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y); drag = { x: e.clientX, y: e.clientY };
      pos[n.id] = { x: Math.max(40, Math.min(860, q.x)), y: Math.max(40, Math.min(480, q.y)) };
      g.setAttribute('transform', `translate(${pos[n.id].x},${pos[n.id].y})`); updateLinks();
    });
    g.addEventListener('pointerup', () => {
      drag = null;
      if (moved > 5) { const all = JSON.parse(localStorage.getItem('ns_pos') || '{}'); all[n.id] = pos[n.id]; localStorage.setItem('ns_pos', JSON.stringify(all)); } else openDevice(n);
    });
  });
  updateLinks();
}

$('#btnDiscover').onclick = async () => {
  if (state.devices.length < 3 && !confirm(`มีเพียง ${state.devices.length} อุปกรณ์ (โจทย์ต้องการอย่างน้อย 3) — ดำเนินการต่อ?`)) return;
  const btn = $('#btnDiscover'), box = $('#discBox'), log = $('#discLog');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>กำลังค้นหา...'; box.hidden = false; log.textContent = '';
  try {
    const j = await runJob(api('/api/discovery/run', { crawl: $('#optCrawl').checked }), appendLog(log));
    if (j.status === 'error') toast('Discovery ล้มเหลว: ' + j.error, true); else toast(`พบ ${j.result.nodes.length} nodes, ${j.result.links.length} links`);
    localStorage.removeItem('ns_pos'); await refresh();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = '▶ Auto Discovery'; }
};

/* ------------------------------------------------------------------ device front panel */
function openDeviceByName(name) {
  const n = state.topology && state.topology.nodes.find((x) => x.name.toLowerCase() === name.toLowerCase());
  if (n) return openDevice(n);
  toast('ยังไม่มีข้อมูล interface — กด Auto Discovery ก่อน', true);
}

const isVirtual = (n) => /^(loopback|vlan|tunnel|null|port-channel)/i.test(n);

function frontPanelSvg(node, selected) {
  const phys = node.interfaces.filter((i) => !isVirtual(i.name));
  const isSw = node.kind === 'switch', perRow = isSw ? Math.ceil(phys.length / 2) : Math.min(8, Math.max(phys.length, 1));
  const rows = isSw ? 2 : Math.ceil(phys.length / perRow) || 1, PW = 46, PH = 34, GX = 14, X0 = 190, Y0 = 18, RH = 66;
  const W = X0 + perRow * (PW + GX) + 10, H = Y0 + rows * RH + 6;
  let s = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;min-width:${W}px;max-width:${Math.round(W * 1.6)}px;height:auto" role="img" aria-label="Front panel of ${esc(node.name)}">`;
  s += `<text x="14" y="30" fill="#e6edf5" font-size="18" font-weight="700">${esc(node.name)}</text>`;
  s += `<text x="14" y="50" fill="#8a99ad" font-size="11">${esc(node.model || (isSw ? 'Switch' : 'Router'))}</text>`;
  s += `<text x="14" y="68" fill="#8a99ad" font-size="11" font-family="monospace">${esc(node.mgmt_ip || '')}</text>`;
  s += `<line x1="170" y1="8" x2="170" y2="${H - 8}" stroke="#3a475a"/>`;
  phys.forEach((p, i) => {
    const col = isSw ? Math.floor(i / 2) : i % perRow, row = isSw ? i % 2 : Math.floor(i / perRow);
    const x = X0 + col * (PW + GX), y = Y0 + row * RH;
    s += `<g class="port ${p.state}${selected === p.name ? ' sel' : ''}" data-if="${esc(p.name)}" tabindex="0">`
      + `<title>${esc(p.name)} — ${p.status}/${p.protocol}${p.ip ? ' — ' + p.ip : ''}</title>`
      + `<rect class="shell" x="${x}" y="${y}" width="${PW}" height="${PH}" rx="4"/>`
      + `<rect x="${x + 9}" y="${y + 11}" width="${PW - 18}" height="${PH - 17}" rx="2" fill="#1c2430"/>`
      + `<path d="M${x + 13} ${y + 22}h${PW - 26}" stroke="#d9a441" stroke-dasharray="2 2" stroke-width="3"/>`
      + `<circle class="led" cx="${x + PW - 8}" cy="${y + 6}" r="3.4"/><circle class="led" cx="${x + 8}" cy="${y + 6}" r="3.4" opacity="${p.state === 'up' ? 1 : .25}"/>`
      + `<text x="${x + PW / 2}" y="${y + PH + 13}">${esc(p.short || shortIf(p.name))}</text>`
      + (p.mgmt ? `<text x="${x + PW / 2}" y="${y + PH + 24}" style="fill:#f59e0b;font-size:9px">MGMT</text>` : '')
      + `</g>`;
  });
  return s + '</svg>';
}

function openDevice(node) {
  const m = openModal(node.name);
  let selected = null, timer = null, traffic = null, trafficBusy = false, trafficTimer = null;
  const linked = state.devices.find((d) => d.id === node.device_id || d.name?.toLowerCase() === node.name?.toLowerCase() || d.mgmt_ip === node.mgmt_ip);
  // A cached console/CDP topology may predate the inventory link. Reattach
  // it in the UI so read-only actions remain available without rescanning.
  node = { ...node, device_id: node.device_id || linked?.id, managed: !!(node.managed || linked) };
  const draw = () => {
    const raw = (state.topology.nodes.find((x) => x.id === node.id)) || node;
    const n = { ...raw, device_id: raw.device_id || node.device_id, managed: !!(raw.managed || node.managed) };
    const phys = n.interfaces.filter((i) => !isVirtual(i.name)), virt = n.interfaces.filter((i) => isVirtual(i.name));
    const up = n.interfaces.filter((i) => i.state === 'up').length;
    m.el.innerHTML = `
      <div class="row gap" style="flex-wrap:wrap;margin-bottom:10px">
        <span class="chip">${n.kind === 'switch' ? 'Switch' : 'Router'}</span>
        <span class="chip">${esc(n.mgmt_ip || 'no IP')}</span>
        <span class="chip">${n.status}</span>${n.version ? `<span class="chip">IOS ${esc(n.version)}</span>` : ''}
        <span class="chip">${up}/${n.interfaces.length} interfaces up</span>
        <span class="sp" style="flex:1"></span>
        ${n.managed ? '<button class="btn small" id="dvRefresh">⟳ Refresh</button><button class="btn small" id="dvTraffic">ดู Traffic</button><button class="btn small" id="dvPing">Ping จากอุปกรณ์นี้</button>' : ''}
      </div>
      ${n.managed ? '' : '<p class="hint">อุปกรณ์นี้ยังไม่ได้ลงทะเบียน/login ไม่ได้ จึงเห็นเฉพาะ port ที่ต่ออยู่ (จาก CDP/LLDP) — เพิ่มอุปกรณ์ที่ Initial Config เพื่อดู port ทั้งหมด</p>'}
      <div class="chassis">${n.interfaces.length ? frontPanelSvg(n, selected) : '<p class="hint">ยังไม่มีข้อมูล interface</p>'}</div>
      <div class="legend"><span><i class="dot ok"></i>up/up</span><span><i class="dot bad"></i>down (ไม่มีสายหรือปลายทางปิด)</span><span><i class="dot" style="background:var(--off)"></i>administratively down</span></div>
      ${virt.length ? '<div style="margin-top:8px">' + virt.map((v) => `<span class="vif ${v.state}" data-if="${esc(v.name)}"><i></i>${esc(v.name)} ${v.ip ? esc(v.ip) : ''}</span>`).join('') + '</div>' : ''}
      <div id="portDetail"></div>
        ${traffic ? `<div class="plan traffic-panel"><div class="row gap" style="align-items:center"><h3 style="margin:0">Traffic Realtime</h3><span class="muted">อัปเดตทุก 3 วินาที · ${new Date(traffic.collected_at * 1000).toLocaleTimeString()}</span></div>
        <table class="t" style="margin-top:8px"><thead><tr><th>Interface</th><th>Input rate</th><th>Output rate</th><th>Packets RX/TX</th><th>Errors RX/TX</th></tr></thead><tbody>
        ${traffic.interfaces.map((i) => `<tr><td>${esc(i.short || i.name)}</td><td>${trafficRate(i.input_rate_bps)}</td><td>${trafficRate(i.output_rate_bps)}</td><td>${trafficCount(i.input_packets)} / ${trafficCount(i.output_packets)}</td><td>${i.input_errors} / ${i.output_errors}</td></tr>`).join('')}</tbody></table></div>` : ''}
      <table class="t" style="margin-top:12px"><thead><tr><th>Interface</th><th>IP</th><th>Status</th><th>Protocol</th><th>Neighbor</th></tr></thead><tbody>
      ${n.interfaces.map((i) => `<tr data-if="${esc(i.name)}" class="${selected === i.name ? 'sel' : ''}" style="cursor:pointer"><td>${esc(i.name)}</td><td>${i.ip ? esc(i.ip) + (i.prefix ? '/' + i.prefix : '') : '—'}</td><td><span class="st ${i.state}">${esc(i.status)}</span></td><td>${esc(i.protocol)}</td><td>${i.neighbor ? esc(i.neighbor.node) + ' ' + esc(shortIf(i.neighbor.port)) : '—'}</td></tr>`).join('')}
      </tbody></table>`;
    $$('[data-if]', m.el).forEach((el) => el.addEventListener('click', () => { selected = el.dataset.if; draw(); }));
    if (selected) portDetail(n);
    const rb = $('#dvRefresh', m.el); if (rb) rb.onclick = () => doRefresh(true);
    const tb = $('#dvTraffic', m.el); if (tb) tb.onclick = () => loadTraffic();
    const pb = $('#dvPing', m.el); if (pb) pb.onclick = () => { m.close(); gotoPing(n.name); };
  };
  const trafficRate = (bps) => { const n = Number(bps || 0); if (n >= 1000000) return (n / 1000000).toFixed(2) + ' Mbps'; if (n >= 1000) return (n / 1000).toFixed(1) + ' Kbps'; return n + ' bps'; };
  const trafficCount = (n) => Number(n || 0).toLocaleString();
  const loadTraffic = async () => {
    if (trafficBusy || !node.device_id) return;
    trafficBusy = true;
    const b = $('#dvTraffic', m.el); if (b) { b.disabled = true; b.innerHTML = '<span class="spin"></span>กำลังอ่าน'; }
    try {
      traffic = await api(`/api/devices/${node.device_id}/traffic`, {});
      draw();
      if (!trafficTimer) trafficTimer = setInterval(loadTraffic, 3000);
    }
    catch (e) { toast('อ่าน Traffic ไม่สำเร็จ: ' + e.message, true); }
    finally { trafficBusy = false; }
  };
  const portDetail = (n) => {
    const p = n.interfaces.find((i) => i.name === selected); if (!p) return;
    const box = $('#portDetail', m.el);
    box.innerHTML = `<div class="plan"><h3>${esc(p.name)} <span class="st ${p.state}">${esc(p.status)} / ${esc(p.protocol)}</span>${p.mgmt ? ' <span class="tag" style="background:var(--warn)">MGMT</span>' : ''}</h3>
      <div class="muted">IP: ${p.ip ? esc(p.ip) + (p.prefix ? '/' + p.prefix : '') : 'ไม่ได้กำหนด'} · เชื่อมกับ: ${p.neighbor ? esc(p.neighbor.node) + ' ' + esc(p.neighbor.port) : 'ไม่พบ neighbor'}</div>
      ${n.managed ? `<div class="row gap" style="margin-top:8px"><button class="btn small" data-act="noshut">no shutdown</button><button class="btn small danger" data-act="shut">shutdown</button></div>` : ''}</div>`;
    $$('[data-act]', box).forEach((b) => b.onclick = () => {
      const shut = b.dataset.act === 'shut';
      showPlan({ plans: [{ device_id: n.device_id, device: n.name, kind: 'config', commands: [`interface ${p.name}`, shut ? ' shutdown' : ' no shutdown', 'exit'],
        explain: [(shut ? 'shutdown ' : 'no shutdown ') + p.name + (p.mgmt && shut ? ' — คำเตือน: นี่คือ port management ถ้าปิดจะเชื่อมต่อ SSH ไม่ได้' : '')] }], problems: [] }, 'ยืนยันการเปลี่ยนสถานะ port', () => doRefresh(false));
    });
  };
  const doRefresh = async (manual) => {
    if (!node.device_id) return;
    try { const r = await api(`/api/devices/${node.device_id}/refresh`, {}); state.topology = r.topology; draw(); renderTopology(); }
    catch (e) { if (manual) toast('Refresh ล้มเหลว: ' + e.message, true); }
  };
  draw();
  if (node.managed) timer = setInterval(() => { if (document.body.contains(m.el)) doRefresh(false); else clearInterval(timer); }, 10000);
  m.overlay.onclose = () => { clearInterval(timer); clearInterval(trafficTimer); };
}

/* ------------------------------------------------------------------ prompt -> plan -> confirm -> apply */
const EXAMPLES = [
  'เปิดทุกพอร์ตของทุกเครื่อง แล้วตั้ง IP /30 ให้ทุกลิงก์จาก 10.10.0.0/16',
  'ทำให้ทุกเครื่อง ping หากันได้ด้วย OSPF area 0',
  'ตั้ง RIP v2 ให้ทุกเครื่อง',
  'ตั้ง EIGRP 100 ให้ R1 กับ R2',
  'ตั้ง static route ไปยัง 192.168.2.0/24 ผ่าน 10.10.0.2 บน R1',
  'ปิดพอร์ต Gi0/3 ของ R3',
  'ping จาก R1 ไปหาทุกเครื่อง',
  'set ip 10.0.0.1/24 on g0/1 of R1',
];
EXAMPLES.forEach((t) => { const b = document.createElement('button'); b.textContent = t; b.onclick = () => { const a = $('#promptText'); a.value = t; a.focus(); }; $('#examples').appendChild(b); });

$('#btnPreview').onclick = async () => {
  const text = $('#promptText').value.trim();
  if (!text) return toast('พิมพ์สิ่งที่ต้องการก่อน', true);
  const rulesOnly = !!window.__rulesOnly; window.__rulesOnly = false;
  const btn = $('#btnPreview'), label = btn.textContent;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>' + (aiPrimary ? 'AI กำลังวางแผน...' : 'กำลังแปลคำสั่ง...');
  try {
    const plan = await api('/api/prompt/parse', { text, rules: rulesOnly });
    plan.goal = text;
    if (!plan.plans.length && !plan.problems.length) return toast('ไม่พบคำสั่งที่แปลได้', true);
    showPlan(plan, plan.source === 'ai' ? '🤖 แผนที่ AI เตรียมไว้ — ตรวจสอบก่อนยืนยัน' : 'ตรวจสอบคำสั่งก่อนส่ง');
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = label; }
};

function resultCards(results) {
  return results.map((r) => `<div class="plan"><h3>${esc(r.device)} <span class="${r.ok ? 'res-ok' : 'res-bad'}">${r.ok ? 'OK' : 'ERROR'}</span></h3>${(r.errors || []).map((e) => `<div class="problem">${esc(e)}</div>`).join('')}${r.hint ? `<div class="problem" style="border-color:var(--warn)">💡 ${esc(r.hint)}</div>` : ''}${r.output ? `<pre class="cmds">${esc(r.output)}</pre>` : ''}</div>`).join('');
}

function showPlan(plan, title, after) {
  const m = openModal(title);
  const problems = plan.problems.map((p) => `<div class="problem">⚠ ${esc(p)}</div>`).join('');
  const hasCfg = plan.plans.some((p) => p.kind !== 'exec');
  const aiNote = plan.plans.some((p) => p.ai)
    ? `<div class="problem" style="border-color:#a855f7;background:#a855f722">${plan.ai_summary ? '<b>📋 แผนของ AI:</b> ' + esc(plan.ai_summary) + '<br>' : ''}🤖 คำสั่งสร้างโดย AI — ตรวจสอบให้ละเอียดก่อนยืนยัน (${plan.plans.length} รายการ, ${plan.plans.filter((p) => p.kind !== 'exec').length} อุปกรณ์ที่จะถูกแก้ config)</div>` : '';
  const autoOffer = aiPrimary && hasCfg;
  m.el.innerHTML = `${aiNote}${problems}
    ${plan.plans.map((p, i) => `<div class="plan" data-i="${i}"><h3>${esc(p.device)} <span class="tag ${p.kind === 'exec' ? 'exec' : ''}">${p.kind === 'exec' ? 'show/ping (ตรวจสอบ)' : 'config'}</span>${p.ai ? ' <span class="tag ai">AI</span>' : ''}</h3>
      <ul>${p.explain.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>
      ${(p.warnings || []).map((w) => `<div class="problem">⚠ ${esc(w)}</div>`).join('')}
      <textarea rows="${Math.min(14, p.commands.length + 1)}" spellcheck="false">${esc(p.commands.join('\n'))}</textarea></div>`).join('')}
    ${plan.plans.length ? `<p class="hint">แก้ไขคำสั่งในกล่องได้ก่อนยืนยัน — ยังไม่มีอะไรถูกส่งไปยังอุปกรณ์จนกว่าจะกดยืนยัน</p>
    ${autoOffer ? `<label class="check block" style="color:var(--text)"><input type="checkbox" id="optAuto" ${plan.source === 'ai' ? 'checked' : ''}> 🔁 ถ้ามี error หรือ ping ไม่ผ่าน ให้ AI แก้แล้วลองใหม่อัตโนมัติ (สูงสุด 3 รอบ — คำสั่งอันตรายยังถูกกรอง และจะเห็นทุกรอบ)</label>` : ''}
    <div class="row gap end">${hasCfg ? '<label class="check" style="margin-right:auto"><input type="checkbox" id="optSave" checked> บันทึก config (write memory)</label>' : ''}
    <button class="btn" id="pCancel">ยกเลิก</button><button class="btn primary" id="pOk">✔ ยืนยันและส่งไปยังอุปกรณ์</button></div>` : '<div class="row end"><button class="btn" id="pCancel">ปิด</button></div>'}`;
  $('#pCancel', m.el).onclick = m.close;
  const ok = $('#pOk', m.el); if (!ok) return;
  ok.onclick = async () => {
    const plans = plan.plans.map((p, i) => ({ ...p, commands: $$('textarea', m.el)[i].value.split('\n').map((x) => x.replace(/\s+$/, '')).filter((x) => x.trim()) }));
    const save = $('#optSave', m.el) ? $('#optSave', m.el).checked : true;
    const auto = !!($('#optAuto', m.el) && $('#optAuto', m.el).checked);
    runApply(m, plan, { plans, approved: true, save, auto_fix: auto, goal: plan.goal || '' }, after);
  };
}

/** Run an apply / auto-fix job inside modal m and render the outcome (all rounds). */
async function runApply(m, plan, body, after) {
  m.el.innerHTML = `<h3><span class="spin"></span>${body.auto_fix ? 'กำลังส่งคำสั่ง ตรวจสอบ และให้ AI แก้ไขอัตโนมัติ...' : 'กำลังส่งคำสั่งไปยังอุปกรณ์...'}</h3><pre class="logbox" id="applyLog" style="max-height:420px"></pre><div id="applyRes"></div>`;
  try {
    const j = await runJob(api('/api/config/apply', body), appendLog($('#applyLog', m.el)));
    const res = j.result || {};
    const rounds = res.rounds || [{ n: 1, summary: '', plans: body.plans, results: res.results || [], ok: !!res.ok }];
    const last = rounds[rounds.length - 1], results = last.results || [];
    const allOk = j.status !== 'error' && last.ok;
    const connFail = results.some((r) => r.conn);
    $('h3', m.el).innerHTML = j.status === 'error' ? '<span class="res-bad">ผิดพลาด</span>'
      : allOk ? `<span class="res-ok">✔ สำเร็จ${rounds.length > 1 ? ` (หลัง AI แก้ ${rounds.length - 1} รอบ)` : ''}</span>`
      : connFail ? '<span class="res-bad">✘ ต่ออุปกรณ์ไม่ได้</span>' : '<span class="res-bad">✘ ยังไม่ผ่าน — บางคำสั่งมีข้อผิดพลาด</span>';

    let extra = '';
    if (connFail) {
      extra = `<div class="plan" style="border-color:var(--warn)"><h3>💡 นี่เป็นปัญหาการเชื่อมต่อ ไม่ใช่คำสั่งผิด — AI แก้ให้ไม่ได้</h3>
        <ul class="muted"><li>เครื่องที่รันโปรแกรมเข้าถึง IP management ของอุปกรณ์ได้หรือไม่ (ping / EVE-NG Cloud)</li><li>อุปกรณ์ยังเปิดอยู่และ SSH ใช้ได้ (ลอง "ทดสอบ console" ที่หน้า Initial Config)</li><li>ถ้าเป็นโหมดจำลอง: โปรแกรมอาจถูกรีสตาร์ท — กด 🧪 Demo เพื่อเริ่มใหม่</li></ul></div>`;
    } else if (!allOk && aiPrimary && j.status !== 'error') {
      extra = `<div class="plan" style="border-color:#a855f7"><h3>🤖 ให้ AI แก้ไข</h3>
        <div class="muted">ส่งโจทย์เดิม + คำสั่งที่ส่งไป + error จากอุปกรณ์ + สถานะล่าสุด ให้ AI วางแผนแก้ (คำสั่งที่สำเร็จไปแล้วไม่ถูก rollback)</div>
        <input id="fixHint" placeholder="บอก AI เพิ่มเติม (ไม่บังคับ) เช่น ไม่ต้องใช้ BGP / ใช้ OSPF แทน" style="margin-top:8px">
        <div class="row gap" style="margin-top:8px"><button class="btn" id="fixBtn">แก้ → ดู Preview ก่อนส่ง</button><button class="btn primary" id="fixAuto">🔁 แก้และทดสอบอัตโนมัติ (สูงสุด 3 รอบ)</button></div></div>`;
    }
    const roundsHtml = rounds.length > 1 ? rounds.map((r) => `<details ${r === last ? 'open' : ''} class="plan"><summary><b>รอบที่ ${r.n}</b> ${r.summary ? '— ' + esc(r.summary) : ''} <span class="${r.ok ? 'res-ok' : 'res-bad'}">${r.ok ? 'ผ่าน' : 'ไม่ผ่าน'}</span></summary>
        <div class="muted" style="margin:6px 0">${(r.plans || []).map((p) => esc(p.device) + ' (' + p.commands.length + ' คำสั่ง)').join(', ')}</div>${resultCards(r.results || [])}</details>`).join('') : resultCards(results);
    $('#applyRes', m.el).innerHTML = extra + roundsHtml;
    $('#applyLog', m.el).remove();

    const attempts = () => (last.plans || body.plans).map((p, i) => ({ device: p.device, kind: p.kind, commands: p.commands, ok: !!(results[i] && results[i].ok),
      errors: (results[i] && results[i].errors) || [], output: (results[i] && results[i].output) || '' }));
    const fb = $('#fixBtn', m.el), fa = $('#fixAuto', m.el);
    if (fb) fb.onclick = async () => {
      fb.disabled = true; fb.innerHTML = '<span class="spin"></span>AI กำลังวิเคราะห์ error...';
      try {
        const np = await api('/api/prompt/fix', { goal: plan.goal || '', attempts: attempts(), hint: $('#fixHint', m.el).value });
        np.goal = plan.goal;
        m.close();
        if (!np.plans.length && !np.problems.length) return toast('AI ไม่ได้เสนอแผนแก้ไข', true);
        showPlan(np, '🤖 แผนแก้ไขจาก AI — ตรวจสอบก่อนยืนยัน', after);
      } catch (e) { toast(e.message, true); fb.disabled = false; fb.textContent = 'แก้ → ดู Preview ก่อนส่ง'; }
    };
    if (fa) fa.onclick = () => runApply(m, plan, { plans: last.plans || body.plans, approved: true, save: body.save, auto_fix: true, goal: plan.goal || '',
      hint: $('#fixHint', m.el).value, prior_results: results }, after);
    await refresh(); if (after) after();
  } catch (e) { toast(e.message, true); m.close(); }
}

/* ------------------------------------------------------------------ add existing device */
$('#btnAddDev').onclick = () => {
  const m = openModal('เพิ่มอุปกรณ์ที่ตั้ง SSH ไว้แล้ว', { narrow: true });
  m.el.innerHTML = `<div class="form"><label>ชื่อ<input id="a_name" placeholder="R1"></label><label>Management IP<input id="a_ip"></label>
    <label>Username<input id="a_user" value="admin"></label><label>Password<input id="a_pass" type="password"></label>
    <label>Enable secret (ถ้าต่าง)<input id="a_en" type="password"></label><label>Protocol<select id="a_proto"><option value="ssh">SSH (22)</option><option value="telnet">Telnet vty (23)</option></select></label></div>
    <div class="row gap end"><button class="btn" id="a_cancel">ยกเลิก</button><button class="btn primary" id="a_ok">ทดสอบและเพิ่ม</button></div>`;
  $('#a_cancel', m.el).onclick = m.close;
  $('#a_ok', m.el).onclick = async () => {
    const b = $('#a_ok', m.el); b.disabled = true; b.innerHTML = '<span class="spin"></span>กำลัง login...';
    try {
      await api('/api/devices', { name: $('#a_name').value, mgmt_ip: $('#a_ip').value, username: $('#a_user').value, password: $('#a_pass').value, enable_secret: $('#a_en').value, protocol: $('#a_proto').value });
      toast('เพิ่มอุปกรณ์แล้ว'); m.close(); refresh();
    } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = 'ทดสอบและเพิ่ม'; }
  };
};

/* ------------------------------------------------------------------ initial config */
let conType = 'telnet';
$$('#conType button').forEach((b) => b.onclick = () => {
  conType = b.dataset.t; $$('#conType button').forEach((x) => x.classList.toggle('on', x === b));
  $('#conTelnet').hidden = conType !== 'telnet'; $('#conSerial').hidden = conType !== 'serial';
  if (conType === 'serial') {
    scanSerial();
    if ($('#f_mgmt_ip').value.trim().startsWith('127.')) {
      $('#f_mgmt_ip').value = '';
      $('#f_mgmt_prefix').value = '24';
      toast('ล้าง Management IP 127.x ของ Demo แล้ว — ใส่ IP เครือข่ายจริง เช่น 192.168.50.11/24');
    }
  }
});

async function scanSerial() {
  const hint = $('#serHint'), list = $('#serList');
  hint.textContent = 'กำลังสแกน COM port...';
  try {
    const { ports } = await api('/api/serial/ports');
    list.innerHTML = ports.map((p) => `<option value="${esc(p.port)}">${esc(p.description)}</option>`).join('');
    if (!ports.length) { hint.textContent = 'ไม่พบ COM port — เสียบสาย console (USB) แล้วกด ⟳ (ถ้าไม่ขึ้น ให้ติดตั้งไดรเวอร์ FTDI/Prolific/CP210x)'; return; }
    const best = ports.find((p) => p.likely_console) || (ports.length === 1 ? ports[0] : null);
    if (best && !$('#serPort').value) $('#serPort').value = best.port;
    hint.textContent = 'พบ: ' + ports.map((p) => `${p.port} (${p.description || '?'})${p.likely_console ? ' ★' : ''}`).join(' · ');
  } catch (e) { hint.textContent = 'สแกนไม่สำเร็จ: ' + e.message; }
}
$('#btnSerScan').onclick = scanSerial;

function fillInterfaces(interfaces) {
  const sel = $('#f_mgmt_if'), previous = sel.value;
  const usable = (interfaces || []).filter((p) => !/^(Null|NVI)/i.test(p.name));
  if (!usable.length) return;
  sel.innerHTML = usable.map((p) => {
    const addr = p.ip ? p.ip + (p.prefix ? '/' + p.prefix : '') : 'ว่าง (ยังไม่มี IP)';
    const recommended = !p.ip && /^(GigabitEthernet|FastEthernet|Ethernet|Vlan)/i.test(p.name);
    return `<option value="${esc(p.name)}">${recommended ? '★ ' : ''}${esc(p.name)} — ${esc(addr)} · ${esc(p.status)}/${esc(p.protocol)}</option>`;
  }).join('');
  if (usable.some((p) => p.name === previous)) sel.value = previous;
  else {
    const best = usable.find((p) => !p.ip && /^(GigabitEthernet|FastEthernet|Ethernet)/i.test(p.name)) || usable.find((p) => !p.ip) || usable[0];
    sel.value = best.name;
  }
}

$('#btnProbe').onclick = async () => {
  const { console: con } = initPayload();
  if (con.type === 'telnet' && (!con.host || !con.port)) return toast('ระบุ Host และ Port ของ console', true);
  if (con.type === 'serial' && !con.port) return toast('ระบุ COM port', true);
  const btn = $('#btnProbe'), log = $('#initLog'); $('#initHint').hidden = true;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>กำลังต่อ console...';
  try {
    const r = await api('/api/console/probe', { console: con });
    if (!r.ok) { log.textContent = '✘ ต่อ console ไม่สำเร็จ: ' + r.error; toast(r.error, true); return; }
    const lines = [`✔ ต่อ console ได้ — prompt: ${r.prompt}`];
    if (r.model) lines.push(`รุ่น: ${r.model}  IOS ${r.version || '?'}  (${r.kind})`);
    lines.push(r.factory ? 'อุปกรณ์ยังเป็นค่าโรงงาน พร้อม Initial Config' : 'อุปกรณ์มี hostname แล้ว — ถ้าส่ง Initial Config จะเขียนทับ');
    if (r.kind) $('#f_device_type').value = r.kind;
    if (r.interfaces && r.interfaces.length) {
      fillInterfaces(r.interfaces);
      lines.push('พอร์ตที่ตรวจพบ:');
      r.interfaces.forEach((p) => lines.push(`  ${p.name}: ${p.ip || 'ว่าง'} · ${p.status}/${p.protocol}`));
    } else lines.push('ไม่พบรายการ interface จาก show ip interface brief');
    log.textContent = lines.join('\n');
    if (r.hostname && !$('#f_hostname').value) $('#f_hostname').value = r.hostname;
    if (r.kind === 'switch') {
      const svi = [...$('#f_mgmt_if').options].find((o) => /^Vlan1$/i.test(o.value));
      if (svi) $('#f_mgmt_if').value = svi.value;
    }
    toast('ต่อ console สำเร็จ');
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = 'ทดสอบ console'; }
};

$('#btnClearTopology').onclick = async () => {
  if (!state.topology) return toast('ไม่มี topology ให้ล้าง');
  if (!confirm('ล้าง topology ที่ค้างอยู่จาก Dashboard?\nจะไม่ลบ config หรืออุปกรณ์จริง')) return;
  try {
    await api('/api/topology', undefined, 'DELETE');
    localStorage.removeItem('ns_pos');
    await refresh();
    toast('ล้าง topology แล้ว — อุปกรณ์จริงยังอยู่ครบ');
  } catch (e) { toast(e.message, true); }
};
$('#btnIfScan').onclick = () => $('#btnProbe').click();
$('#btnConsoleCdp').onclick = async () => {
  const { console: con } = initPayload();
  if (con.type === 'telnet' && (!con.host || !con.port)) return toast('เลือก Node จาก EVE-NG ก่อน', true);
  const btn = $('#btnConsoleCdp'); btn.disabled = true; btn.textContent = 'กำลังอ่าน CDP...';
  try {
    const r = await api('/api/discovery/console', { console: con, name: val('f_hostname'), lab: val('eveLab') });
    $('#initLog').textContent = `${r.name}: พบ CDP neighbor ${r.neighbors} รายการ\nTopology: ${r.topology.nodes.length} nodes, ${r.topology.links.length} links\n${r.neighbors ? 'สแกน Console ของ Node อื่นใน Lab เดียวกันเพื่อเติมสายให้ครบ' : 'ยังไม่พบเพื่อนบ้าน CDP: ตรวจว่าสาย EVE-NG ต่อระหว่างอุปกรณ์โดยตรง, เปิดพอร์ต และเปิด CDP (Cloud0/Net ไม่ใช่ CDP neighbor)'}`;
    await refresh();
    toast(`อัปเดต topology จาก CDP: ${r.topology.links.length} links`);
  } catch (e) { $('#initLog').textContent = 'อ่าน CDP ไม่สำเร็จ: ' + e.message; toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = 'อ่าน CDP → สร้างสาย'; }
};
const val = (id) => $('#' + id).value.trim();
function initPayload() {
  const console_ = conType === 'telnet' ? { type: 'telnet', host: val('conHost'), port: Number(val('conPort')) } : { type: 'serial', port: val('serPort'), baud: Number(val('serBaud')) };
  if (val('conUser')) console_.username = val('conUser');
  if ($('#conPass').value) console_.password = $('#conPass').value;
  if ($('#conEnable').value) console_.enable = $('#conEnable').value;
  const params = {};
  ['hostname', 'device_type', 'domain', 'rsa_bits', 'username', 'mgmt_if', 'mgmt_ip', 'mgmt_prefix', 'gateway', 'vty_transport'].forEach((k) => (params[k] = val('f_' + k)));
  params.password = $('#f_password').value; params.enable_secret = $('#f_enable_secret').value; params.extra = $('#f_extra').value;
  params.bring_up_all = $('#f_bring_up_all').checked;
  params.save_config = $('#f_save_config').checked;
  return { console: console_, params };
}

async function initConfirm(confirmable) {
  const { console: con, params } = initPayload();
  if (con.type === 'telnet' && (!con.host || !con.port)) return toast('ระบุ Host และ Port ของ console', true);
  if (con.type === 'serial' && !con.port) return toast('ระบุ COM port', true);
  let cmds;
  try { cmds = (await api('/api/initconfig/preview', params)).commands; } catch (e) { return toast(e.message, true); }
  const where = con.type === 'telnet' ? `telnet ${con.host}:${con.port}` : `${con.port} @ ${con.baud}`;
  const m = openModal('ตรวจสอบ Initial Config', { narrow: true });
  m.el.innerHTML = `<p>จะเชื่อมต่อ console <b>${esc(where)}</b> และส่งคำสั่งต่อไปนี้ไปที่อุปกรณ์:</p>
    <pre class="cmds">${esc(cmds.join('\n'))}${params.bring_up_all ? '\n! + interface <ทุกพอร์ตกายภาพ> / no shutdown   (ตรวจพบจากอุปกรณ์ตอนส่ง)' : ''}${params.save_config ? '\nwrite memory' : '\n! ไม่บันทึก — config จะอยู่ใน running-config เท่านั้น'}</pre>
    <p class="hint">หลังส่งเสร็จ ระบบจะลอง SSH เข้า ${esc(params.mgmt_ip)} ด้วย user ${esc(params.username)} เพื่อยืนยัน และลงทะเบียนอุปกรณ์อัตโนมัติ</p>
    <div class="row gap end"><button class="btn" id="icCancel">ปิด</button>${confirmable ? '<button class="btn primary" id="icOk">✔ ยืนยันและส่ง</button>' : ''}</div>`;
  $('#icCancel', m.el).onclick = m.close;
  if (!confirmable) return;
  $('#icOk', m.el).onclick = async () => {
    m.close();
    const log = $('#initLog'); log.textContent = ''; $('#initHint').hidden = true;
    const btn = $('#btnInitApply'); btn.disabled = true; btn.innerHTML = '<span class="spin"></span>กำลังตั้งค่า...';
    try {
      const j = await runJob(api('/api/initconfig/apply', { console: con, params, approved: true }), appendLog(log));
      if (j.status === 'error') toast('Initial config ล้มเหลว: ' + j.error, true);
      else if (j.result.ok) toast(`${params.hostname} พร้อมใช้งาน (SSH verified)`);
      else toast(`${params.hostname} ตั้งค่าแล้ว แต่ยังตรวจสอบ SSH ไม่ผ่าน — ดู Log`, true);
      await refresh();
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = 'ส่งค่า Initial Config →'; }
  };
}
$('#btnInitPreview').onclick = () => initConfirm(false);
$('#btnInitApply').onclick = () => initConfirm(true);

$('#btnEve').onclick = async (ev) => {
  ev.preventDefault();
  const box = $('#eveNodes'); box.innerHTML = '<span class="spin"></span>กำลังเชื่อมต่อ EVE-NG...';
  try {
    const r = await api('/api/eve/nodes', { host: val('eveHost'), lab: val('eveLab'), username: val('eveUser'), password: $('#evePass').value, https: $('#eveHttps').checked });
    box.innerHTML = `<table class="t"><thead><tr><th>Node</th><th>Console</th><th>Status</th><th></th></tr></thead><tbody>${r.nodes.map((n, i) => `<tr><td>${esc(n.name)}</td><td>${esc(n.console_host)}:${esc(n.console_port)}</td><td>${n.running ? 'running' : 'stopped'}</td><td><button class="btn small" data-i="${i}">ใช้</button></td></tr>`).join('')}</tbody></table>`;
    $$('button[data-i]', box).forEach((b) => b.onclick = (e2) => {
      e2.preventDefault(); const n = r.nodes[b.dataset.i];
      $('#conHost').value = n.console_host; $('#conPort').value = n.console_port; $('#f_hostname').value = n.name;
      // EVE lab naming convention: SW*/Switch nodes are normally L2 devices.
      // Keep Router as the safe default for all other node names.
      if (/^(sw|switch)/i.test(n.name || '')) $('#f_kind').value = 'switch';
      toast(`ใช้ console ของ ${n.name} (${n.console_host}:${n.console_port})`);
    });
  } catch (e) { box.innerHTML = ''; toast(e.message, true); }
};

/* ------------------------------------------------------------------ ping tests */
function fillPingFrom() {
  const sel = $('#pingFrom'), cur = sel.value;
  sel.innerHTML = state.devices.map((d) => `<option>${esc(d.name)}</option>`).join('');
  if (cur) sel.value = cur;
}
function gotoPing(name) { $('#tabs [data-view=ping]').click(); $('#pingFrom').value = name; $('#pingTarget').focus(); }

$('#btnPing').onclick = async () => {
  const dev = state.devices.find((d) => d.name === $('#pingFrom').value);
  if (!dev) return toast('เลือกอุปกรณ์ต้นทาง', true);
  if (!val('pingTarget')) return toast('ระบุปลายทาง', true);
  const log = $('#pingLog'); log.textContent = ''; $('#pingResult').innerHTML = '';
  const btn = $('#btnPing'); btn.disabled = true;
  try {
    const j = await runJob(api('/api/ping', { device_id: dev.id, target: val('pingTarget'), count: Number(val('pingCount')) || 5, source: val('pingSource') }), appendLog(log));
    if (j.status === 'error') return toast(j.error, true);
    const r = j.result;
    $('#pingResult').innerHTML = `<div class="plan"><h3 class="${r.ok ? 'res-ok' : 'res-bad'}">${r.ok ? '✔ Ping สำเร็จ' : '✘ Ping ไม่ผ่าน'} — ${r.from} → ${esc(r.target)} : ${r.percent}% (${r.received}/${r.sent})${r.avg_ms != null ? ' · avg ' + r.avg_ms + ' ms' : ''}</h3><pre class="cmds">${esc(r.raw)}</pre></div>`;
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
};

$('#btnMatrix').onclick = async () => {
  const log = $('#matrixLog'); log.textContent = ''; $('#matrix').innerHTML = '';
  const btn = $('#btnMatrix'); btn.disabled = true; btn.innerHTML = '<span class="spin"></span>กำลังทดสอบ...';
  try {
    const j = await runJob(api('/api/ping/matrix', {}), appendLog(log));
    if (j.status === 'error') return toast(j.error, true);
    const r = j.result;
    $('#matrix').innerHTML = `<p><b class="${r.ok === r.total ? 'res-ok' : 'res-bad'}">${r.ok}/${r.total} reachable</b></p><table class="mx"><tr><th>จาก \\ ไป</th>${r.names.map((n) => `<th>${esc(n)}</th>`).join('')}</tr>`
      + r.names.map((a) => `<tr><th>${esc(a)}</th>${r.names.map((b) => {
        if (a === b) return '<td class="self">—</td>';
        const rs = r.cells[a][b] || [], ok = rs.filter((x) => x.ok).length;
        return `<td class="${!rs.length ? 'self' : ok === rs.length ? 'ok' : ok ? 'part' : 'fail'}" title="${esc(rs.map((x) => x.ip + (x.ok ? ' ✓' : ' ✗')).join('\n'))}">${rs.length ? (ok === rs.length ? '✔ ' : ok ? '◐ ' : '✘ ') + ok + '/' + rs.length : 'no IP'}</td>`;
      }).join('')}</tr>`).join('') + '</table>';
    await refresh();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = 'รันทดสอบทั้งหมด'; }
};

/* ------------------------------------------------------------------ demo mode (built-in Cisco IOS simulator) */
async function startDemo(reset) {
  const box = $('#discBox'), log = $('#discLog'), btn = $('#btnDemo');
  box.hidden = false; log.textContent = ''; btn.disabled = true; btn.innerHTML = '<span class="spin"></span>กำลังสร้าง...';
  try {
    const j = await runJob(api('/api/demo/start', { reset }), appendLog(log));
    if (j.status === 'error') toast(j.error, true); else { toast(`โหมดจำลองพร้อม: ${j.result.nodes} อุปกรณ์, ${j.result.links} links`); localStorage.removeItem('ns_pos'); }
    await refresh();
  } catch (e) {
    toast(e.message === 'not found' ? 'Backend ยังเป็นเวอร์ชันเก่า — ปิดโปรแกรมแล้วเปิดใหม่ด้วย start.bat' : e.message, true);
  } finally { btn.disabled = false; btn.textContent = '🧪 Demo'; }
}
function openDemoMenu() {
  const m = openModal('🧪 โหมดจำลอง (Cisco IOS จำลอง 4 เครื่อง)', { narrow: true });
  m.el.innerHTML = `<p class="hint" style="margin-top:0">ไม่ต้องมีอุปกรณ์จริง — ใช้โค้ดชุดเดียวกับของจริง ต่างกันแค่ปลายทางเป็นตัวจำลอง เลือกจุดเริ่มต้น:</p>
    <div class="plan" style="cursor:pointer" id="dm_blank"><h3>🧪 เริ่มจากเปล่า <span class="tag">แนะนำ</span></h3><div class="muted">อุปกรณ์ยังเป็นค่าโรงงาน ไม่มีอะไรลงทะเบียน — คุณลองเองทุกขั้น: Initial Config → Discovery → สั่ง config ด้วย Prompt → Ping</div></div>
    <div class="plan" style="cursor:pointer" id="dm_auto"><h3>⚡ ตั้งค่าให้แล้ว</h3><div class="muted">ทำ Initial Config + Discovery ให้อัตโนมัติ (~1 นาที) เห็น topology ทันที แล้วลองสั่ง config / ping ต่อ</div></div>`;
  $('#dm_blank', m.el).onclick = () => { m.close(); startBlankDemo(); };
  $('#dm_auto', m.el).onclick = () => { m.close(); startDemo(false); };
}
async function startBlankDemo() {
  try {
    await api('/api/demo/blank', {});
    localStorage.removeItem('ns_pos'); await refresh();
    toast('อุปกรณ์จำลองพร้อม (ว่างเปล่า) — ไปที่ Initial Config แล้วกด "ใช้ค่านี้"');
    $('#tabs [data-view=init]').click();
  } catch (e) { toast(e.message === 'not found' ? 'Backend ยังเป็นเวอร์ชันเก่า — ปิดโปรแกรมแล้วเปิดใหม่ด้วย start.bat' : e.message, true); }
}
$('#btnDemo').onclick = openDemoMenu;
$('#demoBlank').onclick = () => { if (confirm('คืนอุปกรณ์จำลองเป็นค่าโรงงานและลบออกจากรายการ?')) startBlankDemo(); };
$('#demoAuto').onclick = () => { if (confirm('ตั้งค่าอุปกรณ์จำลองให้ใหม่ทั้งหมดอัตโนมัติ?')) startDemo(true); };
$('#demoExit').onclick = async () => { await api('/api/demo/stop', {}); localStorage.removeItem('ns_pos'); toast('ออกจากโหมดจำลองแล้ว'); refresh(); };
$('#demoPing').onclick = () => $('#tabs [data-view=ping]').click();
$('#demoInit').onclick = () => $('#tabs [data-view=init]').click();
$('#demoDisc').onclick = () => $('#btnDiscover').click();

async function renderSim() {
  const card = $('#simCard');
  card.hidden = !state.demo;
  if (!state.demo) return;
  try {
    const { devices: sims } = await api('/api/demo/info');
    const list = $('#simList'); list.innerHTML = '';
    sims.forEach((d) => {
      const reg = state.devices.some((x) => x.mgmt_ip === d.mgmt_ip);
      const el = document.createElement('div'); el.className = 'dev';
      el.innerHTML = `<i class="dot ${reg ? 'ok' : d.factory ? 'warn' : 'bad'}"></i><div class="sp"><b>${esc(d.name)} <span class="muted">${esc(d.kind)}</span></b><small>console 127.0.0.1:${d.console_port} · mgmt ${esc(d.mgmt_ip)} · ${reg ? 'ลงทะเบียนแล้ว' : d.factory ? 'ค่าโรงงาน' : 'ตั้งค่าแล้ว (ยังไม่ลงทะเบียน)'}</small></div><button class="btn small" ${reg ? 'disabled' : ''}>ใช้ค่านี้</button>`;
      $('button', el).onclick = () => {
        $$('#conType button')[0].click();
        const set = (id, v) => { $('#' + id).value = v; };
        set('conHost', '127.0.0.1'); set('conPort', d.console_port); set('f_hostname', d.name); set('f_device_type', d.kind);
        set('f_mgmt_ip', d.mgmt_ip); set('f_mgmt_prefix', '8'); set('f_mgmt_if', 'GigabitEthernet0/0');
        if (!$('#f_password').value) set('f_password', 'cisco123');
        if (!d.factory) toast('อุปกรณ์นี้ถูกตั้งค่าไปแล้ว — ถ้าจะส่งซ้ำต้องกรอก Existing enable password (cisco123) หรือรีเซ็ตเป็นเปล่าก่อน', true);
      };
      list.appendChild(el);
    });
  } catch (e) { /* ignore */ }
}

$$('[data-demo]').forEach((b) => b.onclick = () => { $('#promptText').value = b.dataset.demo; window.__rulesOnly = true; $('#btnPreview').click(); });

/* ------------------------------------------------------------------ AI settings (OpenRouter) */
let aiPrimary = false;
async function loadAi() {
  try {
    const st = await api('/api/settings');
    aiPrimary = st.ai_ready;
    $('#aiState').textContent = st.ai_ready ? '⚙ ' + st.ai_model.split('/').pop() : '⚠ ใส่ API key';
    $('#btnPreview').textContent = st.ai_ready ? '🤖 ให้ AI วางแผน → Preview' : 'Preview (ยังไม่มี AI key — ใช้ parser)';
    return st;
  } catch (e) { return null; }
}
$('#btnAi').onclick = async () => {
  const st = (await loadAi()) || {};
  const m = openModal('ตั้งค่า AI (OpenRouter)', { narrow: true });
  m.el.innerHTML = `<p class="hint" style="margin-top:0">ทุกคำสั่งในกล่อง Prompt จะถูกส่งให้ AI วางแผน config แล้วแสดงใน Preview ให้คุณยืนยัน — ส่งให้ AI เฉพาะ <b>ชื่ออุปกรณ์ ชื่อ/IP ของ interface และ link</b> (ไม่ส่งรหัสผ่าน) คำสั่งอันตรายถูกกรองทิ้ง และไม่มีอะไรถูกส่งไปอุปกรณ์จนกว่าคุณจะกดยืนยัน</p>
    <div class="form"><label style="grid-column:1/-1">OpenRouter API key ${st.has_key ? '<span class="res-ok">(' + (st.key_from_env ? 'ใช้จากไฟล์ .env / ตัวแปร OPENROUTER_API_KEY' : 'บันทึกแล้ว — เว้นว่างเพื่อใช้ค่าเดิม') + ')</span>' : ''}<input id="ai_key" type="password" placeholder="sk-or-... (หรือใส่ในไฟล์ .env: OPENROUTER_API_KEY=...)" autocomplete="off"></label>
    <label style="grid-column:1/-1">Model (โมเดลเล็กๆ ก็พอ) <input id="ai_model" list="aiModels" value="${esc(st.ai_model || 'openai/gpt-4o-mini')}"><datalist id="aiModels"><option value="openai/gpt-4o-mini"><option value="openai/gpt-4.1-mini"><option value="openai/gpt-5-mini"><option value="google/gemini-2.5-flash"></datalist></label></div>
    <div class="row gap end"><button class="btn" id="ai_test">ทดสอบ key</button>${st.has_key && !st.key_from_env ? '<button class="btn" id="ai_clear">ลบ key</button>' : ''}<button class="btn primary" id="ai_save">บันทึก</button></div>`;
  const save = async (extra = {}) => api('/api/settings', { ai_key: $('#ai_key').value, ai_model: $('#ai_model').value, ...extra });
  $('#ai_save', m.el).onclick = async () => { try { await save(); await loadAi(); toast('บันทึกแล้ว'); m.close(); } catch (e) { toast(e.message, true); } };
  $('#ai_test', m.el).onclick = async () => {
    try { await save(); const r = await api('/api/ai/test', {}); toast('AI ตอบกลับได้: ' + r.reply); await loadAi(); } catch (e) { toast(e.message, true); }
  };
  const clr = $('#ai_clear', m.el); if (clr) clr.onclick = async () => { await api('/api/settings', { clear_key: true }); await loadAi(); toast('ลบ key แล้ว'); m.close(); };
};

$('#promptText').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#btnPreview').click(); } });
loadAi();
refresh();
