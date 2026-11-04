(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const ROOT = document.documentElement;

  window.addEventListener('error', (e) => {
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;left:14px;bottom:14px;z-index:99;max-width:520px;' +
      'padding:10px 14px;border-radius:10px;background:#2a1416;border:1px solid #f2565d;' +
      'color:#fca5a5;font:12px/1.5 ui-monospace,monospace';
    box.textContent = '前端异常：' + (e.message || e);
    document.body.appendChild(box);
  });

  const CAT_COLOR = {
    infra: 'var(--cat-infra)',
    net: 'var(--cat-net)',
    media: 'var(--cat-media)',
    smart: 'var(--cat-smart)',
    client: 'var(--cat-client)',
  };
  const CAT_ORDER = ['infra', 'net', 'media', 'smart', 'client'];
  const CAT_LABEL = {
    infra: '基础设施', net: '网络设备', media: '影音数码',
    smart: '智能家居', client: '个人终端',
  };

  const state = {
    snap: null,
    series: { down: [], up: [] },
    wallKey: '',
    probeKey: '',
    vmKey: '',
    filter: 'all',
    devices: {},
    sheet: null,
  };

  /* ---------------- 工具 ---------------- */
  const pad = (n) => String(n).padStart(2, '0');

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  const cssVar = (n) => getComputedStyle(ROOT).getPropertyValue(n).trim();

  function alpha(color, a) {
    const c = (color || '').trim();
    if (c.startsWith('#')) {
      let h = c.slice(1);
      if (h.length === 3) h = h.split('').map((x) => x + x).join('');
      const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
      return `rgba(${r},${g},${b},${a})`;
    }
    return c;
  }

  function fmtRate(bps) {
    if (!bps || bps < 1) return '0';
    const u = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
    let i = 0, v = bps;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + ' ' + u[i];
  }

  function fmtBytes(b) {
    if (!b) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' ' + u[i];
  }

  function fmtDur(sec) {
    if (!sec) return '—';
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return `${d} 天 ${h} 小时`;
    if (h) return `${h} 小时 ${m} 分`;
    return `${m} 分钟`;
  }

  const icon = (name) => `<svg><use href="#i-${name || 'device'}"></use></svg>`;

  /* ---------------- 主题 ---------------- */
  function currentTheme() {
    return ROOT.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function applyTheme(t) {
    ROOT.setAttribute('data-theme', t);
    try { localStorage.setItem('noc-theme', t); } catch (e) { /* 忽略 */ }
    const m = document.querySelector('meta[name="theme-color"]');
    if (m) m.content = t === 'light' ? '#eef2f7' : '#070a0f';
    drawChart();
  }

  $('themeBtn').addEventListener('click', () => {
    applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
  });

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  }
  $('fsBtn').addEventListener('click', toggleFullscreen);

  /* ---------------- 时钟 ---------------- */
  const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  function tickClock() {
    const d = new Date();
    $('clock').textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    $('date').textContent = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${WEEK[d.getDay()]}`;
  }
  setInterval(tickClock, 1000); tickClock();

  /* ---------------- 拓扑 ---------------- */
  // 通用结构与坐标写在这里；节点上显示的文字由 config.local.json 的 topology 段
  // 通过 /api/snapshot 的 site.topology 下发覆盖（见 applySiteLabels）。
  // 因此仓库里不含任何内网地址。
  const NODES = {
    ont:    { x: 470, y: 72,  w: 176, title: '光猫',        sub: '桥接 · 2 路出口', probe: null },
    iptv:   { x: 800, y: 72,  w: 168, title: 'IPTV 机顶盒',  sub: '独立链路',        probe: null },
    ikuai:  { x: 470, y: 190, w: 176, title: 'iKuai 主路由', sub: '硬件直通',        probe: 'ikuai' },
    switch: { x: 470, y: 306, w: 196, title: '2.5G PoE 交换机', sub: '5 口 · 2.5G', probe: 'switch', ports: 5 },
    apsub:  { x: 148, y: 434, w: 152, title: '子 AP',        sub: 'PoE 供电',        probe: 'apsub' },
    apmain: { x: 396, y: 434, w: 152, title: '主 AP',        sub: 'Mesh 主节点',     probe: 'apmain' },
    pve:    { x: 690, y: 434, w: 168, title: 'PVE 宿主机',   sub: 'vmbr0 管理口',    probe: 'pve' },
    nas:    { x: 396, y: 542, w: 176, title: 'NAS',           sub: '双 2.5G',         probe: 'nas' },
  };
  const NH = 54;

  // 站点相关的显示文字 / NAS 地址：由后端从 config.local.json 下发，只应用一次
  let siteApplied = false;
  let NAS_IP = '';

  function applySiteMeta(site) {
    if (!site || siteApplied) return;
    siteApplied = true;
    NAS_IP = site.nas_ip || '';
    const labels = site.topology || {};
    for (const [key, lab] of Object.entries(labels)) {
      const n = NODES[key];
      if (!n || !lab) continue;
      if (lab.title) n.title = lab.title;
      if (lab.sub) n.sub = lab.sub;
      // buildTopo 已经跑过了，直接改现成的 text 节点，避免重建整张图
      const g = document.getElementById('nd-' + key);
      if (!g) continue;
      const tEl = g.querySelector('.node-title');
      const sEl = g.querySelector('.node-sub');
      if (tEl && lab.title) tEl.textContent = lab.title;
      if (sEl && lab.sub) sEl.textContent = lab.sub;
    }
  }

  function portXY(node, idx) {
    const n = NODES[node];
    const left = n.x - n.w / 2;
    const step = n.w / n.ports;
    return { x: left + step * (idx + 0.5), y: n.y + NH / 2 };
  }

  function buildTopo() {
    const svg = $('topo');
    svg.innerHTML = '';

    const defs = document.createElementNS(SVG_NS, 'defs');
    defs.innerHTML = `
      <marker id="tw-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.6" stroke-linecap="round"/>
      </marker>`;
    svg.appendChild(defs);

    const links = document.createElementNS(SVG_NS, 'g');
    links.setAttribute('id', 'topoLinks');
    svg.appendChild(links);

    const nodesG = document.createElementNS(SVG_NS, 'g');
    nodesG.setAttribute('id', 'topoNodes');
    svg.appendChild(nodesG);

    const p5 = portXY('switch', 4);
    const p4 = portXY('switch', 3);
    const p1 = portXY('switch', 0);
    const p2 = portXY('switch', 1);

    const paths = [
      { id: 'lk-wan', cls: 'wan', d: `M${NODES.ont.x},${NODES.ont.y + NH / 2} L${NODES.ikuai.x},${NODES.ikuai.y - NH / 2}`, arrow: true },
      { id: 'lk-iptv', cls: 'bypass', d: `M${NODES.ont.x + NODES.ont.w / 2},${NODES.ont.y} L${NODES.iptv.x - NODES.iptv.w / 2},${NODES.iptv.y}`, arrow: true },
      { id: 'lk-lan', cls: 'lan', d: `M${NODES.ikuai.x},${NODES.ikuai.y + NH / 2} L${NODES.ikuai.x},${NODES.switch.y - NH / 2 - 26} L${p5.x},${NODES.switch.y - NH / 2 - 26} L${p5.x},${p5.y}`, arrow: true },
      { id: 'lk-hairpin', cls: 'hairpin', d: `M${p4.x},${p4.y} C${p4.x},${p4.y + 66} ${NODES.pve.x - 10},${NODES.pve.y - 96} ${NODES.pve.x},${NODES.pve.y - NH / 2}`, arrow: true },
      { id: 'lk-apsub', cls: 'lan', d: `M${p1.x},${p1.y} L${p1.x},${NODES.apsub.y - 34} L${NODES.apsub.x},${NODES.apsub.y - 34} L${NODES.apsub.x},${NODES.apsub.y - NH / 2}`, arrow: true },
      { id: 'lk-apmain', cls: 'lan', d: `M${p2.x},${p2.y} L${p2.x},${NODES.apmain.y - 34} L${NODES.apmain.x},${NODES.apmain.y - 34} L${NODES.apmain.x},${NODES.apmain.y - NH / 2}`, arrow: true },
      { id: 'lk-nas-wire', cls: 'bypass', d: `M${NODES.apmain.x},${NODES.apmain.y + NH / 2} L${NODES.apmain.x},${NODES.nas.y - NH / 2}`, arrow: false },
    ];

    for (const p of paths) {
      const base = document.createElementNS(SVG_NS, 'path');
      base.setAttribute('class', 'lnk ' + p.cls);
      base.setAttribute('d', p.d);
      base.setAttribute('id', p.id);
      if (p.cls === 'bypass') base.setAttribute('marker-end', 'url(#tw-arrow)');
      links.appendChild(base);

      if (p.cls !== 'bypass') {
        const flow = document.createElementNS(SVG_NS, 'path');
        flow.setAttribute('class', 'flow ' + p.cls);
        flow.setAttribute('d', p.d);
        flow.setAttribute('id', p.id + '-flow');
        flow.style.animationDuration = '2s';
        links.appendChild(flow);
      }
    }

    const labels = [
      { x: 300, y: 132, t: 'WAN 2.5G', cls: 'tag', anchor: 'end' },
      { x: 596, y: 132, t: 'IPTV 独立链路', cls: 'tag', anchor: 'start' },
      { x: 580, y: 246, t: 'LAN1 · 口 5 直通', cls: 'tag', anchor: 'start' },
      { x: 730, y: 366, t: '口 4 回程 → NIC3 → vmbr0', cls: 'tag-warn', anchor: 'end' },
      { x: 186, y: 392, t: '口 1 · PoE', cls: 'tag', anchor: 'start' },
      { x: 424, y: 392, t: '口 2', cls: 'tag', anchor: 'start' },
      { x: 430, y: 500, t: '二层旁路 · 不经 iKuai', cls: 'tag', anchor: 'start' },
    ];
    for (const l of labels) {
      const t = document.createElementNS(SVG_NS, 'text');
      t.setAttribute('x', l.x); t.setAttribute('y', l.y);
      t.setAttribute('class', l.cls);
      t.setAttribute('text-anchor', l.anchor);
      t.textContent = l.t;
      links.appendChild(t);
    }

    for (const [key, n] of Object.entries(NODES)) {
      const g = document.createElementNS(SVG_NS, 'g');
      g.setAttribute('class', 'node on');
      g.setAttribute('id', 'nd-' + key);

      const rect = document.createElementNS(SVG_NS, 'rect');
      rect.setAttribute('class', 'node-box');
      rect.setAttribute('x', n.x - n.w / 2);
      rect.setAttribute('y', n.y - NH / 2);
      rect.setAttribute('width', n.w);
      rect.setAttribute('height', NH);
      rect.setAttribute('rx', 11);
      g.appendChild(rect);

      const title = document.createElementNS(SVG_NS, 'text');
      title.setAttribute('x', n.x);
      title.setAttribute('y', n.y - 7);
      title.setAttribute('text-anchor', 'middle');
      title.setAttribute('class', 'node-title');
      title.textContent = n.title;
      g.appendChild(title);

      const sub = document.createElementNS(SVG_NS, 'text');
      sub.setAttribute('x', n.x);
      sub.setAttribute('y', n.y + 11);
      sub.setAttribute('text-anchor', 'middle');
      sub.setAttribute('class', 'node-sub');
      sub.textContent = n.sub;
      g.appendChild(sub);

      if (n.ports) {
        const left = n.x - n.w / 2, step = n.w / n.ports;
        for (let i = 0; i < n.ports; i++) {
          const cx = left + step * (i + 0.5);
          const port = document.createElementNS(SVG_NS, 'rect');
          port.setAttribute('x', cx - 7);
          port.setAttribute('y', n.y + NH / 2 - 3);
          port.setAttribute('width', 14);
          port.setAttribute('height', 8);
          port.setAttribute('rx', 2);
          port.setAttribute('id', 'pt-' + (i + 1));
          port.setAttribute('class', 'portbox');
          g.appendChild(port);

          const num = document.createElementNS(SVG_NS, 'text');
          num.setAttribute('x', cx);
          num.setAttribute('y', n.y + NH / 2 + 13);
          num.setAttribute('text-anchor', 'middle');
          num.setAttribute('class', 'tag');
          num.setAttribute('style', 'font-size:8px');
          num.textContent = String(i + 1);
          g.appendChild(num);

          const rt = mkText(cx, n.y + NH / 2 + 24, 'port-rate', 'middle', '');
          rt.setAttribute('id', 'ptr-' + (i + 1));
          g.appendChild(rt);
        }
      }

      nodesG.appendChild(g);
    }

    const rateWan = mkText(NODES.ont.x, NODES.ont.y - NH / 2 - 10, 'node-rate', 'middle', '');
    rateWan.setAttribute('id', 'rt-wan');
    links.appendChild(rateWan);

    const rateLan = mkText(NODES.ikuai.x, NODES.ikuai.y + NH / 2 + 15, 'node-rate', 'middle', '');
    rateLan.setAttribute('id', 'rt-lan');
    links.appendChild(rateLan);

    const rateHair = mkText(716, 350, 'node-rate', 'start', '');
    rateHair.setAttribute('id', 'rt-hair');
    rateHair.setAttribute('style', 'fill:var(--warn)');
    links.appendChild(rateHair);

    const rateNas = mkText(NODES.nas.x, NODES.nas.y + NH / 2 + 20, 'node-rate', 'middle', '');
    rateNas.setAttribute('id', 'rt-nas');
    rateNas.setAttribute('style', 'fill:var(--violet)');
    links.appendChild(rateNas);
  }

  function mkText(x, y, cls, anchor, content) {
    const t = document.createElementNS(SVG_NS, 'text');
    t.setAttribute('x', x); t.setAttribute('y', y);
    t.setAttribute('class', cls); t.setAttribute('text-anchor', anchor);
    t.textContent = content;
    return t;
  }

  function setFlow(id, bytesPerSec) {
    const el = $(id + '-flow');
    if (!el) return;
    const kbs = (bytesPerSec || 0) / 1024;
    let dur;
    if (kbs < 4) dur = 2.8;
    else dur = Math.max(0.34, 2.8 - Math.log10(kbs) * 0.62);
    el.style.animationDuration = dur.toFixed(2) + 's';
    el.style.opacity = bytesPerSec > 200 ? 1 : 0.35;
  }

  function updateTopo(snap) {
    const wan = snap.wan || {};
    const lan = snap.lan || {};
    const probes = {};
    (snap.probes || []).forEach((p) => { probes[p.key] = p; });

    for (const [key, n] of Object.entries(NODES)) {
      const g = $('nd-' + key);
      if (!g) continue;
      if (!n.probe) continue;
      const p = probes[n.probe];
      g.setAttribute('class', 'node ' + (p && p.up ? 'on' : 'off'));
    }

    // 交换机端口：所有链路速率都来自设备实测，不再用比例估算
    const pt = {};
    ((snap.switch || {}).ports || []).forEach((p) => { pt[p.no] = p; });
    const sum = (p) => (p ? (p.rx || 0) + (p.tx || 0) : 0);
    const lanTotal = (lan.up || 0) + (lan.down || 0);

    const wanTotal = (wan.up || 0) + (wan.down || 0);
    setFlow('lk-wan', wanTotal);
    setFlow('lk-lan', sum(pt[5]) || lanTotal);
    setFlow('lk-apsub', sum(pt[1]));
    setFlow('lk-apmain', sum(pt[2]));
    setFlow('lk-hairpin', sum(pt[4]));

    // 端口方块状态 + 速率标注（颜色交给 CSS，主题切换才跟着变）
    for (let i = 1; i <= 5; i++) {
      const box = $('pt-' + i);
      const txt = $('ptr-' + i);
      const p = pt[i];
      if (box) box.setAttribute('class', 'portbox' + (p && p.up ? ' up' : ''));
      if (txt) {
        const total = sum(p);
        txt.textContent = (!p || !p.up || total < 1024) ? '' : fmtRate(total);
      }
    }

    const rtWan = $('rt-wan');
    if (rtWan) rtWan.textContent = '↓ ' + fmtRate(wan.down) + '   ↑ ' + fmtRate(wan.up);

    const rtLan = $('rt-lan');
    if (rtLan) rtLan.textContent = '↓ ' + fmtRate(lan.down) + '   ↑ ' + fmtRate(lan.up);

    const rtHair = $('rt-hair');
    if (rtHair) {
      const t = sum(pt[4]);
      rtHair.textContent = t ? fmtRate(t) : '';
    }

    const rtNas = $('rt-nas');
    if (rtNas) {
      const n = snap.nas;
      if (n && n.ok) {
        // NAS 网卡实测总吞吐；减去 iKuai 看到的 .30 外网流量 = 纯二层内网流量
        const l2 = n.l2_bps || 0;
        rtNas.textContent = fmtRate(n.total_bps) + (l2 > 8000 ? ' · 内网 ' + fmtRate(l2) : '');
        rtNas.setAttribute('class', l2 > 1e6 ? 'node-rate hot' : 'node-rate');
      } else if (n && n.name) {
        rtNas.textContent = '采集端失联';
        rtNas.setAttribute('class', 'node-rate bad');
      } else {
        const dev = NAS_IP ? (snap.devices || []).find((d) => d.ip === NAS_IP) : null;
        const t = dev ? (dev.up || 0) + (dev.down || 0) : 0;
        const ap = dev && dev.wifi_ap ? ' · ' + dev.wifi_ap : '';
        rtNas.textContent = t ? '外网 ' + fmtRate(t) + ap : (ap.replace(' · ', '') || '—');
      }
    }
  }

  /* ---------------- 曲线 ---------------- */
  function drawChart() {
    const cv = $('wanChart');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight || 64;
    if (!w) return;
    if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const down = state.series.down, up = state.series.up;
    const n = Math.max(down.length, 2);
    const max = Math.max(1024, ...down, ...up) * 1.18;

    const cDown = cssVar('--violet') || '#9d7cf5';
    const cUp = cssVar('--cyan') || '#35b6f2';

    ctx.strokeStyle = cssVar('--grid-line') || 'rgba(255,255,255,0.055)';
    ctx.lineWidth = 1;
    for (let i = 1; i <= 3; i++) {
      const y = (h / 4) * i;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }

    const plot = (arr, color, fillA) => {
      if (arr.length < 2) return;
      const step = w / (n - 1);
      const x0 = w - (arr.length - 1) * step;
      ctx.beginPath();
      arr.forEach((v, i) => {
        const x = x0 + i * step;
        const y = h - Math.min(v / max, 1) * (h - 4) - 1;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = 'round';
      ctx.stroke();

      if (fillA) {
        ctx.lineTo(w, h); ctx.lineTo(x0, h); ctx.closePath();
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, alpha(color, fillA));
        grad.addColorStop(1, alpha(color, 0));
        ctx.fillStyle = grad;
        ctx.fill();
      }
    };

    plot(down, cDown, 0.22);
    plot(up, cUp, 0.16);
  }

  function pushSeries(snap) {
    const wan = snap.wan || {};
    state.series.down.push(wan.down || 0);
    state.series.up.push(wan.up || 0);
    const MAX = 120;
    if (state.series.down.length > MAX) state.series.down.shift();
    if (state.series.up.length > MAX) state.series.up.shift();
  }

  /* ---------------- 渲染 ---------------- */
  function render(snap) {
    state.snap = snap;
    applySiteMeta(snap.site);
    pushSeries(snap);
    drawChart();
    updateTopo(snap);

    const h = snap.health || { score: 0, level: 'ok', issues: [] };
    const hb = $('healthBox');
    hb.className = 'stat health-stat ' + (h.level === 'ok' ? '' : h.level);
    $('healthScore').textContent = h.score;
    hb.title = '点击查看问题清单';

    const wan = snap.wan || {};
    const wc = $('wanBadge');
    const ok = wan.ok !== false;
    wc.textContent = ok ? (wan.internet || 'PPPoE') : '断线';
    wc.className = 'badge ' + (ok ? 'ok' : 'crit');
    $('wanIp').textContent = wan.public_ip || wan.ip || '—';
    $('wanState').textContent = ok ? '在线' : '异常';
    $('wanState').style.color = ok ? 'var(--ok)' : 'var(--crit)';

    const ik = snap.ikuai || {};
    $('wanUptime').textContent = ik.uptime ? '运行 ' + fmtDur(ik.uptime) : '—';
    $('wanDownBig').textContent = fmtRate(wan.down);
    $('wanUpBig').textContent = fmtRate(wan.up);
    $('wanUp').textContent = fmtRate(wan.up);
    $('wanDown').textContent = fmtRate(wan.down);

    $('onlineCount').textContent = snap.summary ? snap.summary.online : '—';
    $('knownCount').textContent = '/' + (snap.summary ? snap.summary.known : '—');
    $('connCount').textContent = (ik.conn_total || 0).toLocaleString();

    renderStatusBand(snap);
    renderPorts(snap.switch);
    renderWifi(snap.miwifi);
    renderProbes(snap.probes || []);
    renderPVE(snap.pve);
    renderStore(snap.pve, snap.local);
    renderEvents(snap.events || []);
    renderWall(snap.devices || [], snap.summary);
    refreshSheet();
  }

  /* ---------------- 全屋状态带 ---------------- */
  const WEATHER_CN = {
    'clear-night': '晴（夜）', clear: '晴', sunny: '晴', cloudy: '多云',
    partlycloudy: '局部多云', rainy: '雨', pouring: '大雨', lightning: '雷雨',
    snowy: '雪', fog: '雾', windy: '大风', hail: '冰雹', exceptional: '极端',
  };

  function renderStatusBand(snap) {
    const ha = snap.ha || {};
    const sum = snap.summary || {};
    const env = ha.env || [];

    // 优先选带湿度的室温传感器（温湿度一体的那个才是室内真实体感）
    let room = env.find((e) => e.temp != null && e.hum != null);
    if (!room) room = env.find((e) => e.temp != null);
    setTxt('sbTemp', room && room.temp != null ? room.temp.toFixed(1) + '°' : '—');
    setTxt('sbHum', room && room.hum != null ? room.hum.toFixed(0) + '% · ' + room.name : (room ? room.name : '无传感器'));

    const od = ha.outdoor || {};
    setTxt('sbOutdoor', od.temp != null ? od.temp.toFixed(1) + '°' : '—');
    setTxt('sbWeather', od.cond ? (WEATHER_CN[od.cond] || od.cond) : '—');

    const lo = sum.lights_on || 0, lt = sum.lights_total || 0;
    setTxt('sbLights', lt ? `${lo}/${lt}` : '—');
    setSbi('sbiLights', lo > 0 ? 'on' : '');
    // 鼠标悬停就能看到具体是哪几盏亮着，不用点开面板
    setTip('sbiLights', lampTip(ha, lo, lt));

    const acOn = sum.ac_on || 0;
    setTxt('sbAc', `${acOn} 开`);
    setSbi('sbAc', acOn > 0 ? 'okk' : '');

    // 门窗：真门磁优先；没有就如实说"未接入"，别拿人体传感器冒充
    if (sum.has_door_sensor) {
      const op = sum.openings_on || 0;
      setTxt('sbOpen', op ? `${op} 开` : '全关');
      setSbi('sbiOpen', op > 0 ? 'crit' : 'okk');
    } else {
      setTxt('sbOpen', '未接入');
      setSbi('sbiOpen', 'dim');
    }
    setTip('sbiOpen', openingTip(ha, sum));

    const pOn = sum.presence_on || 0, pTot = sum.presence_total || 0;
    setTxt('sbPresence', pTot ? `${pOn}/${pTot}` : '—');
    setSbi('sbPresence', pOn > 0 ? 'okk' : '');
    const who = (ha.presence || []).filter((p) => p.active).map((p) => p.area + ' ' + p.name);
    setTip('sbPresence', who.length ? '有人：' + who.join('、') : '全部区域无人');

    const wifi = snap.miwifi || {};
    setTxt('sbWifi', (wifi.online || 0) + ' 台');

    const home = (ha.persons || []).filter((p) => p.state === 'home');
    setTxt('sbHome', home.length ? home.map((p) => p.name).join('·') : '无人');
    setSbi('sbHome', home.length ? 'okk' : '');

    // 内网二层流量：NAS 网卡实测总吞吐 − iKuai 侧 .30 外网流量
    const nas = snap.nas;
    if (nas && nas.ok) {
      setTxt('sbL2', fmtRate(nas.l2_bps));
      setSbi('sbL2', nas.l2_bps > 5e6 ? 'on' : nas.l2_bps > 500e3 ? 'okk' : '');
      setTxt('sbL2Note', '总 ' + fmtRate(nas.total_bps));
    } else if (nas && nas.name) {
      setTxt('sbL2', '失联');
      setSbi('sbL2', 'crit');
      setTxt('sbL2Note', nas.age ? nas.age + ' 秒前' : '采集端');
    } else {
      setTxt('sbL2', '—');
      setSbi('sbL2', '');
      setTxt('sbL2Note', '待接入采集端');
    }

    if (ha.total) {
      const pct = Math.round((ha.available || 0) / ha.total * 100);
      setTxt('sbHa', pct + '%');
      setSbi('sbHa', pct < 35 ? 'crit' : pct < 60 ? 'on' : 'okk');
    } else {
      setTxt('sbHa', '—');
    }
  }

  function setTxt(id, v) {
    const el = $(id);
    if (el) el.textContent = v;
  }

  // 只动状态类，别把 clickable 一起抹掉
  function setSbi(id, cls) {
    const el = $(id);
    if (!el) return;
    const box = el.classList.contains('sbi') ? el : el.closest('.sbi');
    if (!box) return;
    box.classList.remove('on', 'crit', 'okk', 'dim');
    if (cls) box.classList.add(cls);
  }

  function setTip(id, text) {
    const el = $(id);
    if (!el) return;
    const box = el.classList.contains('sbi') ? el : el.closest('.sbi');
    if (box) box.title = text;
  }

  function lampTip(ha, lo, lt) {
    const on = (ha.lamps || []).filter((l) => l.on);
    if (!on.length) return `共 ${lt} 盏灯，当前全暗`;
    const names = on.map((l) => (l.area && l.area !== '其他' ? l.area + ' ' : '') + l.name);
    return `${lo} 盏亮着：\n` + names.join('\n') + '\n\n点击查看全屋灯位';
  }

  function openingTip(ha, sum) {
    const cows = ha.covers || [];
    const op = cows.filter((c) => c.open);
    const lines = [];
    if (sum.has_door_sensor) {
      const d = (ha.openings || []).filter((o) => o.open);
      lines.push(d.length ? '开着的门窗：' + d.map((o) => o.area + ' ' + o.name).join('、') : '全部门窗关闭');
    } else {
      lines.push('未接入门窗磁传感器');
    }
    lines.push(op.length
      ? '窗帘开着：' + op.map((c) => c.name).join('、')
      : '窗帘全部关闭');
    return lines.join('\n');
  }

  /* ---------------- 交换机端口 ---------------- */
  const PORTS = [
    { no: 1, role: '书房 AP', node: 'apsub' },
    { no: 2, role: '客厅 AP', node: 'apmain' },
    { no: 3, role: '空置', node: null },
    { no: 4, role: 'PVE 回程', node: 'pve' },
    { no: 5, role: 'iKuai 主干', node: 'ikuai' },
  ];

  function renderPorts(sw) {
    const ul = $('portList');
    if (!ul) return;
    const ports = (sw && sw.ports) || [];
    const byNo = {};
    ports.forEach((p) => { byNo[p.no] = p; });
    const maxRate = Math.max(1024 * 64, ...ports.map((p) => (p.rx || 0) + (p.tx || 0)));

    ul.innerHTML = PORTS.map((meta) => {
      const p = byNo[meta.no] || { up: false, rx: 0, tx: 0, speed: '—' };
      const up = !!p.up;
      const total = (p.rx || 0) + (p.tx || 0);
      const rxW = Math.min((p.rx || 0) / maxRate * 100, 100);
      const txW = Math.min((p.tx || 0) / maxRate * 100, 100);
      const rateHtml = up && total > 512
        ? `<b>${fmtRate(p.rx)}</b> <i>${fmtRate(p.tx)}</i>`
        : (up ? '<span style="color:var(--txt-3)">空闲</span>' : '—');
      // speed 带 ? 后缀 = 本轮速率没读到，沿用上次（交换机页面偶发读 0，链路是通的）
      const speedTxt = up ? (p.speed || '') : 'DOWN';
      const jit = p.jitter ? ' · 速率读数抖动，已沿用上次值' : '';
      return `<li class="${up ? 'up' : 'down'}" title="口${meta.no} ${meta.role} · ${p.speed || '—'}${jit}">
        <span class="port-no">${meta.no}</span>
        <span class="port-mid">
          <span class="port-name">${meta.role}<em class="${p.jitter ? 'jit' : ''}">${speedTxt}</em></span>
          <span class="port-bars"><i class="rx" style="width:${rxW.toFixed(1)}%"></i><i class="tx" style="width:${txW.toFixed(1)}%"></i></span>
        </span>
        <span class="port-rate ${up && total > 512 ? '' : 'off'}">${rateHtml}</span>
      </li>`;
    }).join('');

    const foot = $('portFoot');
    if (foot) {
      const upN = sw ? sw.up_count : 0;
      const poe = sw ? sw.poe_used_w : 0;
      const poeCap = sw ? sw.poe_ports : 0;
      foot.innerHTML = sw && sw.ok
        ? `<span><b>${upN}</b>/${sw.total_ports} 链路</span><span>PoE <b>${poe.toFixed(1)}W</b> · ${poeCap} 口</span>`
        : '<span style="color:var(--crit)">交换机不可达</span>';
    }
  }

  /* ---------------- 无线覆盖 ---------------- */
  function renderWifi(wifi) {
    const box = $('apList');
    if (!box) return;
    const badge = $('wifiBadge');
    if (badge) {
      const n = (wifi && wifi.online) || 0;
      badge.textContent = n ? n + ' 台在线' : '—';
      badge.className = 'badge ' + (wifi && wifi.ok ? 'ok' : 'crit');
    }
    const aps = (wifi && wifi.aps) || [];
    if (!aps.length) {
      box.innerHTML = '<div class="ap-empty">暂无 AP 数据</div>';
      return;
    }
    box.innerHTML = aps.map((a) => {
      const chs = (a.channels || []).map((c) =>
        `<span class="${c.ch ? 'hot' : ''}">${c.band} ch${c.ch || '?'}</span>`).join('');
      const extra = [
        a.firmware ? `<span>${a.firmware}</span>` : '',
        a.locale ? `<span>${a.locale}</span>` : '',
        a.uplink ? `<span>${a.uplink === 'wired' ? '有线回程' : a.uplink}</span>` : '',
      ].join('');
      return `<div class="ap-item ${a.ok ? '' : 'off'}">
        <div class="ap-top">
          ${icon('ap').replace('<svg>', '<svg class="ap-ico">')}
          <span class="ap-nm">${a.label} · ${a.model}</span>
          <span class="ap-cnt ${a.online ? '' : 'sp'}">${a.ok ? a.online + ' 台' : '离线'}</span>
        </div>
        <div class="ap-meta">${chs}${extra}</div>
      </div>`;
    }).join('');
  }

  function renderProbes(list) {
    const key = list.map((p) => p.key + p.up + Math.round(p.rtt || 0)).join('|');
    if (key === state.probeKey) return;
    state.probeKey = key;
    const ul = $('probeList');
    ul.innerHTML = list.map((p) => {
      const cls = !p.up ? 'bad' : (p.rtt > 100 ? 'warn' : '');
      const val = p.up ? p.rtt.toFixed(1) + ' ms' : '超时';
      const w = p.up ? Math.min(Math.max(p.rtt / 60 * 100, 3), 100) : 100;
      return `<li class="${cls}" title="${p.ip}:${p.port}">
        <span class="pd"></span>
        <span class="pn">${p.name}</span>
        <span class="pbar"><i style="width:${w.toFixed(0)}%"></i></span>
        <span class="pr">${val}</span>
      </li>`;
    }).join('');

    const up = list.filter((p) => p.up).length;
    const avg = list.filter((p) => p.up && p.key !== 'inet')
      .reduce((a, p, _, arr) => a + p.rtt / arr.length, 0);
    const loss = list.length ? Math.max(...list.map((p) => p.loss || 0)) : 0;
    $('probeFoot').textContent = `内网均值 ${avg.toFixed(1)} ms · 最大丢包 ${loss.toFixed(0)}% · ${up}/${list.length} 在线`;
  }

  function renderPVE(pve) {
    if (!pve) return;
    $('pveCpu').textContent = pve.cpu.toFixed(0) + '%';
    const memPct = pve.mem_used / pve.mem_total * 100;
    $('pveMem').textContent = memPct.toFixed(0) + '%';
    setBar($('pveCpuBar'), pve.cpu);
    setBar($('pveMemBar'), memPct);
    $('pveBadge').textContent = (pve.version || '').split('/')[0].replace('pve-manager', 'PVE') || '在线';
    $('pveMeta').textContent = `${pve.cpus} 线程 · ${(pve.mem_used / 1e9).toFixed(1)}/${(pve.mem_total / 1e9).toFixed(0)}G · 运行 ${fmtDur(pve.uptime)}`;

    const key = (pve.guests || []).map((g) => g.vmid + g.status + Math.round((g.cpu || 0)) + Math.round((g.netin + g.netout) / 4096)).join('|');
    if (key === state.vmKey) return;
    state.vmKey = key;
    $('vmList').innerHTML = (pve.guests || []).map((g) => {
      const net = (g.netin || 0) + (g.netout || 0);
      return `<li class="${g.status === 'running' ? '' : 'stopped'}">
        <span class="vdot"></span>
        <span class="vmid">${g.vmid}</span>
        <span class="vname">${g.name}</span>
        <span class="vnet">${net > 1024 ? fmtRate(net) : '—'}</span>
      </li>`;
    }).join('');
  }

  function setBar(el, pct) {
    if (!el) return;
    el.style.width = Math.min(Math.max(pct, 0), 100) + '%';
    el.className = pct > 90 ? 'crit' : pct > 75 ? 'warn' : '';
  }

  function renderStore(pve, local) {
    if (local && local.vol_total) {
      const pct = local.vol_used / local.vol_total * 100;
      $('volText').textContent = `${(local.vol_used / 1e9).toFixed(0)} / ${(local.vol_total / 1e9).toFixed(0)} GB · ${pct.toFixed(0)}%`;
      setBar($('volBar'), pct);
    }
    if (pve && pve.disk_total) {
      const pct = pve.disk_used / pve.disk_total * 100;
      $('rootText').textContent = `${(pve.disk_used / 1e9).toFixed(0)} / ${(pve.disk_total / 1e9).toFixed(0)} GB · ${pct.toFixed(0)}%`;
      setBar($('rootBar'), pct);
    }
  }

  function renderEvents(list) {
    const ul = $('eventList');
    $('eventHint').textContent = list.length ? list.length + ' 条' : '';
    if (!list.length) {
      ul.innerHTML = '<li class="empty">暂无事件</li>';
      return;
    }
    ul.innerHTML = list.slice(0, 24).map((e) => {
      const d = new Date(e.t * 1000);
      const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      const cls = (e.level || '') + (e.kind === 'ha' ? ' ha' : '');
      return `<li class="${cls}" title="${esc(e.detail || '')}"><span class="et">${hm}</span><span class="ex">${esc(e.text)}</span></li>`;
    }).join('');
  }

  /* ---------------- 终端墙 ---------------- */
  function renderWall(devices, summary) {
    // stale / ikuai_seen 变化也要重画，否则格子会停在旧状态
    const key = state.filter + '#' + devices.map((d) => d.ip + (d.stale ? '~' : '') + (d.ikuai_seen === false ? '?' : '')).join(',');
    if (key === state.wallKey) return;
    state.wallKey = key;

    state.devices = {};
    devices.forEach((d) => { state.devices[d.ip] = d; });

    const groups = {};
    CAT_ORDER.forEach((c) => { groups[c] = []; });
    devices.forEach((d) => {
      if (state.filter !== 'all' && d.cat !== state.filter) return;
      (groups[d.cat] || groups.client).push(d);
    });

    const body = $('wallBody');
    body.innerHTML = CAT_ORDER.map((cat) => {
      const list = groups[cat];
      if (!list.length) return '';
      const color = CAT_COLOR[cat];
      const cells = list.map((d) => {
        const tail = d.ip.split('.').pop();
        // stale = iKuai 列表里刚消失，还在宽限期内；ikuai_seen=false = iKuai 没报但探测可达
        const cls = d.stale ? ' stale' : (d.ikuai_seen === false ? ' unwatched' : '');
        const note = d.stale ? ' · 刚失去在线信号'
          : (d.ikuai_seen === false ? ' · iKuai 未上报，探测可达' : '');
        return `<div class="wall-cell on${cls}" style="--c:${color}" data-ip="${d.ip}" title="${esc(d.display)} · ${d.ip}${note}">
          ${icon(d.icon)}<span class="tail">.${tail}</span>
        </div>`;
      }).join('');
      return `<div class="wall-group">
        <div class="wall-gh"><i style="background:${color}"></i>${CAT_LABEL[cat]}<b>${list.length}</b></div>
        <div class="wall-grid">${cells}</div>
      </div>`;
    }).join('');

    body.querySelectorAll('.wall-cell').forEach((el) => {
      el.addEventListener('click', () => openDrawer(el.dataset.ip));
    });
  }

  function buildFilter(summary) {
    const box = $('wallFilter');
    const cats = ['all'].concat(CAT_ORDER);
    box.innerHTML = cats.map((c) => {
      const label = c === 'all' ? '全部' : CAT_LABEL[c];
      const n = c === 'all' ? (summary ? summary.online : '') : (summary && summary.by_cat ? summary.by_cat[c] : '');
      return `<button class="wf ${c === 'all' ? 'active' : ''}" data-cat="${c}">${label}${n !== '' ? ' ' + n : ''}</button>`;
    }).join('');
    box.querySelectorAll('.wf').forEach((b) => {
      b.addEventListener('click', () => {
        box.querySelectorAll('.wf').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        state.filter = b.dataset.cat;
        state.wallKey = '';
        if (state.snap) renderWall(state.snap.devices || [], state.snap.summary);
      });
    });
  }

  /* ---------------- 设备抽屉 ---------------- */
  function openDrawer(ip) {
    const d = state.devices[ip];
    if (!d) return;
    const color = CAT_COLOR[d.cat] || 'var(--cyan)';
    const items = [
      ['IP 地址', d.ip],
      ['MAC', d.mac],
      ['分类', d.cat_label],
      ['位置', d.zone],
      ['厂商', d.vendor || '—'],
      ['型号', d.model || d.hw || '—'],
      ['接入方式', d.wifi_ap ? (d.wifi_ap + (d.wifi_band ? ' · ' + d.wifi_band : '')) : '有线 / 未知'],
      ['当前下行', fmtRate(d.down)],
      ['当前上行', fmtRate(d.up)],
      ['累计下行', fmtBytes(d.down_total)],
      ['累计上行', fmtBytes(d.up_total)],
      ['今日流量', fmtBytes(d.today_total)],
      ['连接数', String(d.conn)],
      ['首次上线', d.uptime || '—'],
      ['地址分配', d.static ? '静态绑定' : 'DHCP 动态'],
    ];
    if (d.ikuai_seen === false) {
      items.push(['在线信号', 'iKuai 未上报，但 NOC 探测可达']);
    } else if (d.stale) {
      items.push(['在线信号', '刚失去，宽限期内']);
    }
    $('drawerBody').innerHTML = `
      <div class="dr-head">
        <div class="dr-ico" style="--c:${color}">${icon(d.icon)}</div>
        <div>
          <h3>${esc(d.display)}</h3>
          <p>${d.ip} · ${esc(d.mac)}</p>
        </div>
      </div>
      <div class="dr-grid">
        ${items.map(([k, v]) => `<div class="dr-item"><span class="dk">${k}</span><span class="dv">${esc(v)}</span></div>`).join('')}
      </div>
      ${d.note ? `<div class="dr-note">${esc(d.note)}</div>` : ''}
    `;
    $('drawer').classList.add('open');
  }

  /* ==========================================================================
     全屋智能明细（二级面板）
     ========================================================================== */
  const SHEET_TITLE = {
    lights: ['灯光', '哪一盏亮着'],
    openings: ['门窗 · 窗帘', '门磁 / 窗帘开合 / 人体存在'],
    presence: ['人体存在', '各区域有人无人'],
    climate: ['空调', '室温与运行状态'],
    env: ['温湿度', '全屋传感器读数'],
    health: ['健康检查', '异常项清单'],
  };

  function openSheet(kind) {
    state.sheet = kind;
    const meta = SHEET_TITLE[kind] || ['明细', ''];
    $('sheetTitle').textContent = meta[0];
    const body = $('sheetBody');
    body.innerHTML = sheetHTML(kind);
    body.scrollTop = 0;
    $('sheet').classList.add('open');
    refreshSheet();
  }

  function closeSheet() {
    state.sheet = null;
    $('sheet').classList.remove('open');
  }

  // 面板开着的时候跟着数据刷新，但不能把用户滚动位置抖没了
  function refreshSheet() {
    if (!state.sheet || !state.snap) return;
    const body = $('sheetBody');
    const html = sheetHTML(state.sheet);
    if (body.dataset.key === html) return;
    const top = body.scrollTop;
    body.innerHTML = html;
    body.dataset.key = html;
    body.scrollTop = top;
    const meta = SHEET_TITLE[state.sheet] || ['明细', ''];
    $('sheetSub').textContent = meta[1];
  }

  function sheetHTML(kind) {
    const snap = state.snap || {};
    const ha = snap.ha || {};
    const sum = snap.summary || {};
    if (kind === 'lights') return lightsHTML(ha, sum);
    if (kind === 'openings') return openingsHTML(ha, sum);
    if (kind === 'presence') return presenceHTML(ha, sum);
    if (kind === 'climate') return climateHTML(ha);
    if (kind === 'env') return envHTML(ha);
    if (kind === 'health') return healthHTML(snap.health);
    return '<div class="empty-note">暂无数据</div>';
  }

  /* ---- 灯光 ---- */
  function lampHTML(l) {
    const pct = l.pct != null ? l.pct + '%' : '';
    // 面板名常常和 full 是同一个字符串，别在 tooltip 里重复两遍
    const src = [l.full, l.panel].filter((x, i, arr) => x && arr.indexOf(x) === i);
    const tip = [l.area].concat(src, [l.on ? '开' : '关']).join(' · ');
    return `<div class="lamp ${l.on ? 'on' : ''}" title="${esc(tip)}">
      <svg><use href="#i-bulb"></use></svg>
      <span class="ln">${esc(l.name)}</span>
      <span class="lp">${pct}</span>
    </div>`;
  }

  function lightsHTML(ha, sum) {
    const lamps = ha.lamps || [];
    if (!lamps.length) {
      return `<div class="empty-note">HA 未上报灯具实体</div>`;
    }
    const on = sum.lights_on || 0;
    const total = sum.lights_total || 0;
    const areas = (ha.lamp_areas || []).slice();
    const litAreas = areas.filter((a) => a.on > 0).length;

    const groups = {};
    lamps.forEach((l) => { (groups[l.area] = groups[l.area] || []).push(l); });

    const blocks = areas.map((a) => {
      const list = (groups[a.area] || []).slice().sort((x, y) => {
        if (!!y.on !== !!x.on) return (y.on ? 1 : 0) - (x.on ? 1 : 0);
        return String(x.name).localeCompare(String(y.name), 'zh');
      });
      if (!list.length) return '';
      const pct = a.total ? Math.round(a.on / a.total * 100) : 0;
      return `<div class="zone-block">
        <div class="zone-head">
          <b>${esc(a.area)}</b>
          <span class="zn ${a.on ? 'on' : ''}">${a.on}/${a.total}</span>
          <i class="zbar"><u style="width:${pct}%"></u></i>
        </div>
        <div class="lamp-grid">${list.map(lampHTML).join('')}</div>
      </div>`;
    }).join('');

    return `<div class="sheet-sum">
        <div class="ss-item ${on ? 'hot' : ''}"><b>${on}</b><span>盏亮着</span></div>
        <div class="ss-item"><b>${total}</b><span>盏总计</span></div>
        <div class="ss-item ${litAreas ? 'ok' : ''}"><b>${litAreas}</b><span>房间有灯亮</span></div>
      </div>
      <div class="note"><svg><use href="#i-bulb"></use></svg>
        <span>已排除网关/路由/摄像头自带的<b>指示灯</b>和米家<b>灯组</b>，只统计真实灯位。
        开关面板的按键也从 switch 域一并并入。</span></div>
      ${blocks}`;
  }

  /* ---- 门窗 / 窗帘 / 人体 ---- */
  function coversHTML(ha, sum) {
    const covers = ha.covers || [];
    if (!covers.length) return '';
    const open = sum.covers_open || 0;
    const rows = covers.map((c) => {
      const off = c.avail === false;
      const pos = c.pos != null ? c.pos : (c.open ? 100 : 0);
      const st = off ? '离线' : (c.open ? (c.pos != null ? '开 ' + c.pos + '%' : '开') : '已关');
      // 「窗帘 / 纱帘」这种名字离了房间分不清是哪个，补上区域
      const nm = c.name.length < 3 ? c.area + c.name : c.name;
      return `<div class="cov ${c.open && !off ? 'open' : ''}">
        <div class="cv-top">
          <span class="cv-nm">${esc(nm)}</span>
          <span class="cv-st">${st}</span>
        </div>
        <div class="cv-bar"><i style="width:${off ? 0 : pos}%"></i></div>
        <div class="cv-ft">${esc(c.area)}</div>
      </div>`;
    }).join('');
    return `<div class="sec-h"><h4>窗帘</h4><span>${open} / ${covers.length} 开</span></div>
      <div class="cov-list">${rows}</div>`;
  }

  function presenceListHTML(ha) {
    const list = ha.presence || [];
    if (!list.length) return '<div class="empty-note">没有人体存在传感器</div>';
    return `<div class="pres-list">${list.map((p) => `
      <div class="pres ${p.active ? 'on' : ''}">
        <span class="dot"></span>
        <span class="pn">${esc(p.name)}</span>
        <span class="pz">${esc(p.area)}</span>
        <span class="pv">${p.active ? '有人' : '无人'}</span>
      </div>`).join('')}</div>`;
  }

  function openingsHTML(ha, sum) {
    const doors = ha.openings || [];
    const hasDoor = !!sum.has_door_sensor;
    const leaks = (ha.leaks || []).filter((w) => w.wet);

    let head = '';
    if (hasDoor) {
      const openN = sum.openings_on || 0;
      const list = doors.map((o) => `
        <div class="pres ${o.open ? 'on' : ''}">
          <span class="dot"></span>
          <span class="pn">${esc(o.name)}</span>
          <span class="pz">${esc(o.area)}</span>
          <span class="pv">${o.open ? '开' : '关'}</span>
        </div>`).join('');
      head = `<div class="sec-h"><h4>门窗磁</h4><span>${openN} / ${doors.length} 开</span></div>
        <div class="pres-list">${list}</div>`;
    } else {
      head = `<div class="note warn"><svg><use href="#i-alert"></use></svg>
        <span>全屋<b>没有门窗磁传感器</b>（HA 里 0 个 door / window 实体）。
        面板上的“门窗”因此显示为「未接入」——不是坏了。
        想补上：买个米家门窗传感器 2，接入 HA 后这里会自动按房间列出每一扇的开关状态。</span></div>`;
    }

    if (leaks.length) {
      head = `<div class="note crit"><svg><use href="#i-alert"></use></svg>
        <span>${leaks.map((w) => esc(w.name)).join('、')} <b>检测到积水</b></span></div>` + head;
    }

    return head + coversHTML(ha, sum) +
      `<div class="sec-h"><h4>人体存在</h4><span>${sum.presence_on || 0} / ${sum.presence_total || 0} 有人</span></div>
       ${presenceListHTML(ha)}`;
  }

  function presenceHTML(ha, sum) {
    return `<div class="sheet-sum">
        <div class="ss-item ${sum.presence_on ? 'ok' : ''}"><b>${sum.presence_on || 0}</b><span>区域有人</span></div>
        <div class="ss-item"><b>${sum.presence_total || 0}</b><span>传感器数</span></div>
      </div>
      <div class="note"><svg><use href="#i-person"></use></svg>
        <span>毫米波/PIR 存在传感器。它只回答“这个区域有没有人”，不等于门窗开关——
        门窗需要门磁。</span></div>
      ${presenceListHTML(ha)}`;
  }

  /* ---- 空调 ---- */
  const HVAC_CN = {
    cool: '制冷', heat: '制热', dry: '除湿', fan_only: '送风',
    auto: '自动', off: '关闭', heat_cool: '冷暖',
  };

  function climateHTML(ha) {
    const list = ha.climates || [];
    if (!list.length) return '<div class="empty-note">HA 未上报空调实体</div>';
    const rows = list.map((c) => {
      const off = c.state === 'unavailable';
      const running = !off && !c.off;
      const sub = off ? '设备离线'
        : `${HVAC_CN[c.state] || c.state}` +
          (c.mode && c.mode !== c.state ? ' · ' + (HVAC_CN[c.mode] || c.mode) : '') +
          (c.cur != null ? ` · 回风 ${c.cur.toFixed(1)}°` : '');
      return `<div class="ac ${running ? 'on' : ''} ${off ? 'off-dev' : ''}">
        <svg><use href="#i-ac"></use></svg>
        <div class="ac-mid">
          <div class="ac-nm">${esc(c.name)}</div>
          <div class="ac-sub">${esc(c.area)} · ${esc(sub)}</div>
        </div>
        <span class="ac-t">${c.target != null && !off ? c.target.toFixed(0) + '°' : '—'}</span>
      </div>`;
    }).join('');
    return `<div class="sheet-sum">
        <div class="ss-item ${(ha.climates || []).some((c) => !c.off && c.state !== 'unavailable') ? 'ok' : ''}">
          <b>${(ha.climates || []).filter((c) => !c.off && c.state !== 'unavailable').length}</b><span>台运行中</span></div>
        <div class="ss-item"><b>${list.length}</b><span>台空调</span></div>
      </div>
      <div class="ac-list">${rows}</div>`;
  }

  /* ---- 温湿度 ---- */
  function envHTML(ha) {
    const env = ha.env || [];
    const od = ha.outdoor || {};
    const head = `<div class="sheet-sum">
        <div class="ss-item"><b>${env.filter((e) => e.temp != null).length}</b><span>温度传感器</span></div>
        <div class="ss-item"><b>${env.filter((e) => e.hum != null).length}</b><span>湿度传感器</span></div>
        <div class="ss-item"><b>${od.temp != null ? od.temp.toFixed(0) + '°' : '—'}</b><span>室外</span></div>
      </div>`;
    const rows = env.map((e) => `
      <div class="ac">
        <svg><use href="#i-thermo"></use></svg>
        <div class="ac-mid">
          <div class="ac-nm">${esc(e.name)}</div>
          <div class="ac-sub">${e.hum != null ? '湿度 ' + e.hum.toFixed(0) + '%' : '仅温度'}</div>
        </div>
        <span class="ac-t">${e.temp != null ? e.temp.toFixed(1) + '°' : '—'}</span>
      </div>`).join('');
    return head + `<div class="ac-list">${rows || '<div class="empty-note">无传感器</div>'}</div>`;
  }

  /* ---- 健康检查 ---- */
  function healthHTML(health) {
    const h = health || { score: 0, level: 'ok', issues: [] };
    const head = `<div class="sheet-sum">
        <div class="ss-item ${h.level === 'ok' ? 'ok' : h.level === 'warn' ? '' : 'bad'}">
          <b>${h.score}</b><span>健康分</span></div>
        <div class="ss-item"><b>${h.issues.length}</b><span>异常项</span></div>
      </div>`;
    if (!h.issues.length) {
      return head + `<div class="empty-note ok">全部正常，没有任何告警</div>`;
    }
    return head + `<ul class="issue-list">${h.issues.map((i) => `
      <li class="${i.level}"><i></i><span>${esc(i.text)}</span></li>`).join('')}</ul>`;
  }

  $('healthBox').addEventListener('click', () => openSheet('health'));
  $('sheetClose').addEventListener('click', closeSheet);
  $('sheet').addEventListener('click', (e) => {
    if (e.target.id === 'sheet') closeSheet();
  });
  document.querySelectorAll('.sbi.clickable').forEach((el) => {
    el.addEventListener('click', () => openSheet(el.dataset.sheet));
  });

  $('drawerClose').addEventListener('click', () => $('drawer').classList.remove('open'));
  $('drawer').addEventListener('click', (e) => {
    if (e.target.id === 'drawer') $('drawer').classList.remove('open');
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeSheet();
      $('drawer').classList.remove('open');
      return;
    }
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
    if (e.key === 't' || e.key === 'T') {
      applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
    } else if (e.key === 'f' || e.key === 'F') {
      // 挂墙大屏用：全屏，去掉浏览器边框
      toggleFullscreen();
    }
  });

  /* ---------------- 数据接入 ---------------- */
  async function bootstrapHistory() {
    try {
      const r = await fetch('/api/history?minutes=2');
      const rows = await r.json();
      state.series.down = rows.map((x) => x.wan_down);
      state.series.up = rows.map((x) => x.wan_up);
      drawChart();
    } catch (e) { /* 忽略 */ }
  }

  let sse = null;
  function connect() {
    if (sse) sse.close();
    sse = new EventSource('/api/stream');
    sse.onmessage = (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        if (!state.snap) buildFilter(snap.summary);
        render(snap);
      } catch (e) {
        console.error(e);
      }
    };
    sse.onerror = () => {
      sse.close();
      setTimeout(connect, 3000);
    };
  }

  buildTopo();
  bootstrapHistory();
  connect();
  window.addEventListener('resize', drawChart);
})();
