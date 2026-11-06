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
    wallQ: '',
    devices: {},
    sheet: null,
    vpnSites: {},
  };

  /* ---------------- 页签：总览 / 网络 / 算力 / 终端 ----------------
     一屏塞不下所有东西，就按「要不要现在处理」分层：
     总览只回答「现在正常吗」，其余明细各自成页，进来才渲染关注。 */
  const VIEWS = ['overview', 'network', 'compute', 'backup', 'clients', 'security'];
  let currentView = 'overview';

  function setView(v, silent) {
    if (VIEWS.indexOf(v) < 0) v = 'overview';
    currentView = v;
    $('app').setAttribute('data-view', v);
    document.querySelectorAll('.view').forEach((el) => {
      el.classList.toggle('is-on', el.id === 'view-' + v);
    });
    document.querySelectorAll('.tab').forEach((b) => {
      const on = b.dataset.view === v;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    // 终端页本身就是终端墙，底部统计条在那一页是多余的；备份/安防是全屏页，也不需要
    const sb = $('summaryBar');
    if (sb) sb.style.display = (v === 'clients' || v === 'backup' || v === 'security') ? 'none' : '';
    // 摄像头按需拉流：进页面才开始轮询，离开就把所有连接断掉，别让转码空跑
    if (v === 'security') startCameras();
    else stopCameras();
    // 曲线 canvas 在隐藏容器里量不到宽度，切回总览补画一次
    if (v === 'overview') drawChart();
    if (v === 'compute') loadPveTrend();
    if (!silent) {
      try { history.replaceState(null, '', '#' + v); } catch (e) { /* 忽略 */ }
    }
  }

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

  /* 紧凑写法：给双 WAN 那一行的行内速率用，窄面板下也放得下 */
  function fmtRateShort(bps) {
    if (!bps || bps < 1) return '0';
    const u = ['B', 'K', 'M', 'G'];
    let i = 0, v = bps;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    const n = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
    return n + u[i] + '/s';
  }

  /* 更短的写法：给交换机端口条下面的标注用，10 口并排也不挤 */
  function fmtRateMini(bps) {
    if (!bps || bps < 1) return '0';
    const u = ['', 'K', 'M', 'G'];
    let i = 0, v = bps;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + u[i];
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
  //
  // 本布局对应"双运营商 + 一台 H3C 核心交换机 + PVE 承载全部虚拟机"的家庭结构：
  //   光猫A ┐                       ┌─ PVE ─┬─ 群晖 / 飞牛 / OpenWrt ×2 / HA
  //         ├─ iKuai ─ H3C ────────┘
  //   光猫B ┘            └─ GE8 ─ AP 汇聚交换 ─ 4 × AP（VLAN100 无线）
  const NODES = {
    ont1:   { x: 176, y: 58,  w: 196, title: '光猫 A',      sub: 'WAN1',        probe: null },
    ont2:   { x: 432, y: 58,  w: 196, title: '光猫 B',      sub: 'WAN2',        probe: null },
    ikuai:  { x: 304, y: 176, w: 232, title: 'iKuai 主路由', sub: '双 WAN',      probe: 'ikuai' },
    switch: { x: 304, y: 296, w: 252, title: 'H3C 交换机',   sub: 'GE1-8',       probe: null, ports: 10 },
    apsw:   { x: 330, y: 370, w: 156, title: 'AP 汇聚交换',   sub: '',            probe: null, chip: true },
    ap1:    { x: 78,  y: 432, w: 132, title: 'AP 1',        sub: '',            probe: 'ap1' },
    ap2:    { x: 216, y: 432, w: 132, title: 'AP 2',        sub: '',            probe: 'ap2' },
    ap3:    { x: 354, y: 432, w: 132, title: 'AP 3',        sub: '',            probe: 'ap3' },
    ap4:    { x: 492, y: 432, w: 132, title: 'AP 4',        sub: '',            probe: 'ap4' },
    pve:    { x: 786, y: 176, w: 232, title: 'PVE 宿主机',   sub: '',            probe: 'pve' },
    vm1:    { x: 792, y: 286, w: 208, title: 'VM',          sub: '',            probe: 'syno' },
    vm3:    { x: 792, y: 344, w: 208, title: 'VM',          sub: '',            probe: 'openwrt' },
    vm4:    { x: 792, y: 402, w: 208, title: 'VM',          sub: '',            probe: 'openwrt2' },
    vm5:    { x: 792, y: 460, w: 208, title: 'VM',          sub: '',            probe: 'ha' },
    // 飞牛 NAS 走 GE4 直通（PCIe 网卡直通给 PVE，绕过虚拟交换机），不挂在 PVE 业务口 spine 上
    vm2:    { x: 792, y: 518, w: 208, title: 'VM',          sub: '',            probe: 'nas' },
    // VPN 三个分支站点不占机位，直接排在右上角：一块一块的小卡，各自带隧道线
  };
  // 站点卡坐标：x 左边界 / 宽 / 高 / 三行的中心 y（和右侧 PVE 顶部留 12px 间隙）
  const VPN_TILE = { x: 660, w: 300, h: 38, ys: [26, 68, 110] };
  const NH = 54;
  const VM_KEYS = ['vm1', 'vm2', 'vm3', 'vm4', 'vm5'];

  // 站点相关的显示文字 / NAS 地址：由后端从 config.local.json 下发，只应用一次
  let siteApplied = false;
  let NAS_IP = '';
  let VM_NAMES = {};

  function applySiteMeta(site) {
    if (!site || siteApplied) return;
    siteApplied = true;
    NAS_IP = site.nas_ip || '';
    VM_NAMES = site.vm_names || {};
    // VPN 站点名录：隧道地址 → 站点名。只在配置里出现，前端不写死任何办公室名字
    state.vpnSites = site.vpn || {};
    // 站点名：标题栏与浏览器标签都用配置里的名字，避免面板上挂着别人的招牌
    if (site.name) {
      const bn = document.getElementById('brandName');
      if (bn) bn.textContent = site.name;
      // 站点名里已经带 NOC 就别再拼一次，免得标签页出现「xxx NOC · NOC」
      document.title = /NOC/i.test(site.name) ? site.name : site.name + ' · NOC';
    }
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

    // 双运营商：把 wan1 / wan2 标成真实运营商名字（配置里没写就退回 WAN1 / WAN2）
    const wl = site.wan || {};
    const tag1 = document.getElementById('wan1Tag');
    const tag2 = document.getElementById('wan2Tag');
    const tg1 = document.getElementById('tg-wan1');
    const tg2 = document.getElementById('tg-wan2');
    const nm = (k, fb) => {
      const w = wl[k] || {};
      const isp = w.isp || '';
      const line = w.line || '';
      return isp ? (isp + (line ? ' · ' + line : '')) : fb;
    };
    if (tag1) tag1.textContent = (wl.wan1 || {}).isp || 'WAN1';
    if (tag2) tag2.textContent = (wl.wan2 || {}).isp || 'WAN2';
    if (tg1) tg1.textContent = nm('wan1', 'WAN1');
    if (tg2) tg2.textContent = nm('wan2', 'WAN2');

    // 算力页卡片标题：站点相关（可能是地名），写在 config.local.json 的 site.compute
    const ct = site.compute || {};
    const pveT = document.getElementById('pveTitle');
    const fnosT = document.getElementById('fnosTitle');
    const rikuaiT = document.getElementById('rikuaiTitle');
    if (pveT && ct.pve) pveT.textContent = ct.pve;
    if (fnosT && ct.fnos) fnosT.textContent = ct.fnos;
    if (rikuaiT && ct.rikuai) rikuaiT.textContent = ct.rikuai;
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

    const nIK = NODES.ikuai, nSW = NODES.switch, nPVE = NODES.pve;
    const apKeys = ['ap1', 'ap2', 'ap3', 'ap4'];
    // 实测：4 台 AP 并不直接接在 H3C 上，而是都挂在 GE1/0/8 下联的那台 PoE 交换机后面
    const apPort = portXY('switch', 7);
    const BUSB = NODES.apsw.y + 18;     // AP 汇聚线：落在小牌子下方，横线不压牌子上的字

    // AP 分支：GE8 先落到汇聚交换，再由它拉一条横线、各落一根到 4 台 AP
    const apDrops = apKeys.map((k) => {
      const ap = NODES[k];
      return { id: 'lk-' + k, cls: 'lan', d: `M${ap.x},${BUSB} L${ap.x},${ap.y - NH / 2}` };
    });
    const busPaths = [
      { id: 'lk-apsw', cls: 'lan',
        d: `M${apPort.x},${nSW.y + NH / 2} L${apPort.x},${NODES.apsw.y - 11}` },
      { id: 'lk-apsw2', cls: 'lan', flow: false,
        d: `M${NODES.apsw.x},${NODES.apsw.y + 11} L${NODES.apsw.x},${BUSB}` },
      { id: 'lk-bus', cls: 'lan', flow: false,
        d: `M${NODES.ap1.x},${BUSB} L${NODES.ap4.x},${BUSB}` },
    ];

    const paths = [
      // 两条 WAN 分别从对应光猫下来，收敛到 iKuai 顶部
      { id: 'lk-wan1', cls: 'wan', d: `M${NODES.ont1.x},${NODES.ont1.y + NH / 2} L${nIK.x - 18},${nIK.y - NH / 2}` },
      { id: 'lk-wan2', cls: 'wan', d: `M${NODES.ont2.x},${NODES.ont2.y + NH / 2} L${nIK.x + 18},${nIK.y - NH / 2}` },
      // iKuai 到交换机主干
      { id: 'lk-lan', cls: 'lan', d: `M${nIK.x},${nIK.y + NH / 2} L${nIK.x},${nSW.y - NH / 2}` },
      // 交换机到 PVE（trunk，弧形走右侧空白区）
      { id: 'lk-pve', cls: 'lan', d: `M${nSW.x + nSW.w / 2},${nSW.y} C${nSW.x + nSW.w / 2 + 120},${nSW.y} ${nPVE.x - nPVE.w / 2 - 110},${nPVE.y} ${nPVE.x - nPVE.w / 2},${nPVE.y}` },
      // VPN：三个分支站点各拉一条隧道，并到同一根竖管再落回 iKuai 公网侧
      ...VPN_TILE.ys.map((y, i) => (
        { id: 'lk-vpn-' + i, cls: 'tunnel', d: `M${VPN_TILE.x},${y} L612,${y}` }
      )),
      { id: 'lk-vpn', cls: 'tunnel',
        d: `M612,${VPN_TILE.ys[0]} L612,${VPN_TILE.ys[2]} Q612,130 600,130 L398,130 Q386,130 386,142 L386,${nIK.y - NH / 2}` },
      ...busPaths,
      ...apDrops,
      // PVE 业务口（GE7 trunk）→ spine → 四台 VM（群晖 / OpenWrt×2 / HA）
      ...VM_KEYS.filter((k) => k !== 'vm2').map((k) => {
        const vm = NODES[k];
        return { id: 'lk-' + k, cls: 'bypass', arrow: false,
          d: `M${nPVE.x},${nPVE.y + NH / 2} L${nPVE.x},${nPVE.y + NH / 2 + 16} L660,${nPVE.y + NH / 2 + 16} L660,${vm.y} L${vm.x - vm.w / 2},${vm.y}` };
      }),
      // 飞牛 NAS：GE4 直通，独立线路（绕开 PVE 虚拟交换机），路由避开 AP 汇聚区域
      { id: 'lk-fnos', cls: 'lan',
        d: `M${portXY('switch', 3).x},${nSW.y + NH / 2} L${portXY('switch', 3).x},${nSW.y + NH / 2 + 33} L580,${nSW.y + NH / 2 + 33} L580,${NODES.vm2.y} L${NODES.vm2.x - NODES.vm2.w / 2},${NODES.vm2.y}` },
    ];

    for (const p of paths) {
      const base = document.createElementNS(SVG_NS, 'path');
      base.setAttribute('class', 'lnk ' + p.cls);
      base.setAttribute('d', p.d);
      base.setAttribute('id', p.id);
      if (p.cls === 'bypass') base.setAttribute('marker-end', 'url(#tw-arrow)');
      links.appendChild(base);

      if (p.flow !== false) {
        const flow = document.createElementNS(SVG_NS, 'path');
        flow.setAttribute('class', 'flow ' + p.cls);
        flow.setAttribute('d', p.d);
        flow.setAttribute('id', p.id + '-flow');
        flow.style.animationDuration = '2s';
        links.appendChild(flow);
      }
    }

    // 链路文字标注：都放在链路旁边留白处，避免压在线上
    const labels = [
      { x: nIK.x + 14, y: 240, t: 'trunk · VLAN100/200', cls: 'tag', anchor: 'start' },
      { x: 14, y: 340, t: 'VLAN100 无线 · PoE', cls: 'tag', anchor: 'start' },
      { x: 470, y: 226, t: 'GE7 · PVE 业务', cls: 'tag', anchor: 'start' },
      { x: 440, y: 350, t: 'GE4 · 飞牛直通', cls: 'tag', anchor: 'start' },
    ];
    for (const l of labels) {
      const t = document.createElementNS(SVG_NS, 'text');
      t.setAttribute('x', l.x); t.setAttribute('y', l.y);
      t.setAttribute('class', l.cls);
      t.setAttribute('text-anchor', l.anchor);
      t.textContent = l.t;
      if (l.id) t.setAttribute('id', l.id);
      links.appendChild(t);
    }

    for (const [key, n] of Object.entries(NODES)) {
      if (n.chip) continue;                     // 小牌子单独画（见下）
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
        // 端口条画在盒子内部下沿（像交换机前面板），不越界、不压链路
        const left = n.x - n.w / 2, step = n.w / n.ports;
        for (let i = 0; i < n.ports; i++) {
          const cx = left + step * (i + 0.5);
          const port = document.createElementNS(SVG_NS, 'rect');
          port.setAttribute('x', cx - 6);
          port.setAttribute('y', n.y + NH / 2 - 11);
          port.setAttribute('width', 12);
          port.setAttribute('height', 6);
          port.setAttribute('rx', 1.5);
          port.setAttribute('id', 'pt-' + (i + 1));
          port.setAttribute('class', 'portbox');
          g.appendChild(port);
          // 端口实时速率标注：画在端口条正下方，updateTopo 按实测填充
          const rate = mkText(cx, n.y + NH / 2 + 9, 'port-rate', 'middle', '');
          rate.setAttribute('id', 'ptr-' + (i + 1));
          g.appendChild(rate);
        }
      }

      nodesG.appendChild(g);
    }

    // 「链路上一块小牌子」型节点：AP 汇聚交换（无管理地址）、VPN 入口（点开有弹窗）。
    // 它们不占机位、不参与探针，所以单独一轮画，文字 id 给 updateTopo 改。
    for (const [key, n] of Object.entries(NODES)) {
      if (!n.chip) continue;
      const h = n.h || 22;
      const g = document.createElementNS(SVG_NS, 'g');
      g.setAttribute('class', 'node on' + (n.click ? ' clickable' : ''));
      g.setAttribute('id', 'nd-' + key);
      const r = document.createElementNS(SVG_NS, 'rect');
      r.setAttribute('class', 'node-box chip' + (n.vpn ? ' vpn' : ''));
      r.setAttribute('x', n.x - n.w / 2);
      r.setAttribute('y', n.y - h / 2);
      r.setAttribute('width', n.w);
      r.setAttribute('height', h);
      r.setAttribute('rx', h / 2);
      g.appendChild(r);
      const t = mkText(n.x, n.y + 3.5, 'node-title chip-text', 'middle', n.title);
      t.setAttribute('id', 'nt-' + key);
      g.appendChild(t);
      if (n.click) {
        // 牌子的底色是透明的，得显式给整块矩形开命中，否则只有描边那 1px 能点
        r.style.pointerEvents = 'all';
        t.style.pointerEvents = 'none';
        g.addEventListener('click', () => openVpnModal());
      }
      nodesG.appendChild(g);
    }

    const rateWan1 = mkText(NODES.ont1.x, NODES.ont1.y - NH / 2 - 8, 'node-rate', 'middle', '');
    rateWan1.setAttribute('id', 'rt-wan1');
    links.appendChild(rateWan1);

    const rateWan2 = mkText(NODES.ont2.x, NODES.ont2.y - NH / 2 - 8, 'node-rate', 'middle', '');
    rateWan2.setAttribute('id', 'rt-wan2');
    links.appendChild(rateWan2);

    const rateLan = mkText(NODES.ikuai.x + NODES.ikuai.w / 2 + 8, NODES.ikuai.y + 4, 'node-rate', 'start', '');
    rateLan.setAttribute('id', 'rt-lan');
    links.appendChild(rateLan);

    // PVE 业务口（GE7）实时速率：标在交换机→PVE 弧线下方留白处
    const ratePve = mkText(560, 252, 'node-rate', 'middle', '');
    ratePve.setAttribute('id', 'rt-pve');
    links.appendChild(ratePve);

    // 每台 AP 下方显示它的无线终端实时吞吐合计
    for (const key of ['ap1', 'ap2', 'ap3', 'ap4']) {
      const n = NODES[key];
      const t = mkText(n.x, n.y + NH / 2 + 18, 'node-rate', 'middle', '');
      t.setAttribute('id', 'rt-' + key);
      links.appendChild(t);
    }

    // 每台虚拟机右侧显示 CPU% / 已用内存
    for (const key of VM_KEYS) {
      const n = NODES[key];
      const t = mkText(n.x + n.w / 2 + 8, n.y + 4, 'node-rate', 'start', '');
      t.setAttribute('id', 'rt-' + key);
      links.appendChild(t);
    }

    // VPN 分支站点：容器压在最上层，卡片内容由 renderVpnTiles 按配置 + 实测数据填
    const vpnG = document.createElementNS(SVG_NS, 'g');
    vpnG.setAttribute('id', 'vpnTiles');
    svg.appendChild(vpnG);
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
    const wan2 = snap.wan2 || {};
    const lan = snap.lan || {};
    const probes = {};
    (snap.probes || []).forEach((p) => { probes[p.key] = p; });

    // 探针驱动的节点状态
    for (const [key, n] of Object.entries(NODES)) {
      const g = $('nd-' + key);
      if (!g) continue;
      if (!n.probe) continue;
      const p = probes[n.probe];
      g.setAttribute('class', 'node ' + (p && p.up ? 'on' : 'off'));
    }

    // 光猫状态跟随各自 WAN 的线路检测结果（拨号成功且有地址才亮）
    const setOnt = (key, w) => {
      const g = $('nd-' + key);
      if (!g) return;
      const alive = !!(w && (w.ip || w.public_ip)) && w.ok !== false;
      g.setAttribute('class', 'node ' + (alive ? 'on' : 'off'));
    };
    setOnt('ont1', wan);
    setOnt('ont2', wan2);

    // 交换机现在有 SSH 管理通道，状态直接取自采集结果
    const sw = snap.switch || {};
    const swG = $('nd-switch');
    if (swG) swG.setAttribute('class', 'node ' + (sw.ok ? 'on' : 'off'));

    // 链路流速：全部来自实测
    setFlow('lk-wan1', (wan.up || 0) + (wan.down || 0));
    setFlow('lk-wan2', (wan2.up || 0) + (wan2.down || 0));
    setFlow('lk-lan', (lan.up || 0) + (lan.down || 0));

    const pt = {};
    (sw.ports || []).forEach((p) => { pt[p.no] = p; });
    const sum = (p) => (p ? (p.rx || 0) + (p.tx || 0) : 0);

    // GE7 是 PVE 业务 trunk，交换机→PVE 那根弧线的流速就取它
    setFlow('lk-pve', sum(pt[7]));
    const rtPve = $('rt-pve');
    if (rtPve) {
      const p7 = pt[7];
      rtPve.textContent = (p7 && p7.up) ? '↓' + fmtRateMini(p7.rx || 0) + ' ↑' + fmtRateMini(p7.tx || 0) : '';
    }

    // GE4 是飞牛直通，独立线路，流速与速率标注都取它
    setFlow('lk-fnos', sum(pt[4]));

    // AP 汇聚交换的牌子跟着 GE8 亮灭
    const apSwG = $('nd-apsw');
    if (apSwG) apSwG.setAttribute('class', 'node ' + (pt[8] && pt[8].up ? 'on' : 'off'));
    setFlow('lk-apsw', sum(pt[8]));

    // VPN：三个分支站点直接画在拓扑上，在线站点自己那条隧道会流动
    const vpn = snap.vpn || {};
    const vlinks = vpn.links || [];
    const vlink = vlinks.find((l) => l.ok) || vlinks[0];
    const vclients = (vpn.server || {}).clients || [];
    const vrows = vpnSiteRows(vpn);
    renderVpnTiles(vpn, vrows);
    VPN_TILE.ys.forEach((y, i) => {
      setFlow('lk-vpn-' + i, vrows[i] && vrows[i].c ? 900 : 0);
    });
    setFlow('lk-vpn', (vclients.length || (vlink && vlink.ok)) ? 1800 : 0);

    // 端口方块状态 + 速率标注（颜色交给 CSS，主题切换才跟着变）
    for (let i = 1; i <= 10; i++) {
      const box = $('pt-' + i);
      const txt = $('ptr-' + i);
      const p = pt[i];
      if (box) box.setAttribute('class', 'portbox' + (p && p.up ? ' up' : ''));
      if (txt) {
        const total = sum(p);
        txt.textContent = (!p || !p.up || total < 2048) ? '' : fmtRateMini(total);
      }
    }

    // 每台 AP：爱快终端表对无线客户端只给协商速率、不给实时流量，
    // 所以这里显示在线台数；真有流量数据时再补速率
    const aps = {};
    ((snap.miwifi || {}).aps || []).forEach((a) => { if (a.node) aps[a.node] = a; });
    for (const key of ['ap1', 'ap2', 'ap3', 'ap4']) {
      const a = aps[key];
      const t = a ? (a.up || 0) + (a.down || 0) : 0;
      setFlow('lk-' + key, t);
      const rt = $('rt-' + key);
      if (!rt) continue;
      // AP 框只有 132px 宽，只放台数；流量数值留给右侧「无线覆盖」面板
      rt.textContent = a ? a.online + ' 台在线' : '';
    }

    const rtWan1 = $('rt-wan1');
    if (rtWan1) rtWan1.textContent = '↓ ' + fmtRate(wan.down) + '   ↑ ' + fmtRate(wan.up);

    const rtWan2 = $('rt-wan2');
    if (rtWan2) {
      rtWan2.textContent = (wan2.ip || wan2.public_ip)
        ? '↓ ' + fmtRate(wan2.down) + '   ↑ ' + fmtRate(wan2.up) : '';
    }

    const rtLan = $('rt-lan');
    if (rtLan) rtLan.textContent = '↓ ' + fmtRate(lan.down) + '   ↑ ' + fmtRate(lan.up);

    // 虚拟机：链路流速 + 右侧小字。CPU/内存详情在算力页，拓扑这里给网络吞吐。
    // 飞牛（vm2）走 GE4 直通，速率取交换机端口实测；其余 VM 取 PVE guest 的 netin/netout（已折算 B/s）。
    const guests = {};
    ((snap.pve || {}).guests || []).forEach((g) => { guests[g.vmid] = g; });
    const VM_ID = { vm1: 204, vm2: 203, vm3: 100, vm4: 200, vm5: 202 };
    for (const [key, vmid] of Object.entries(VM_ID)) {
      const g = guests[vmid];
      const gEl = $('nd-' + key);
      if (!gEl) continue;
      if (g) gEl.setAttribute('class', 'node ' + (g.status === 'running' ? 'on' : 'off'));
      const rt = $('rt-' + key);
      if (!rt) continue;
      if (!g) { rt.textContent = '—'; continue; }
      if (g.status !== 'running') {
        rt.textContent = '已停止';
        rt.setAttribute('class', 'node-rate bad');
        continue;
      }
      let rate;
      if (key === 'vm2') {
        // 飞牛直通：速率就是 GE4 的收发
        const p4 = pt[4];
        rate = (p4 && p4.up) ? '↓' + fmtRateMini(p4.rx || 0) + ' ↑' + fmtRateMini(p4.tx || 0) : '';
      } else {
        rate = '↓' + fmtRateMini(g.netin || 0) + ' ↑' + fmtRateMini(g.netout || 0);
        setFlow('lk-' + key, (g.netin || 0) + (g.netout || 0));
      }
      rt.textContent = rate || '空闲';
      rt.setAttribute('class', 'node-rate' + ((g.netin + g.netout) > 51200 || (key === 'vm2' && sum(pt[4]) > 51200) ? ' hot' : ''));
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
    const w1 = snap.wan || {};
    const w2 = snap.wan2 || {};
    // 大盘曲线 = 双线合计（联通 + 广电），单线速率在各自那一行里显示
    state.series.down.push((w1.down || 0) + (w2.down || 0));
    state.series.up.push((w1.up || 0) + (w2.up || 0));
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

    // 双 WAN：每行显示各自实时速率，大盘显示双线合计
    const w1 = snap.wan || {};
    const w2 = snap.wan2 || null;
    const totDown = (w1.down || 0) + (w2 ? (w2.down || 0) : 0);
    const totUp = (w1.up || 0) + (w2 ? (w2.up || 0) : 0);
    $('wanDownBig').textContent = fmtRate(totDown);
    $('wanUpBig').textContent = fmtRate(totUp);
    $('wanUp').textContent = fmtRate(totUp);
    $('wanDown').textContent = fmtRate(totDown);

    const r1 = $('wan1Rate');
    if (r1) {
      r1.innerHTML = (w1.public_ip || w1.ip)
        ? '↑ <b>' + fmtRateShort(w1.up) + '</b> · ↓ <b>' + fmtRateShort(w1.down) + '</b>'
        : '';
    }
    const st1 = $('wan1State');
    if (st1) {
      const ok1 = w1.ok !== false;
      st1.textContent = ok1 ? '' : '断线';
      st1.className = 'wan2-state ' + (ok1 ? 'ok' : 'bad');
    }

    // 双 WAN：WAN2 副线路（有数据才显示）
    const w2row = $('wan2Row');
    if (w2row) {
      if (w2 && w2.ip) {
        w2row.hidden = false;
        $('wan2Ip').textContent = w2.ip;
        const ok2 = w2.ok !== false;
        const st2 = $('wan2State');
        // 正常时不占位（头部的 PPPoE 徽标已经说明了），只有断线才红字提示
        st2.textContent = ok2 ? '' : '断线';
        st2.className = 'wan2-state ' + (ok2 ? 'ok' : 'bad');
        $('wan2Up').textContent = fmtRateShort(w2.up);
        $('wan2Down').textContent = fmtRateShort(w2.down);
      } else {
        w2row.hidden = true;
      }
    }

    $('onlineCount').textContent = snap.summary ? snap.summary.online : '—';
    $('knownCount').textContent = '/' + (snap.summary ? snap.summary.known : '—');
    $('connCount').textContent = (ik.conn_total || 0).toLocaleString();

    renderStatusBand(snap);
    renderVPN(snap.vpn);
    renderPorts(snap.switch);
    renderWifi(snap.miwifi);
    renderProbes(snap.probes || []);
    renderPVE(snap.pve);
    renderFnos(snap.fnos);
    renderRemoteIkuai(snap.rikuai);
    renderIkuai(snap.ikuai, snap.ifaces, snap.wan_check);
    renderStore(snap.pve, snap.local);
    renderEvents(snap.events || []);
    renderWall(snap.devices || [], snap.summary);
    renderSummary(snap.summary);
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

    // 室内固定读客厅（ZM1）：HA 里叫「温度」的实体有几十个，按数组顺序抓第一个
    // 会抓到群晖磁盘 68°C，所以顺序由后端按 config 里的房间表定好，这里只认客厅
    const room = env.find((e) => e.name === '客厅') || env.find((e) => e.temp != null) || env[0];
    setTxt('sbTemp', room && room.temp != null ? room.temp.toFixed(1) + '°' : '—');
    setTxt('sbHum', room ? (room.hum != null ? room.hum.toFixed(0) + '% · ' + room.name : room.name) : '无传感器');
    setTip('sbiEnv', envTip(room));
    setSbi('sbiEnv', room && room.stale ? 'dim' : '');

    const od = ha.outdoor || {};
    setTxt('sbOutdoor', od.temp != null ? od.temp.toFixed(1) + '°' : '—');
    // 天气实体挂了退回备用传感器时没有天气现象，就把来源写在这里
    setTxt('sbWeather', od.cond ? (WEATHER_CN[od.cond] || od.cond) : (od.short || (od.src || '').slice(0, 5) || '—'));
    setTip('sbiOut', outTip(od));
    setSbi('sbiOut', od.stale ? 'dim' : '');

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

  // 悬浮看全这个房间的读数：温度湿度 + 空气指标（PM2.5 / 甲醛…），不用点开
  function envTip(room) {
    if (!room) return 'HA 里没有可用的室内传感器';
    const lines = [`${room.name}${room.src ? '（' + room.src + '）' : ''}`];
    if (room.temp != null) lines.push('温度 ' + room.temp.toFixed(1) + '°C');
    if (room.hum != null) lines.push('湿度 ' + room.hum.toFixed(0) + '%');
    (room.extra || []).forEach((x) => lines.push(`${x.k} ${fmtAir(x)}`));
    if (room.stale) lines.push(`（传感器暂时没上报，显示 ${agoText(room.stale)}的读数）`);
    lines.push('', '点击查看各房间');
    return lines.join('\n');
  }

  function outTip(od) {
    if (!od || od.temp == null) return '没有可用的天气实体';
    const bits = ['室外 · ' + (od.src || od.entity || od.name || 'weather')];
    bits.push(`${WEATHER_CN[od.cond] || od.cond || '备用传感器'} ${od.temp.toFixed(1)}°C`);
    if (od.hum != null) bits.push('湿度 ' + od.hum.toFixed(0) + '%');
    if (od.wind != null) bits.push('风 ' + od.wind.toFixed(1) + ' km/h');
    if (od.pressure != null) bits.push('气压 ' + od.pressure.toFixed(0) + ' hPa');
    if (od.dew != null) bits.push('露点 ' + od.dew.toFixed(1) + '°C');
    if (od.stale) bits.push(`（天气实体暂时不可用，显示 ${agoText(od.stale)}的读数）`);
    return bits.join('\n');
  }

  // 多久之前的读数：秒 → 刚到 / N 分钟前 / N 小时前
  function agoText(sec) {
    const s = Math.max(0, Number(sec) || 0);
    if (s < 90) return '刚刚';
    if (s < 5400) return Math.round(s / 60) + ' 分钟前';
    return Math.round(s / 3600) + ' 小时前';
  }

  // 空气质量读数：整数就不带小数，0.04 这种保留两位
  function fmtAir(x) {
    const v = Number(x.v);
    if (!isFinite(v)) return String(x.v);
    const s = Math.abs(v) >= 10 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(2);
    return s + (x.u ? ' ' + x.u : '');
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
  // H3C 只有 8 个 RJ45 电口 + 2 个 SFP；面板只列电口，SFP 状态放到脚注里
  const PORT_GE = [1, 2, 3, 4, 5, 6, 7, 8];

  function renderPorts(sw) {
    const ul = $('portList');
    if (!ul) return;
    const ports = (sw && sw.ports) || [];
    const byNo = {};
    ports.forEach((p) => { byNo[p.no] = p; });
    const maxRate = Math.max(1024 * 64, ...ports.map((p) => (p.rx || 0) + (p.tx || 0)));
    // 网络页是逐口的明细页，10 个口（8 电 + 2 光）全列，别再把 SFP 塞进脚注
    const nos = PORT_GE.concat([9, 10].filter((n) => byNo[n]));

    ul.innerHTML = nos.map((no) => {
      const p = byNo[no] || { no: no, up: false, rx: 0, tx: 0, speed: '' };
      const up = !!p.up;
      const total = (p.rx || 0) + (p.tx || 0);
      // 有流量就至少给 1.2% 宽，否则千分之几的占用会渲染成一个像灰尘的点
      const barW = (v) => (v ? Math.max(Math.min(v / maxRate * 100, 100), 1.2) : 0);
      const rxW = barW(p.rx || 0);
      const txW = barW(p.tx || 0);
      const rateHtml = (!sw || !sw.ok)
        ? ''
        : (up && total > 512
          ? `<b>${fmtRate(p.rx)}</b><i>${fmtRate(p.tx)}</i>`
          : (up ? '<span class="idle">空闲</span>' : '—'));
      const speedTxt = up ? (p.speed || '') : (sw && sw.ok ? 'DOWN' : '—');
      const role = p.role || ('口 ' + no);
      const isSfp = no > 8;
      const linkTxt = p.link_type
        ? p.link_type.toUpperCase() + (p.vlan ? ' ' + p.vlan : (p.pvid ? ' P' + p.pvid : ''))
        : (p.pvid ? 'PVID ' + p.pvid : '—');
      const tip = [`口${no} ${role}`, p.desc ? '设备名 ' + p.desc : '',
        linkTxt, up ? (p.speed || '') : '未连接',
        p.errors ? p.errors + ' 个错误包' : ''].filter(Boolean).join(' · ');
      return `<li class="${up ? 'up' : 'down'}${isSfp ? ' sfp' : ''}" title="${tip}">
        <span class="port-no">${no}</span>
        <span class="port-mid">
          <span class="port-name"><span class="pn-txt">${role}</span><em>${speedTxt}</em></span>
          <span class="port-desc">${esc(p.desc || (up ? '—' : '未接线'))}</span>
        </span>
        <span class="port-vlan">${linkTxt}</span>
        <span class="port-bars"><i class="rx" style="width:${rxW.toFixed(1)}%"></i><i class="tx" style="width:${txW.toFixed(1)}%"></i></span>
        <span class="port-rate ${up && total > 512 ? '' : 'off'}">${rateHtml}</span>
        ${p.errors ? `<span class="port-err">${p.errors}</span>` : '<span class="port-err"></span>'}
      </li>`;
    }).join('');

    const hint = $('portHint');
    if (hint) hint.textContent = (sw && sw.ok) ? (sw.model || 'H3C').replace('H3C ', '') : '—';

    const foot = $('portFoot');
    if (foot) {
      if (sw && sw.ok) {
        const sfpUp = [9, 10].filter((n) => byNo[n] && byNo[n].up).length;
        foot.innerHTML = `<span><b>${sw.up_count}</b>/${sw.total_ports} 链路在线</span>`
          + `<span>SFP+ ${sfpUp ? sfpUp + '/2 使用' : '空'}</span>`
          + (sw.poe_used_w ? `<span>PoE ${sw.poe_used_w} W</span>` : '')
          + `<span>收 / 发</span>`;
      } else {
        foot.innerHTML = sw
          ? '<span style="color:var(--crit)">交换机 SSH 采集失败</span>'
          : '<span style="color:var(--txt-3)">等待交换机数据</span>';
      }
    }
  }

  /* ---------------- VPN 隧道 ---------------- */
  // 站点名：隧道 IP 是主键，账号名兜底（爱快偶尔把会话记在别的 lanip 上）
  function vpnSiteOf(ip, user) {
    const sites = state.vpnSites || {};
    const m = sites[ip];
    if (m && (m.name || m.short)) return m;
    for (const v of Object.values(sites)) {
      if (user && v.user === user) return v;
    }
    return m || {};
  }

  function vpnSiteRows(vpn) {
    const srv = (vpn && vpn.server) || { clients: [] };
    const clients = (srv.clients || []).slice();
    const byIp = {};
    clients.forEach((c) => { if (c.ip) byIp[c.ip] = c; });

    // 先按配置铺：三个站点即使离线也要占一张卡，拓扑才不会时有时无
    const rows = [];
    for (const ip of Object.keys(state.vpnSites || {})) {
      rows.push({ ip, meta: state.vpnSites[ip] || {}, c: byIp[ip] || null });
      delete byIp[ip];
    }
    // 配置里没登记、但确实拨进来的（比如临时账号），照样补一张卡
    for (const c of clients) {
      if (c.ip && byIp[c.ip]) {
        rows.push({ ip: c.ip, meta: {}, c });
        delete byIp[c.ip];
      }
    }
    return rows;
  }

  /* ---------------- VPN 站点卡（拓扑右上角直显，不再是弹窗里的隐藏信息） ---------------- */
  let _vpnTileKey = '';
  function renderVpnTiles(vpn, rowsIn) {
    const g = $('vpnTiles');
    if (!g) return;
    const rows = (rowsIn || vpnSiteRows(vpn)).slice(0, VPN_TILE.ys.length);

    // 只有真正变了才重建，免得每 2 秒把 hover / 悬浮提示全部重置
    const key = rows.map((r) => [
      r.ip, r.c ? 1 : 0, r.meta.name || '', r.meta.gw || '', r.meta.lan || '',
      (r.meta.dev || []).map((d) => d.name + d.ip).join(','),
      r.c ? (r.c.user || '') + '|' + (r.c.peer || '') + '|' + (r.c.since || '') : '',
    ].join('|')).join('~');
    if (key === _vpnTileKey) return;
    _vpnTileKey = key;
    g.innerHTML = '';

    rows.forEach((r, i) => {
      const cy = VPN_TILE.ys[i];
      const on = !!r.c;
      const meta = r.meta || {};
      const name = meta.name || meta.short || (r.c && r.c.user) || '未知站点';
      const l2 = [meta.gw, meta.lan].filter(Boolean).join(' · ') || r.ip;

      const grp = document.createElementNS(SVG_NS, 'g');
      grp.setAttribute('class', 'vtile ' + (on ? 'on' : 'off'));

      const rect = document.createElementNS(SVG_NS, 'rect');
      rect.setAttribute('class', 'vt-box');
      rect.setAttribute('x', VPN_TILE.x);
      rect.setAttribute('y', cy - VPN_TILE.h / 2);
      rect.setAttribute('width', VPN_TILE.w);
      rect.setAttribute('height', VPN_TILE.h);
      rect.setAttribute('rx', 9);
      grp.appendChild(rect);

      const bar = document.createElementNS(SVG_NS, 'rect');
      bar.setAttribute('class', 'vt-bar');
      bar.setAttribute('x', VPN_TILE.x);
      bar.setAttribute('y', cy - VPN_TILE.h / 2 + 1);
      bar.setAttribute('width', 3);
      bar.setAttribute('height', VPN_TILE.h - 2);
      bar.setAttribute('rx', 1.5);
      grp.appendChild(bar);

      grp.appendChild(mkText(VPN_TILE.x + 16, cy - 3, 'vt-name', 'start', name));
      grp.appendChild(mkText(VPN_TILE.x + 16, cy + 12, 'vt-sub', 'start', l2));
      grp.appendChild(mkText(VPN_TILE.x + VPN_TILE.w - 24, cy - 3, 'vt-ip', 'end', r.ip));

      const dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('class', 'vt-dot');
      dot.setAttribute('cx', VPN_TILE.x + VPN_TILE.w - 15);
      dot.setAttribute('cy', cy + 12);
      dot.setAttribute('r', 3);
      grp.appendChild(dot);

      // 悬浮提示放弹窗里才有的细节：设备、账号、对端公网、上线时间
      const tip = document.createElementNS(SVG_NS, 'title');
      const devs = (meta.dev || []).map((d) => d.name + ' ' + d.ip).join('、');
      tip.textContent = name + '\n'
        + (meta.gw ? '网关 ' + meta.gw : '网关未登记') + (meta.lan ? ' · 网段 ' + meta.lan : '') + '\n'
        + (devs ? '网段内 ' + devs + '\n' : '')
        + (on
          ? `账号 ${r.c.user || '—'} · 对端 ${r.c.peer || '—'}${r.c.port ? ':' + r.c.port : ''}`
            + (r.c.since ? ` · 自 ${r.c.since}` : '')
          : '当前离线');
      grp.appendChild(tip);

      grp.addEventListener('click', openVpnModal);
      g.appendChild(grp);
    });
  }

  function renderVPN(vpn) {
    const block = $('vpnBlock');
    if (!block) return;
    const srv = (vpn && vpn.server) || { online: 0, clients: [] };
    const links = (vpn && vpn.links) || [];
    if (!vpn || (!srv.online && !links.length)) { block.hidden = true; }
    else block.hidden = false;

    // 拨入行：站点名比账号名有用得多（谁在哪儿拨进来的，一眼就认出来了）
    const rows = vpnSiteRows(vpn);
    const onlineRows = rows.filter((r) => r.c);
    const sEl = $('vpnSrv');
    if (sEl && !block.hidden) {
      if (!onlineRows.length) {
        sEl.innerHTML = '<b>0</b> 站点在线';
      } else if (onlineRows.length === 1) {
        const r = onlineRows[0];
        const nm = r.meta.short || r.meta.name || (r.c.user || '未知站点');
        sEl.innerHTML = `<b>1</b> 在线 · ${esc(nm)} <i>${esc(r.ip)}</i>`;
      } else {
        const nm = onlineRows.map((r) => r.meta.short || r.meta.name || (r.c.user || r.ip));
        sEl.innerHTML = `<b>${onlineRows.length}</b> 站点 · ${esc(nm.join('·'))}`;
      }
      sEl.title = rows.map((r) => {
        const nm = r.meta.name || r.meta.short || (r.c && r.c.user) || r.ip;
        return `${nm}  ${r.ip}`
          + (r.c && r.c.peer ? ` ← ${r.c.peer}` : '')
          + (r.c ? (r.c.since ? `  · 自 ${r.c.since}` : '  · 在线') : '  · 离线');
      }).join('\n') + '\n\n点击查看三个站点的隧道拓扑';
    }

    const lEl = $('vpnCli');
    if (lEl) {
      const ordered = links.slice().sort((a, b) => (b.ok ? 1 : 0) - (a.ok ? 1 : 0));
      lEl.innerHTML = ordered.length
        ? ordered.map((l) => `${l.iface} <em class="${l.ok ? 'ok' : 'bad'}">${l.ok ? (l.ip && l.ip !== '--' ? l.ip : '已连接') : (l.msg || '断开')}</em>`).join(' · ')
        : '<span style="color:var(--txt-3)">无拨出隧道</span>';
    }

    // 弹窗开着的时候顺带跟着刷新，不用关掉再点开
    const modal = $('vpnModal');
    if (modal && modal.classList.contains('open')) fillVpnModal(vpn);
  }

  /* 弹窗：一个站点一张小拓扑（站点 → 加密隧道 → iKuai 服务端） */
  function fillVpnModal(vpn) {
    const rows = vpnSiteRows(vpn);
    const srv = (vpn && vpn.server) || { clients: [] };
    const links = (vpn && vpn.links) || [];
    const onCount = rows.filter((r) => r.c).length;

    const sub = $('vpnModalSub');
    if (sub) {
      sub.textContent = `${onCount}/${rows.length} 站点在线 · OVPN 加密隧道`
        + (links.length ? ` · 拨出 ${links.filter((l) => l.ok).length}/${links.length}` : '');
    }

    const box = $('vpnSites');
    if (box) box.innerHTML = rows.map(vpnCard).join('');

    const outBox = $('vpnOutBox');
    const outHint = $('vpnOutHint');
    if (outHint) outHint.textContent = links.length ? `${links.filter((l) => l.ok).length} / ${links.length} 条在线` : '未配置';
    if (outBox) {
      outBox.innerHTML = links.length
        ? links.map((l) => `<div class="vo-card ${l.ok ? 'ok' : 'bad'}">
            <span class="vo-if">${esc(l.iface)}</span>
            <span class="vo-ar">→</span>
            <span class="vo-ip">${esc(l.ip && l.ip !== '--' ? l.ip : '—')}</span>
            <span class="vo-gw">网关 ${esc(l.gateway || '—')}</span>
            <span class="vo-st">${esc(l.ok ? (l.msg || '已连接') : (l.msg || '拨号失败'))}</span>
          </div>`).join('')
        : '<div class="vo-empty">没有拨出隧道</div>';
    }
  }

  function vpnCard(row) {
    const c = row.c;
    const meta = row.meta || {};
    const ip = row.ip || '—';
    const name = meta.name || (c && c.user ? c.user + ' 拨入' : (c ? '未登记站点' : '未知站点'));
    const peer = c && c.peer ? c.peer : '';
    const port = c && c.port ? ':' + c.port : '';
    const since = c && c.since ? '自 ' + c.since : '';
    const up = c && c.up_total ? fmtBytes(c.up_total) : '';
    const down = c && c.down_total ? fmtBytes(c.down_total) : '';

    // 站点侧：拨号的那台路由器 + 它的内网网段 + 网段里的其他设备（配置里没写就留空）
    const router = meta.router || '';
    const lan = meta.lan || '';
    const gw = meta.gw || '';
    const devs = meta.dev || [];

    // 左框三行：路由器 / 网关 / 网段；网关没登记就只写网段两行，别硬编 .1
    const hasGw = !!gw;
    const l1 = router || (c ? 'VPN 客户端' : '站点');
    const l2 = hasGw ? gw : lan;
    const l3 = hasGw ? lan : '';
    const y1 = hasGw ? 31 : 40;
    const y2 = hasGw ? 49 : 58;
    const y3 = 67;

    const svg = `<svg class="vs-svg" viewBox="0 0 360 96" preserveAspectRatio="xMidYMid meet">
      <rect class="vs-box" x="2" y="6" width="128" height="76" rx="10"></rect>
      <text class="vs-t" x="66" y="${y1}">${esc(l1)}</text>
      <text class="vs-gw" x="66" y="${y2}">${esc(l2)}</text>
      ${l3 ? `<text class="vs-lan" x="66" y="${y3}">${esc(l3)}</text>` : ''}
      <path class="vs-lnk" d="M130,44 H230"></path>
      <path class="vs-arw" d="M223,39.5 L230,44 L223,48.5"></path>
      <text class="vs-ip" x="180" y="33">${esc(ip)}</text>
      <text class="vs-lbl" x="180" y="70">加密隧道</text>
      <rect class="vs-box" x="230" y="6" width="128" height="76" rx="10"></rect>
      <text class="vs-t" x="294" y="40">iKuai 主路由</text>
      <text class="vs-s" x="294" y="58">OVPN 加密隧道</text>
    </svg>`;

    const bits = [];
    if (peer) bits.push(`<span class="vm-peer">${esc(peer + port)}</span>`);
    if (since) bits.push(`<span>${esc(since)}</span>`);
    if (up || down) bits.push(`<span class="vm-tr">↑ ${esc(up || '0')} · ↓ ${esc(down || '0')}</span>`);
    else bits.push('<span class="vm-tr dim">无流量统计</span>');

    // 网段里的其他设备：只写主机位（网段已经在框里了），完整地址放悬浮提示。
    // 没有别的设备也留这一行，三张卡的结构才对齐、高度才一致
    const devLine = `<div class="vs-dev"><span class="vs-dev-k">网段内</span>${devs.length
      ? devs.map((d) => {
        const tail = d.ip ? '.' + String(d.ip).split('.').pop() : '';
        return `<span class="vs-dev-i" title="${esc(d.ip || '')}">${esc(d.name)} <b>${esc(tail)}</b></span>`;
      }).join('')
      : '<span class="vs-dev-none">无其他设备</span>'}</div>`;

    return `<div class="vs-card ${c ? 'on' : 'off'}">
      <div class="vs-top">
        <span class="vs-dot"></span>
        <span class="vs-name">${esc(name)}</span>
        <span class="vs-badge">${c ? '在线' : '离线'}</span>
      </div>
      ${svg}
      ${devLine}
      <div class="vs-meta">${bits.join('')}</div>
    </div>`;
  }

  function openVpnModal() {
    const modal = $('vpnModal');
    if (!modal) return;
    fillVpnModal(state.snap ? state.snap.vpn : null);
    modal.classList.add('open');
  }

  function closeVpnModal() {
    const modal = $('vpnModal');
    if (modal) modal.classList.remove('open');
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
        a.uplink ? `<span>${a.uplink === 'wired' ? '有线回程' : a.uplink}</span>` : '',
      ].join('');
      // 每台 AP 带上前几台终端，无线面板才看得出挂了什么设备 —— 光写「10 台」没有信息量。
      // 只放 2 台 + 「还有 N 台」，四台 AP 才能在一屏里排完不用滚；SSID 收进 title。
      const cls = (a.clients || []).slice().sort((x, y) => (y.signal || -999) - (x.signal || -999));
      const show = cls.slice(0, 2);
      const rest = cls.length - show.length;
      const listHtml = show.length
        ? `<div class="ap-cl">${show.map((c) => {
            const sig = c.signal > -50 ? 's3' : c.signal > -65 ? 's2' : 's1';
            return `<span class="ap-c" title="${esc(c.name || c.mac)} · ${c.ip} · ${c.dev} ${c.signal || ''}dBm">
              <i class="${sig}"></i><span class="ap-cn">${esc(c.name || c.mac)}</span>
              <em>${c.signal ? c.signal + 'dBm' : ''}</em></span>`;
          }).join('')}${rest > 0 ? `<span class="ap-c more">还有 ${rest} 台接入</span>` : ''}</div>`
        : '<div class="ap-cl"><span class="ap-c more">当前没有终端接入</span></div>';
      return `<div class="ap-item ${a.ok ? '' : 'off'}" title="${esc(a.ssid || '')}">
        <div class="ap-top">
          ${icon('ap').replace('<svg>', '<svg class="ap-ico">')}
          <span class="ap-nm">${a.label} · ${a.model}</span>
          <span class="ap-cnt ${a.online ? '' : 'sp'}">${a.ok ? a.online + ' 台' : '离线'}</span>
        </div>
        ${extra ? `<div class="ap-meta">${extra}</div>` : ''}
        ${listHtml}
      </div>`;
    }).join('');

    const foot = $('apFoot');
    if (foot) {
      const apsOk = aps.filter((a) => a.ok).length;
      const cli = aps.reduce((n, a) => n + (a.online || 0), 0);
      const weak = aps.reduce((n, a) => n + (a.clients || []).filter((c) => c.signal && c.signal < -70).length, 0);
      foot.innerHTML = `<span>${apsOk}/${aps.length} 台 AP 在线</span>`
        + `<span>${cli} 台终端</span>`
        + (weak ? `<span style="color:var(--warn)">${weak} 台信号弱</span>` : '<span>信号均良好</span>');
    }
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
    renderVmTable(pve);
  }

  // 远程飞牛 NAS（经 VPN）：SSH 采回来的 CPU/内存/磁盘/温度/运行时长。
  // 没配置或 VPN 断时 fnos 为 null，卡片显示「离线」。
  function renderFnos(fnos) {
    const badge = $('fnosBadge');
    if (!fnos || !fnos.ok) {
      $('fnosCpu').textContent = '—';
      $('fnosMem').textContent = '—';
      $('fnosMeta').textContent = fnos === null ? '未连接' : '采集失败';
      if (badge) badge.textContent = '离线';
      setBar($('fnosCpuBar'), 0);
      setBar($('fnosMemBar'), 0);
      const dk = $('fnosDisks');
      if (dk) dk.innerHTML = '<div class="sm-row"><span>—</span></div>';
      const nt = $('fnosNote');
      if (nt) nt.textContent = '—';
      return;
    }
    if (badge) badge.textContent = '在线';
    $('fnosCpu').textContent = (fnos.cpu || 0).toFixed(0) + '%';
    const memPct = fnos.mem_total ? (fnos.mem_used / fnos.mem_total * 100) : 0;
    $('fnosMem').textContent = memPct.toFixed(0) + '%';
    setBar($('fnosCpuBar'), fnos.cpu || 0);
    setBar($('fnosMemBar'), memPct);
    const tempStr = fnos.temp != null ? ' · ' + fnos.temp.toFixed(0) + '°C' : '';
    $('fnosMeta').textContent = (fnos.cores || '?') + ' 核 · ' +
      (fnos.mem_used / 1e9).toFixed(1) + '/' + (fnos.mem_total / 1e9).toFixed(0) + 'G' +
      tempStr + ' · 运行 ' + fmtDur(fnos.uptime || 0);
    // 磁盘：根分区 + 数据卷，每个一行带进度条
    const dk = $('fnosDisks');
    if (dk) {
      const ds = fnos.disks || [];
      dk.innerHTML = ds.length ? ds.map((d) => {
        const pct = d.total ? (d.used / d.total * 100) : 0;
        const lbl = d.label || d.mount || '磁盘';
        return '<div class="sm-row"><span>' + esc(lbl) + '</span><b>' +
          (d.used / 1e9).toFixed(0) + '/' + (d.total / 1e9).toFixed(0) + 'G · ' + pct.toFixed(0) + '%</b></div>' +
          '<span class="g-bar"><i style="width:' + Math.min(pct, 100) + '%"></i></span>';
      }).join('') : '<div class="sm-row"><span>无磁盘数据</span></div>';
    }
    const nt = $('fnosNote');
    if (nt) {
      nt.innerHTML = esc(fnos.model || '—') + '<br>' + esc(fnos.kernel || '—') +
        '<br>负载 ' + (fnos.load1 || 0).toFixed(2) + ' · ' + esc(fnos.hostname || fnos.host || '');
    }
  }

  // 远程爱快（VPN 站点）：经 VPN API 采集的远程路由器状态。
  // 卡片展示 CPU/内存/在线数/连接数/实时速率，隧道断时显示「离线」。
  // 远程爱快的网口名映射：VPN 站点通常是 lan1/lan2/wan1/ovpn_xxx
  const RIK_SEG_LABEL = (k) => {
    if (/^wan/i.test(k)) return 'WAN';
    if (/^ovpn|^pptp|^l2tp/i.test(k)) return 'VPN 隧道';
    if (/^lan/i.test(k)) return k.toUpperCase();
    return k;
  };
  let _rikCoreKey = '';
  let _rikStatKey = '';

  function renderRemoteIkuai(rik) {
    const badge = $('rikuaiBadge');
    if (!rik || !rik.ok) {
      $('rikCpu').textContent = '—';
      $('rikMem').textContent = '—';
      $('rikMeta').textContent = rik === null ? '未连接' : '采集失败';
      if (badge) badge.textContent = '离线';
      setBar($('rikCpuBar'), 0);
      setBar($('rikMemBar'), 0);
      $('rikUptime').textContent = '';
      ['rikCores', 'rikStats', 'rikSegs', 'rikWans'].forEach((id) => { const e = $(id); if (e) e.innerHTML = '<span class="ik-empty">—</span>'; });
      const nt = $('rikNote'); if (nt) nt.textContent = '—';
      drawRikChart();
      return;
    }
    if (badge) badge.textContent = '在线';
    const cpu = rik.cpu || 0;
    const memPct = rik.mem_used_pct || 0;
    $('rikCpu').textContent = cpu.toFixed(0) + '%';
    $('rikMem').textContent = memPct.toFixed(0) + '%';
    setBar($('rikCpuBar'), cpu);
    setBar($('rikMemBar'), memPct);

    const cores = rik.cpu_cores || [];
    $('rikUptime').textContent = cores.length ? cores.length + ' 核' : '';
    $('rikMeta').textContent = (rik.mem_used / 1e9).toFixed(1) + '/' + (rik.mem_total / 1e9).toFixed(0) + 'G'
      + ' · ' + (rik.online || 0) + ' 台在线 · ' + (rik.conn_total || 0).toLocaleString() + ' 会话'
      + ' · 运行 ' + fmtDur(rik.uptime || 0);

    // 每核占用
    const ck = cores.map((c) => Math.round(c)).join(',');
    const cbox = $('rikCores');
    if (cbox) {
      if (!cores.length) {
        cbox.innerHTML = '<span class="ik-empty">未上报核心占用</span>';
      } else if (ck !== _rikCoreKey) {
        _rikCoreKey = ck;
        cbox.innerHTML = cores.map((c, i) =>
          `<span class="ik-core${c > 85 ? ' hot' : ''}" title="核心 ${i + 1} · ${c.toFixed(1)}%">`
          + `<span class="ik-ck">核 ${i + 1}</span><em>${c.toFixed(0)}%</em></span>`
        ).join('');
      }
      $('rikPeak').textContent = cores.length ? '峰值 ' + Math.max.apply(null, cores).toFixed(0) + '%' : '—';
    }

    // 四格统计：远程 V3 不分 TCP/UDP，副标题改成线路类型
    const lines = ((rik.wan_check || {}).lines) || [];
    const lineType = lines.map((l) => l.internet).filter(Boolean).join(' · ') || '—';
    const stats = [
      ['会话总数', (rik.conn_total || 0).toLocaleString(), lineType],
      ['在线终端', (rik.online || 0) + ' 台', `有线 ${rik.online_wired || 0} · 无线 ${rik.online_wireless || 0}`],
      ['累计上行', fmtBytes(rik.up_total || 0), '本次运行'],
      ['累计下行', fmtBytes(rik.down_total || 0), '本次运行'],
    ];
    const sk = stats.map((s) => s.join('|')).join('~');
    if (sk !== _rikStatKey && $('rikStats')) {
      _rikStatKey = sk;
      $('rikStats').innerHTML = stats.map((s) => `<div class="iks">
        <span class="k">${s[0]}</span><b class="v">${esc(s[1])}</b><span class="n">${esc(s[2])}</span>
      </div>`).join('');
    }

    // 网段与隧道：lan/ovpn 口的实时收发（不含 wan，wan 放线路区块）
    const f = rik.ifaces || {};
    const segs = Object.keys(f).filter((k) => !/^wan/i.test(k)).map((k) => ({
      name: RIK_SEG_LABEL(k), ip: (f[k] || {}).ip || '—',
      up: (f[k] || {}).up || 0, down: (f[k] || {}).down || 0,
    }));
    const sbox = $('rikSegs');
    if (sbox) {
      sbox.innerHTML = segs.length ? segs.map((s) => `<div class="ik-seg">
        <span class="ik-sn">${esc(s.name)}</span>
        <span class="ik-sip">${esc(s.ip)}</span>
        <span class="ik-sw">↑ ${fmtRateShort(s.up)} · ↓ ${fmtRateShort(s.down)}</span>
      </div>`).join('') : '<span class="ik-empty">未读到网段</span>';
      $('rikSegNote').textContent = segs.length ? segs.length + ' 个' : '';
    }

    // 线路实时：wan1 + VPN 隧道，每条 IP + 在线状态 + 速率
    const wbox = $('rikWans');
    if (wbox) {
      let td = 0, tu = 0;
      wbox.innerHTML = lines.map((l) => {
        const it = f[l.iface] || {};
        const bad = l.ok === false;
        td += it.down || 0; tu += it.up || 0;
        const tag = RIK_SEG_LABEL(l.iface);
        return `<div class="ik-wan${bad ? ' bad' : ''}">
          <span class="wan2-tag">${esc(tag)}</span>
          <span class="ik-wip">${esc(l.ip || it.ip || '—')}</span>
          ${bad ? '<span class="ik-wbad">断线</span>' : ''}
          <span class="ik-wr">↑ ${fmtRateShort(it.up)} · ↓ ${fmtRateShort(it.down)}</span>
        </div>`;
      }).join('') || '<span class="ik-empty">未读到线路</span>';
      $('rikSum').textContent = '↓ ' + fmtRateShort(td) + ' · ↑ ' + fmtRateShort(tu);
    }

    $('rikNote').innerHTML = '数据源 远程爱快 sysstat / monitor_iface<br>'
      + '经 VPN 隧道采集 · 每 15 秒刷新';
    drawRikChart();
  }

  // 算力页的虚拟机全表：原来右栏只挤了「名字 + 流量」，这里把 CPU / 内存 / 磁盘 / 运行时长
  // 一起摊开。PVE 的 guests 本来就有这些字段，之前是没地方放。
  function renderVmTable(pve) {
    const box = $('vmTable');
    if (!box) return;
    const gs = pve.guests || [];
    const running = gs.filter((g) => g.status === 'running').length;
    const hint = $('vmHint');
    if (hint) hint.textContent = `${running}/${gs.length} 运行中`;
    const note = $('compNote');
    if (note) {
      note.innerHTML = `${esc(pve.model || '—')}<br>${esc(pve.kernel || '—')}`
        + `<br>交换 ${(pve.swap_used / 1e9).toFixed(1)}/${(pve.swap_total / 1e9).toFixed(1)} GB`
        + ` · 运行 ${fmtDur(pve.uptime)}`;
    }
    if (!gs.length) {
      box.innerHTML = '<div class="wall-empty">没有读到虚拟机</div>';
      return;
    }
    box.innerHTML = gs.map((g) => {
      const memPct = g.maxmem ? (g.mem / g.maxmem * 100) : 0;
      const net = (g.netin || 0) + (g.netout || 0);
      const stopped = g.status !== 'running';
      return `<div class="vmt-row ${stopped ? 'stopped' : ''}">
        <span class="vt-id">${g.vmid}</span>
        <span class="vt-name" title="${esc(g.name || '')}"><i></i>${esc(VM_NAMES[g.vmid] || g.name || '—')}</span>
        <span class="vt-kind">${g.kind === 'lxc' ? 'CT' : 'VM'}</span>
        <span class="vt-num vt-cpu">${stopped ? '—' : (g.cpu || 0).toFixed(1) + '%'}</span>
        <span class="vmt-mem">
          <span>${(g.mem / 1e9).toFixed(2)} / ${(g.maxmem / 1e9).toFixed(1)}G · ${memPct.toFixed(0)}%</span>
          <span class="g-bar"><i style="width:${Math.min(memPct, 100)}%"></i></span>
        </span>
        <span class="vt-num">${g.maxdisk ? (g.maxdisk / 1e9).toFixed(0) + 'G' : '—'}</span>
        <span class="vt-net ${net > 1024 ? '' : 'zero'}">${stopped ? '—' : (net > 1024 ? fmtRate(net) : '空闲')}</span>
        <span class="vt-up">${stopped ? '已停' : fmtDur(g.uptime || 0)}</span>
      </div>`;
    }).join('');
  }

  /* 爱快主路由：路由器也是台机器，CPU / 内存 / 会话同样算算力。
     版式跟 PVE 一样，多两样路由器才有的东西：每核占用、双 WAN 实时速率。 */
  let _ikCoreKey = '';
  let _ikStatKey = '';
  // 爱快网口名太技术，面板上换成一眼能懂的说法（未登记的照原样显示）
  const SEG_LABEL = {
    lan1: 'LAN1', vlanwuxian: '无线段', vlanyouxian: '有线段',
    vlanwan: 'WAN 段', sovpn: 'VPN 隧道',
  };

  function renderIkuai(ik, ifaces, wc) {
    if (!ik) return;
    const cpu = ik.cpu || 0;
    const memPct = ik.mem_used_pct || 0;
    $('ikCpu').textContent = cpu.toFixed(0) + '%';
    $('ikMem').textContent = memPct.toFixed(0) + '%';
    setBar($('ikCpuBar'), cpu);
    setBar($('ikMemBar'), memPct);

    const totalGb = (ik.mem_total_kb || 0) / 1048576;
    const usedGb = (ik.mem_used_kb || 0) / 1048576;
    const cores = ik.cpu_cores || [];
    $('ikUptime').textContent = cores.length ? cores.length + ' 核' : '';
    $('ikBadge').textContent = cpu > 0 ? '在线' : '空闲';
    $('ikMeta').textContent = `${usedGb.toFixed(1)} / ${totalGb.toFixed(0)} GB`
      + ` · ${ik.online || 0} 台在线 · ${(ik.conn_total || 0).toLocaleString()} 会话`
      + ` · 运行 ${fmtDur(ik.uptime)}`;

    // 每核柱子：数值没变就不重建，否则每 2 秒把柱子的过渡动画打断一次
    const ck = cores.map((c) => Math.round(c)).join(',');
    const cbox = $('ikCores');
    if (cbox) {
      if (!cores.length) {
        cbox.innerHTML = '<span class="ik-empty">未上报核心占用</span>';
      } else if (ck !== _ikCoreKey) {
        _ikCoreKey = ck;
        // 每核一个数字块：路由常年个位数占用，细条画出来几乎看不见，数字反而直观
        cbox.innerHTML = cores.map((c, i) => {
          return `<span class="ik-core${c > 85 ? ' hot' : ''}" title="核心 ${i + 1} · ${c.toFixed(1)}%">`
            + `<span class="ik-ck">核 ${i + 1}</span>`
            + `<em>${c.toFixed(0)}%</em></span>`;
        }).join('');
      }
      $('ikPeak').textContent = cores.length
        ? '峰值 ' + Math.max.apply(null, cores).toFixed(0) + '%' : '—';
    }

    // 四格：会话 / 终端 / 累计收发。累计是"这次开机以来"，重启会归零
    const stats = [
      ['会话总数', (ik.conn_total || 0).toLocaleString(), `TCP ${ik.conn_tcp || 0} · UDP ${ik.conn_udp || 0}`],
      ['在线终端', (ik.online || 0) + ' 台', `有线 ${ik.online_wired || 0} · 无线 ${ik.online_wireless || 0}`],
      ['累计上行', fmtBytes(ik.up_total), '本次运行'],
      ['累计下行', fmtBytes(ik.down_total), '本次运行'],
    ];
    const sk = stats.map((s) => s.join('|')).join('~');
    if (sk !== _ikStatKey && $('ikStats')) {
      _ikStatKey = sk;
      $('ikStats').innerHTML = stats.map((s) => `<div class="iks">
        <span class="k">${s[0]}</span><b class="v">${esc(s[1])}</b><span class="n">${esc(s[2])}</span>
      </div>`).join('');
    }

    // 双 WAN：每条线各自 IP + 实时速率，断线直接标红
    const f = ifaces || {};
    const lines = (wc && wc.lines) || [];

    // 内网各网段：有线段 / 无线段 / 隧道各跑了多少，从路由器角度看谁在吃带宽
    const segs = Object.keys(f).filter((k) => /^(lan\d|vlan)/.test(k)).map((k) => ({
      name: SEG_LABEL[k] || k,
      ip: (f[k] || {}).ip || '—',
      up: (f[k] || {}).up || 0,
      down: (f[k] || {}).down || 0,
    }));
    const sbox = $('ikSegs');
    if (sbox) {
      sbox.innerHTML = segs.length ? segs.map((s) => `<div class="ik-seg">
        <span class="ik-sn">${esc(s.name)}</span>
        <span class="ik-sip">${esc(s.ip)}</span>
        <span class="ik-sw">↑ ${fmtRateShort(s.up)} · ↓ ${fmtRateShort(s.down)}</span>
      </div>`).join('') : '<span class="ik-empty">未读到网段</span>';
      $('ikSegNote').textContent = segs.length ? segs.length + ' 个' : '';
    }

    const wbox = $('ikWans');
    if (wbox) {
      const keys = ['wan1', 'wan2'];
      let td = 0, tu = 0;
      wbox.innerHTML = keys.map((k, i) => {
        const it = f[k] || {};
        const lm = lines.filter((l) => l.iface === k)[0] || {};
        const bad = lm.ok === false;
        td += it.down || 0; tu += it.up || 0;
        return `<div class="ik-wan${bad ? ' bad' : ''}">
          <span class="wan2-tag">WAN${i + 1}</span>
          <span class="ik-wip">${esc(it.ip || lm.ip || '—')}</span>
          ${bad ? '<span class="ik-wbad">断线</span>' : ''}
          <span class="ik-wr">↑ ${fmtRateShort(it.up)} · ↓ ${fmtRateShort(it.down)}</span>
        </div>`;
      }).join('');
      $('ikSum').textContent = '↓ ' + fmtRateShort(td) + ' · ↑ ' + fmtRateShort(tu);
    }

    $('ikNote').innerHTML = `数据源 爱快 sysstat / monitor_iface<br>`
      + `会话表 ${(ik.conn_total || 0).toLocaleString()} 条 · 每 15 秒刷新`;
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

  /* ---------------- 事件历史（飞牛本地库回溯） ---------------- */
  const HIST = { hours: 24, level: '', q: '', rows: [], timer: null };
  const LV_LABEL = { info: '信息', warn: '告警', crit: '严重' };

  function histDay(t) {
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function histHM(t) {
    const d = new Date(t * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function openHistory() {
    $('hist').classList.add('open');
    loadHistory();
  }

  function closeHistory() {
    $('hist').classList.remove('open');
  }

  async function loadHistory() {
    const qs = new URLSearchParams({
      limit: '400', hours: String(HIST.hours), level: HIST.level, q: HIST.q,
    });
    let rows = [];
    try {
      const r = await fetch('/api/events/history?' + qs.toString());
      rows = await r.json();
    } catch (e) { rows = []; }
    HIST.rows = Array.isArray(rows) ? rows : [];
    renderHist();
    loadTrend();
    loadStoreMeta();
  }

  function renderHist() {
    const ul = $('histList');
    if (!HIST.rows.length) {
      ul.innerHTML = '<li class="hist-empty">这个范围里没有事件</li>';
      return;
    }
    // 按天分组：一天一个小标题，往下才是事件行
    const cnt = {};
    HIST.rows.forEach((e) => { const k = histDay(e.t); cnt[k] = (cnt[k] || 0) + 1; });
    let prev = '';
    const html = [];
    for (const e of HIST.rows) {
      const k = histDay(e.t);
      if (k !== prev) {
        const d = new Date(e.t * 1000);
        html.push(`<li class="hist-day"><span>${k} ${WEEK[d.getDay()]}</span><i>${cnt[k]} 条</i></li>`);
        prev = k;
      }
      const lv = e.level || 'info';
      html.push(`<li class="hist-row ${lv}">
        <span class="hr-t">${histHM(e.t)}</span>
        <span class="hr-lv">${LV_LABEL[lv] || lv}</span>
        <span class="hr-x">${esc(e.text)}</span>
        <span class="hr-d" title="${esc(e.detail || '')}">${esc(e.detail || '')}</span>
      </li>`);
    }
    ul.innerHTML = html.join('');
  }

  async function loadTrend() {
    const step = HIST.hours <= 24 ? 300 : HIST.hours <= 168 ? 1800 : 7200;
    let rows = [];
    try {
      const r = await fetch(`/api/metrics?hours=${HIST.hours}&step=${step}`);
      rows = await r.json();
    } catch (e) { rows = []; }
    drawHistChart(Array.isArray(rows) ? rows : []);
    const span = $('histTrendSpan');
    if (span) {
      span.textContent = HIST.hours <= 24 ? '近 24 小时' : HIST.hours <= 168 ? '近 7 天' : '近 30 天';
    }
    const note = $('histTrendNote');
    if (note) {
      const peak = (rows || []).reduce((m, r) => Math.max(m, (r.down || 0) + (r.up || 0)), 0);
      note.textContent = rows && rows.length ? `峰值 ${fmtRate(peak)} · ${rows.length} 个采样点` : '还没有采样数据';
    }
  }

  async function loadStoreMeta() {
    try {
      const r = await fetch('/api/store');
      const s = await r.json();
      const foot = $('histFoot');
      const meta = $('histMeta');
      if (!s.ok) {
        if (foot) foot.textContent = '事件库未启用，只保留本次运行的实时事件';
        if (meta) meta.textContent = '未连接本地库';
        return;
      }
      const kb = (s.bytes || 0) / 1024;
      const size = kb >= 1024 ? (kb / 1024).toFixed(1) + ' MB' : kb.toFixed(0) + ' KB';
      if (foot) {
        foot.textContent = `库内共 ${s.events.toLocaleString()} 条事件 · ${s.metrics.toLocaleString()} 个指标采样 · ${s.path} · ${size}`;
      }
      if (meta) {
        const since = s.since ? new Date(s.since * 1000) : null;
        meta.textContent = since
          ? `最早记录 ${since.getFullYear()}-${pad(since.getMonth() + 1)}-${pad(since.getDate())} · 当前范围 ${HIST.rows.length} 条`
          : `当前范围 ${HIST.rows.length} 条`;
      }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------------- 算力页：CPU / 内存 24 小时趋势 ---------------- */
  // 指标表里每分钟存了 cpu / mem，这里按 15 分钟聚合画出来。
  // 单看一个「2%」判断不了是常态还是异常，有曲线才知道。
  const PVE_TREND = { rows: [], at: 0 };
  const RIK_TREND = { rows: [], at: 0 };

  async function loadPveTrend(force) {
    const now = Date.now();
    if (!force && now - PVE_TREND.at < 60000) { drawPveChart(); return; }
    PVE_TREND.at = now;
    RIK_TREND.at = now;
    try {
      const r = await fetch('/api/metrics?hours=24&step=900');
      PVE_TREND.rows = (await r.json()) || [];
    } catch (e) {
      PVE_TREND.rows = [];
    }
    // 远程爱快趋势（单独一张表，VPN 断时为空）
    try {
      const rr = await fetch('/api/rmetrics?hours=24&step=900');
      RIK_TREND.rows = (await rr.json()) || [];
    } catch (e) {
      RIK_TREND.rows = [];
    }
    drawPveChart();
  }

  function drawPveChart() {
    const cv = $('pveChart');
    if (!cv) return;
    drawIkChart();
    drawRikChart();
    const rows = PVE_TREND.rows || [];
    const ctx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 300;
    // 高度跟着面板走：算力页把趋势图撑满了卡片（CSS 里是 flex:1）
    const h = cv.clientHeight || 118;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const note = $('pveTrendNote');
    if (note) note.textContent = rows.length ? rows.length + ' 个采样' : '采样不足';

    // 纵轴固定 0-100%，CPU 常年个位数会贴在底下看不见，
    // 所以图例里带上当前值和峰值，曲线看形状、数字看量级
    if (rows.length) {
      const last = rows[rows.length - 1];
      const peak = Math.max(...rows.map((r) => r.cpu || 0));
      const lc = $('ptlCpu'), lm = $('ptlMem'), lp = $('ptlPeak');
      if (lc) lc.textContent = 'CPU ' + (last.cpu || 0).toFixed(1) + '%';
      if (lm) lm.textContent = '内存 ' + (last.mem || 0).toFixed(0) + '%';
      if (lp) lp.textContent = 'CPU 峰值 ' + peak.toFixed(0) + '% · 内存峰值 '
        + Math.max(...rows.map((r) => r.mem || 0)).toFixed(0) + '%';
    }

    if (rows.length < 2) {
      ctx.fillStyle = cssVar('--txt-3');
      ctx.font = '10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('采样还不够，跑一段时间就有曲线', w / 2, h / 2 + 3);
      return;
    }

    const padL = 4, padR = 4, padT = 8, padB = 14;
    const iw = w - padL - padR, ih = h - padT - padB;
    const X = (i) => padL + iw * i / (rows.length - 1);
    const Y = (v) => padT + ih * (1 - Math.min(1, Math.max(0, v || 0) / 100));

    // 纵轴固定 0-100%，三条参考线对应 0 / 50 / 100
    ctx.strokeStyle = cssVar('--grid-line');
    ctx.lineWidth = 1;
    for (let i = 0; i <= 2; i++) {
      const yy = padT + ih * i / 2;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
    }

    const area = (key, color) => {
      ctx.beginPath();
      rows.forEach((r, i) => {
        const px = X(i), py = Y(r[key]);
        if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.lineJoin = 'round';
      ctx.stroke();
      ctx.lineTo(X(rows.length - 1), padT + ih);
      ctx.lineTo(X(0), padT + ih);
      ctx.closePath();
      const g = ctx.createLinearGradient(0, padT, 0, padT + ih);
      g.addColorStop(0, alpha(color, 0.22));
      g.addColorStop(1, alpha(color, 0));
      ctx.fillStyle = g;
      ctx.fill();
    };
    area('mem', cssVar('--violet'));
    area('cpu', cssVar('--cyan'));

    // 首尾时间
    const fmt = (t) => {
      const d = new Date(t * 1000);
      return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    ctx.fillStyle = cssVar('--txt-3');
    ctx.font = '9px ui-monospace, monospace';
    ctx.textAlign = 'left';
    ctx.fillText(fmt(rows[0].t), padL, h - 3);
    ctx.textAlign = 'right';
    ctx.fillText(fmt(rows[rows.length - 1].t), w - padR, h - 3);
  }

  // 路由卡底部的小走势：近 24 小时双线带宽。和 PVE 趋势同一批采样（/api/metrics），
  // 不额外打接口。路由器是全网流量的唯一入口，这张图才是它真正的负载。
  function drawIkChart() {
    const cv = $('ikChart');
    if (!cv) return;
    const w = cv.clientWidth;
    const h = cv.clientHeight;
    if (!w || !h) return;
    const rows = (PVE_TREND.rows || []).filter((r) => r.down || r.up);
    const ctx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const note = $('ikTrendNote');
    if (rows.length < 2) {
      if (note) note.textContent = '采样不足';
      ctx.fillStyle = cssVar('--txt-3');
      ctx.font = '10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('攒够采样后显示走势', w / 2, h / 2 + 3);
      return;
    }

    const dmax = Math.max.apply(null, rows.map((r) => r.down || 0));
    const umax = Math.max.apply(null, rows.map((r) => r.up || 0));
    const top = Math.max(dmax, umax, 1024);        // 全静默时也别除零
    const padL = 2, padR = 2, padT = 8, padB = 6;
    const iw = w - padL - padR, ih = h - padT - padB;
    const X = (i) => padL + iw * i / (rows.length - 1);
    const Y = (v) => padT + ih * (1 - Math.min(1, (v || 0) / top));

    const trace = (key) => {
      ctx.beginPath();
      rows.forEach((r, i) => {
        const px = X(i), py = Y(r[key]);
        if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
      });
    };

    // 下行：面积压在下面（量级最大，作为背景）
    trace('down');
    ctx.lineTo(X(rows.length - 1), h - padB);
    ctx.lineTo(X(0), h - padB);
    ctx.closePath();
    ctx.fillStyle = alpha(cssVar('--cyan'), 0.13);
    ctx.fill();

    trace('down');
    ctx.strokeStyle = cssVar('--cyan');
    ctx.lineWidth = 1.4;
    ctx.lineJoin = 'round';
    ctx.stroke();

    trace('up');
    ctx.strokeStyle = cssVar('--ok');
    ctx.lineWidth = 1.1;
    ctx.stroke();

    if (note) {
      note.textContent = '峰值 ↓ ' + fmtRateShort(dmax) + ' · ↑ ' + fmtRateShort(umax);
    }
  }

  // 远程爱快带宽趋势：画法和 drawIkChart 一致，数据来自 /api/rmetrics
  function drawRikChart() {
    const cv = $('rikChart');
    if (!cv) return;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    const rows = (RIK_TREND.rows || []).filter((r) => r.down || r.up);
    const ctx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const note = $('rikTrendNote');
    if (rows.length < 2) {
      if (note) note.textContent = '采样不足';
      ctx.fillStyle = cssVar('--txt-3');
      ctx.font = '10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('攒够采样后显示走势', w / 2, h / 2 + 3);
      return;
    }
    const dmax = Math.max.apply(null, rows.map((r) => r.down || 0));
    const umax = Math.max.apply(null, rows.map((r) => r.up || 0));
    const top = Math.max(dmax, umax, 1024);
    const padL = 2, padR = 2, padT = 8, padB = 6;
    const iw = w - padL - padR, ih = h - padT - padB;
    const X = (i) => padL + iw * i / (rows.length - 1);
    const Y = (v) => padT + ih * (1 - Math.min(1, (v || 0) / top));
    const trace = (key) => {
      ctx.beginPath();
      rows.forEach((r, i) => {
        const px = X(i), py = Y(r[key]);
        if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
      });
    };
    trace('down');
    ctx.lineTo(X(rows.length - 1), h - padB);
    ctx.lineTo(X(0), h - padB);
    ctx.closePath();
    ctx.fillStyle = alpha(cssVar('--cyan'), 0.13);
    ctx.fill();
    trace('down');
    ctx.strokeStyle = cssVar('--cyan');
    ctx.lineWidth = 1.4;
    ctx.lineJoin = 'round';
    ctx.stroke();
    trace('up');
    ctx.strokeStyle = cssVar('--ok');
    ctx.lineWidth = 1.1;
    ctx.stroke();
    if (note) note.textContent = '峰值 ↓ ' + fmtRateShort(dmax) + ' · ↑ ' + fmtRateShort(umax);
  }

  function drawHistChart(rows) {
    const cv = $('histChart');
    if (!cv) return;
    const ctx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 620;
    const h = 84;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    if (!rows || rows.length < 2) {
      ctx.fillStyle = cssVar('--txt-3');
      ctx.font = '10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('采样数据还不够，跑一段时间就有曲线了', w / 2, h / 2 + 3);
      return;
    }

    const padL = 4, padR = 4, padT = 8, padB = 13;
    const iw = w - padL - padR, ih = h - padT - padB;
    const maxV = Math.max(1, ...rows.map((r) => Math.max(r.down || 0, r.up || 0)));
    const X = (i) => padL + iw * i / (rows.length - 1);
    const Y = (v) => padT + ih * (1 - Math.min(1, (v || 0) / maxV));

    ctx.strokeStyle = cssVar('--grid-line');
    ctx.lineWidth = 1;
    for (let i = 0; i <= 2; i++) {
      const yy = padT + ih * i / 2;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
    }

    const line = (key, color) => {
      ctx.beginPath();
      rows.forEach((r, i) => {
        const px = X(i), py = Y(r[key]);
        if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = 'round';
      ctx.stroke();
      ctx.lineTo(X(rows.length - 1), padT + ih);
      ctx.lineTo(X(0), padT + ih);
      ctx.closePath();
      const g = ctx.createLinearGradient(0, padT, 0, padT + ih);
      g.addColorStop(0, alpha(color, 0.2));
      g.addColorStop(1, alpha(color, 0));
      ctx.fillStyle = g;
      ctx.fill();
    };
    line('down', cssVar('--cyan'));
    line('up', cssVar('--ok'));

    // 左中右三个时间刻度
    ctx.fillStyle = cssVar('--txt-3');
    ctx.font = '9px ui-monospace, monospace';
    ctx.textBaseline = 'top';
    const marks = [0, Math.floor(rows.length / 2), rows.length - 1];
    marks.forEach((i, n) => {
      const d = new Date(rows[i].t * 1000);
      const label = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      ctx.textAlign = n === 0 ? 'left' : n === 2 ? 'right' : 'center';
      ctx.fillText(label, X(i), h - 11);
    });
  }

  /* ---------------- 终端统计条 ---------------- */
  // 底部那条：总数 + 分类分布 + 掉线数。点分类直接跳终端页并筛好，不用先跳再点。
  function renderSummary(summary) {
    const s = summary || {};
    const on = $('sumOnline'), kn = $('sumKnown'), off = $('sumOffline');
    if (on) on.textContent = s.online != null ? s.online : '—';
    if (kn) kn.textContent = s.known != null ? '/' + s.known + ' 台' : '';
    if (off) {
      off.innerHTML = s.offline ? `离线 <b>${s.offline}</b>` : '全部在线';
      off.style.color = s.offline ? '' : 'var(--ok)';
    }

    const box = $('sumCats');
    if (!box) return;
    const by = s.by_cat || {};
    const key = CAT_ORDER.map((c) => c + (by[c] || 0)).join(',');
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = CAT_ORDER.map((c) => {
      const n = by[c] || 0;
      if (!n) return '';
      return `<button class="sumcat" data-cat="${c}" title="只看${CAT_LABEL[c]}">
        <i style="background:${CAT_COLOR[c]}"></i>${CAT_LABEL[c]}<b>${n}</b></button>`;
    }).join('');
    box.querySelectorAll('.sumcat').forEach((b) => {
      b.addEventListener('click', () => {
        setView('clients');
        const active = document.querySelector('#wallFilter .wf[data-cat="' + b.dataset.cat + '"]');
        if (active) active.click();
      });
    });

    const tn = $('tabCliN');
    if (tn) tn.textContent = s.online != null ? String(s.online) : '';
    const tw = $('tabNetN');
    if (tw) {
      const sw = (state.snap || {}).switch || {};
      tw.textContent = sw.total_ports ? sw.up_count + '/' + sw.total_ports : '';
    }
  }

  /* ---------------- 终端墙 ---------------- */
  // 站点网段判断：VPN 隧道地址与站点内网都由本地配置下发，这里只按 CIDR 归属分组。
  // iKuai 对这类跨站终端只给个 IP，名字全是 Unknown，卡片上得自己补。
  function cidrHas(cidr, ip) {
    const p = String(cidr || '').split('/');
    if (p.length < 2) return false;
    const toInt = (s) => String(s).split('.').reduce((n, x) => (n << 8) + (parseInt(x, 10) || 0), 0) >>> 0;
    const bits = parseInt(p[1], 10);
    if (!bits) return false;
    const mask = bits >= 32 ? 0xffffffff : ((0xffffffff << (32 - bits)) >>> 0);
    return (toInt(ip) & mask) === (toInt(p[0]) & mask);
  }

  function devLabel(d) {
    const raw = String(d.display || '').trim();
    if (raw && !/^(unknown|未知设备|未知终端|—)$/i.test(raw)) return raw;
    const sites = state.vpnSites || {};
    for (const key of Object.keys(sites)) {
      const s = sites[key] || {};
      const hit = (s.dev || []).filter((x) => x.ip === d.ip)[0];
      if (hit) return (s.short || s.name) + ' · ' + hit.name;
      if (d.ip === key) return (s.short || s.name) + (d.user ? ' · ' + d.user : '');
      if (s.lan && cidrHas(s.lan, d.ip)) return (s.short || s.name) + ' 内网设备';
    }
    return '未知终端';
  }

  // 卡片第三行：接在哪 / 是什么 + 当前速率（速率每轮重画，其它字段随列表重建）
  const PPP_LABEL = { ovpn: 'OpenVPN 拨入', pptp: 'PPTP 拨入', l2tp: 'L2TP 拨入', dhcp: 'DHCP' };

  function devMeta(d) {
    if (d.stale) return '暂时失联 · 等待探活';
    if (d.ikuai_seen === false) return '探活确认在线';
    const bits = [];
    if (d.wifi_ap) bits.push(d.wifi_ap + (d.wifi_band ? ' ' + d.wifi_band : ''));
    else if (d.ppp) bits.push(PPP_LABEL[d.ppp] || d.ppp.toUpperCase());
    else if (d.iface) bits.push('有线');
    // 有实时流量时优先显示速率，位置信息让位（完整信息在悬浮提示和详情里）
    const rate = Math.max(d.down || 0, d.up || 0);
    if (rate > 1024) {
      bits.push((d.down >= d.up ? '↓ ' : '↑ ') + fmtRateShort(rate));
    } else if (!d.wifi_ap && d.zone && d.zone !== '未知') {
      bits.push(d.zone);
    }
    if (!bits.length) bits.push(d.vendor && d.vendor !== 'Unknown' ? d.vendor : '—');
    return bits.join(' · ');
  }

  function renderWall(devices, summary) {
    state.devices = {};
    devices.forEach((d) => { state.devices[d.ip] = d; });

    // stale / ikuai_seen 变化也要重画，否则格子会停在旧状态
    const key = state.filter + '#' + state.wallQ + '#'
      + devices.map((d) => d.ip + (d.stale ? '~' : '') + (d.ikuai_seen === false ? '?' : '') + (d.display || '')).join(',');
    if (key !== state.wallKey) {
      state.wallKey = key;
      buildWall(devices);
    }
    paintRates();
  }

  // 流量每 2 秒就变，但整堵墙不能跟着重建（会打断 hover、滚动、动画），
  // 所以只把第三行文字换掉。
  function paintRates() {
    const body = $('wallBody');
    if (!body) return;
    const cells = body.querySelectorAll('.wall-cell');
    for (let i = 0; i < cells.length; i++) {
      const d = state.devices[cells[i].dataset.ip];
      if (!d) continue;
      const m = cells[i].querySelector('.wc-meta');
      const t = devMeta(d);
      if (m && m.textContent !== t) m.textContent = t;
    }
  }

  function buildWall(devices) {
    const q = state.wallQ.toLowerCase();
    const groups = {};
    CAT_ORDER.forEach((c) => { groups[c] = []; });
    devices.forEach((d) => {
      if (state.filter !== 'all' && d.cat !== state.filter) return;
      if (q) {
        const hay = (devLabel(d) + ' ' + (d.display || '') + ' ' + d.ip + ' '
          + (d.mac || '') + ' ' + (d.name || '') + ' ' + (d.zone || '') + ' '
          + (d.wifi_ap || '') + ' ' + (d.vendor || '')).toLowerCase();
        if (hay.indexOf(q) < 0) return;
      }
      (groups[d.cat] || groups.client).push(d);
    });

    const body = $('wallBody');
    const total = CAT_ORDER.reduce((n, c) => n + groups[c].length, 0);
    if (!total) {
      body.innerHTML = `<div class="wall-empty">没有匹配的终端${q ? '：' + esc(state.wallQ) : ''}</div>`;
      return;
    }
    body.innerHTML = CAT_ORDER.map((cat) => {
      const list = groups[cat];
      if (!list.length) return '';
      const color = CAT_COLOR[cat];
      const cells = list.map((d) => {
        // stale = iKuai 列表里刚消失、探活也没通，还在宽限期内；ikuai_seen=false = iKuai 没报但探活可达
        const cls = d.stale ? ' stale' : (d.ikuai_seen === false ? ' unwatched' : '');
        const note = d.stale ? ' · 暂时失联，探活未通'
          : (d.ikuai_seen === false ? ' · iKuai 在线表未列出，探活确认在线' : '');
        const name = devLabel(d);
        const tip = [name, d.ip, d.mac, d.vendor && d.vendor !== 'Unknown' ? d.vendor : '', note]
          .filter(Boolean).join(' · ');
        return `<div class="wall-cell on${cls}" style="--c:${color}" data-ip="${d.ip}" title="${esc(tip)}">
          <span class="wc-h">${icon(d.icon)}<span class="wc-name">${esc(name)}</span></span>
          <span class="wc-ip">${esc(d.ip)}</span>
          <span class="wc-meta">${esc(devMeta(d))}</span>
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
      ['接入方式', d.wifi_ap ? (d.wifi_ap + (d.wifi_band ? ' · ' + d.wifi_band : ''))
        : (d.ppp ? d.ppp.toUpperCase() + ' 拨入' : '有线 / 未知')],
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
      items.push(['在线信号', 'iKuai 在线表未列出，NOC 探活确认在线']);
    } else if (d.stale) {
      items.push(['在线信号', 'iKuai 未列出且探活未通，宽限期内']);
    }
    if (d.user) items.push(['账号', d.user]);
    $('drawerBody').innerHTML = `
      <div class="dr-head">
        <div class="dr-ico" style="--c:${color}">${icon(d.icon)}</div>
        <div>
          <h3>${esc(devLabel(d))}</h3>
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

  /* ---- 空调 / 浴霸：状态 + 反向控制 ---- */
  const HVAC_CN = {
    cool: '制冷', heat: '制热', dry: '除湿', fan_only: '送风',
    auto: '自动', off: '关闭', heat_cool: '冷暖', idle: '待机',
    heating: '加热中', cooling: '制冷中', drying: '除湿中', fan: '送风中',
  };
  const FAN_CN = {
    auto: '自动', low: '低速', high: '高速', 'medium low': '中低',
    medium: '中速', 'medium high': '中高', null: '—',
    '自动': '自动', '低': '低', '中': '中', '高': '高',
    '一档': '1 档', '二档': '2 档', '三档': '3 档', '四档': '4 档',
    '五档': '5 档', '六档': '6 档', '七档': '7 档',
  };
  const PRESET_CN = {
    eco: '节能', away: '离家', boost: '强劲', sleep: '睡眠', none: '关闭',
    Idle: '待机', Ventilate: '换气', Defog: '除雾', 'Quick Heat': '速热',
  };
  const climateBusy = new Set();     // 防止连点，重绘后仍能保留状态
  const climateLastMode = {};        // 记住关机前的模式，再开机直接回到它
  let climateMsg = null;

  function cnName(v) {
    const s = String(v == null ? '' : v);
    if (HVAC_CN[s]) return HVAC_CN[s];
    if (FAN_CN[s]) return FAN_CN[s];
    if (PRESET_CN[s]) return PRESET_CN[s];
    return /^level\d+$/i.test(s) ? s.slice(5) + ' 档' : s;
  }

  function acBtn(c, act, val, label, cls, disabled) {
    const busy = climateBusy.has(c.id + '|' + act);
    const dis = !c.avail || busy || !!disabled;
    return `<button class="${cls}" data-climate-id="${esc(c.id)}" data-climate-act="${act}"
      data-climate-val="${esc(val == null ? '' : val)}"${dis ? ' disabled' : ''}>${label}</button>`;
  }

  function tempText(v, step) {
    if (v == null) return '—';
    return (Number(step) < 1 ? Number(v).toFixed(1) : Math.round(v)) + '°';
  }

  function climateHTML(ha) {
    const list = ha.climates || [];
    if (!list.length) {
      return '<div class="empty-note">未配置空调实体：在 config.ha.climate_entities 中登记要控制的设备</div>';
    }
    const rows = list.map((c) => {
      const off = !c.avail;
      const running = !off && !c.off;
      const modes = c.hvac_modes || [];
      const presets = (c.preset_modes || []).filter((p) => p && p !== 'none');
      const fans = c.fan_modes || [];
      if (!off && c.hvac_mode && c.hvac_mode !== 'off') climateLastMode[c.id] = c.hvac_mode;
      const boot = climateLastMode[c.id] || modes.find((m) => m === 'auto') || modes[0] || 'auto';
      const step = Number(c.step) || 1;
      const lo = c.min == null ? -Infinity : Number(c.min);
      const hi = c.max == null ? Infinity : Number(c.max);
      const round = (v) => step < 1 ? Math.round(v * 2) / 2 : Math.round(v);
      const cur = Number(c.target);
      const sub = off ? '设备离线'
        : cnName(c.state) +
          (c.mode && c.mode !== c.state ? ' · ' + cnName(c.mode) : '') +
          (c.cur != null ? ` · 回风 ${Number(c.cur).toFixed(1)}°` : '');

      const tempCtl = (c.target == null || off) ? '' : `<div class="ac-row">
          <span class="ac-lb">设定</span>
          ${acBtn(c, 'set_temperature', Math.max(lo, round(cur - step)), '−', 'ac-step', !running || cur <= lo)}
          <b class="ac-set">${tempText(cur, step)}</b>
          ${acBtn(c, 'set_temperature', Math.min(hi, round(cur + step)), '+', 'ac-step', !running || cur >= hi)}
        </div>`;
      const modeCtl = modes.length > 1 ? `<div class="ac-row">
          <span class="ac-lb">模式</span>
          <div class="ac-chips">${modes.map((m) => acBtn(c, 'set_hvac_mode', m,
            cnName(m), 'ac-chip' + (!off && c.hvac_mode === m ? ' sel' : ''), off)).join('')}</div>
        </div>` : '';
      const presetCtl = presets.length > 1 ? `<div class="ac-row">
          <span class="ac-lb">功能</span>
          <div class="ac-chips">${presets.map((p) => acBtn(c, 'set_preset_mode', p,
            cnName(p), 'ac-chip' + (!off && c.preset_mode === p ? ' sel' : ''), off)).join('')}</div>
        </div>` : '';
      const fanCtl = fans.length > 1 ? `<div class="ac-row">
          <span class="ac-lb">风速</span>
          <select class="ac-sel" data-climate-id="${esc(c.id)}"
            data-climate-act="set_fan_mode"${!running ? ' disabled' : ''}>
            ${fans.map((f) => `<option value="${esc(f)}"${c.fan_mode === f ? ' selected' : ''}>${esc(cnName(f))}</option>`).join('')}
          </select>
        </div>` : '';
      const offHint = (!off && !running && (c.target != null || fans.length > 1))
        ? '<div class="ac-hint">关机时温度 / 风速锁定，开机后即可调节</div>' : '';
      const msg = (climateMsg && climateMsg.id === c.id && Date.now() - climateMsg.ts < 6000)
        ? `<div class="ac-msg${climateMsg.bad ? ' bad' : ''}">${esc(climateMsg.text)}</div>` : '';

      return `<div class="ac ${running ? 'on' : ''} ${off ? 'off-dev' : ''}">
        <div class="ac-top">
          <svg><use href="#i-ac"></use></svg>
          <div class="ac-mid">
            <div class="ac-nm">${esc(c.name)}</div>
            <div class="ac-sub">${esc(c.area)} · ${esc(sub)}</div>
          </div>
          <span class="ac-t">${c.target != null && !off ? tempText(cur, step) : '—'}</span>
          ${acBtn(c, 'set_hvac_mode', running ? 'off' : boot, running ? '关' : '开',
            'ac-pwr' + (running ? ' on' : ''), off)}
        </div>
        <div class="ac-ctl">${tempCtl}${modeCtl}${presetCtl}${fanCtl}${offHint}</div>
        ${msg}
      </div>`;
    }).join('');
    return `<div class="sheet-sum">
        <div class="ss-item ${list.some((c) => c.avail && !c.off) ? 'ok' : ''}">
          <b>${list.filter((c) => c.avail && !c.off).length}</b><span>台运行中</span></div>
        <div class="ss-item"><b>${list.length}</b><span>台设备</span></div>
      </div>
      <div class="ac-list">${rows}</div>`;
  }

  async function climateCommand(el) {
    const id = el.dataset.climateId;
    const act = el.dataset.climateAct;
    const value = act === 'set_temperature'
      ? Number(el.dataset.climateVal)
      : (el.tagName === 'SELECT' ? el.value : el.dataset.climateVal);
    const key = id + '|' + act;
    if (!id || !act || climateBusy.has(key)) return;
    climateBusy.add(key);
    climateMsg = null;
    refreshSheet();
    try {
      const res = await fetch('/api/ha/climate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: id, action: act, value }),
      });
      const j = await res.json().catch(() => ({}));
      climateMsg = {
        id, bad: !res.ok || !j.ok, ts: Date.now(),
        text: res.ok && j.ok ? '已下发，等待设备响应…' : (j.error || ('HTTP ' + res.status)),
      };
    } catch (err) {
      climateMsg = { id, bad: true, ts: Date.now(), text: '请求失败：' + err.message };
    } finally {
      climateBusy.delete(key);
      refreshSheet();
    }
  }

  /* ---- 温湿度 / 空气质量 ---- */
  // 读数按房间给：客厅走 ZM1（MQTT）、书房走 Broadlink A2，各房间再带 PM2.5、甲醛这些指标
  // 阈值按国标来：PM2.5 24h 均值 75/115，甲醛 GB/T 18883 限值 0.08 mg/m³
  function airLevel(k, v) {
    if (k === '甲醛') return v >= 0.1 ? 'crit' : (v >= 0.08 ? 'warn' : '');
    if (k === 'PM2.5') return v >= 115 ? 'crit' : (v >= 75 ? 'warn' : '');
    if (k === 'PM10') return v >= 150 ? 'warn' : '';
    if (k === 'CO₂' || k === 'CO2') return v >= 1000 ? 'warn' : '';
    return '';
  }

  function envHTML(ha) {
    const env = ha.env || [];
    const od = ha.outdoor || {};
    const room = env.find((e) => e.name === '客厅') || env[0];
    const head = `<div class="sheet-sum">
        <div class="ss-item"><b>${room && room.temp != null ? room.temp.toFixed(1) + '°' : '—'}</b><span>${room ? esc(room.name) + '温度' : '室内温度'}</span></div>
        <div class="ss-item"><b>${room && room.hum != null ? room.hum.toFixed(0) + '%' : '—'}</b><span>${room ? esc(room.name) + '湿度' : '室内湿度'}</span></div>
        <div class="ss-item"><b>${od.temp != null ? od.temp.toFixed(0) + '°' : '—'}</b><span>室外 ${esc(WEATHER_CN[od.cond] || od.cond || '')}</span></div>
      </div>`;
    if (!env.length) {
      return head + `<div class="empty-note">HA 里没有可用的房间传感器</div>`;
    }
    const cards = env.map((e) => {
      const chips = [
        ['湿度', e.hum != null ? e.hum.toFixed(0) + ' %' : '—', '', e.hum == null],
        ...(e.extra || []).map((x) => [x.k, fmtAir(x), airLevel(x.k, Number(x.v)), false]),
      ].filter((c) => !c[3]).map(([k, v, lv]) =>
        `<span class="env-chip ${lv}">${esc(k)}<b>${esc(v)}</b></span>`).join('')
        + (e.stale ? `<span class="env-chip stale">${agoText(e.stale)}的读数</span>` : '');
      return `<div class="env-card">
        <div class="env-hd">
          <span class="env-nm">${esc(e.name)}</span>
          ${e.src ? `<span class="env-src">${esc(e.src)}</span>` : ''}
          <span class="env-t">${e.temp != null ? e.temp.toFixed(1) : '—'}<i>°C</i></span>
        </div>
        <div class="env-kv">${chips || '<span class="env-chip">仅温度</span>'}</div>
      </div>`;
    }).join('');
    const okv = [
      od.hum != null ? ['湿度', od.hum.toFixed(0) + ' %'] : null,
      od.wind != null ? ['风', od.wind.toFixed(1) + ' km/h'] : null,
      od.pressure != null ? ['气压', od.pressure.toFixed(0) + ' hPa'] : null,
      od.dew != null ? ['露点', od.dew.toFixed(1) + '°'] : null,
      od.uv != null ? ['紫外线', od.uv.toFixed(1)] : null,
    ].filter(Boolean);
    const outCard = `<div class="env-card out">
        <div class="env-hd">
          <span class="env-nm">室外</span>
          <span class="env-src">${esc(od.src || od.entity || 'weather')}</span>
          <span class="env-t">${od.temp != null ? od.temp.toFixed(1) : '—'}<i>°C</i></span>
        </div>
        <div class="env-kv">
          ${od.cond
            ? `<span class="env-chip"><b>${esc(WEATHER_CN[od.cond] || od.cond)}</b></span>`
            : '<span class="env-chip stale">天气实体暂不可用，仅温度</span>'}
          ${okv.map(([k, v]) => `<span class="env-chip">${esc(k)}<b>${esc(v)}</b></span>`).join('')}
          ${od.stale ? `<span class="env-chip stale">${agoText(od.stale)}的读数</span>` : ''}
        </div>
      </div>`;
    return head + `<div class="env-list">${cards}${od.temp != null ? outCard : ''}</div>`;
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
  // 空调控制：委托到面板容器，面板随 SSE 重绘也不会丢事件
  $('sheetBody').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-climate-act]');
    if (btn && btn.tagName !== 'SELECT' && !btn.disabled) climateCommand(btn);
  });
  $('sheetBody').addEventListener('change', (e) => {
    const sel = e.target.closest('select[data-climate-act]');
    if (sel && !sel.disabled) climateCommand(sel);
  });
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

  // VPN 弹窗：拓扑上的牌子 / 左侧「拨入」行都能点开，点遮罩和 × 关掉
  $('vpnModalClose').addEventListener('click', closeVpnModal);
  $('vpnModal').addEventListener('click', (e) => {
    if (e.target.id === 'vpnModal') closeVpnModal();
  });
  const vpnBlockEl = $('vpnBlock');
  if (vpnBlockEl) {
    vpnBlockEl.classList.add('clickable');
    vpnBlockEl.addEventListener('click', openVpnModal);
    vpnBlockEl.title = '点击查看三个站点的隧道拓扑';
  }

  // 事件历史：面板右上角「历史 ›」打开，可按时间窗 / 级别 / 关键字回溯
  $('eventHistory').addEventListener('click', openHistory);
  $('histClose').addEventListener('click', closeHistory);
  $('hist').addEventListener('click', (e) => {
    if (e.target.id === 'hist') closeHistory();
  });
  $('histRange').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    $('histRange').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    HIST.hours = parseInt(b.dataset.h, 10) || 24;
    loadHistory();
  });
  $('histLevel').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    $('histLevel').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    HIST.level = b.dataset.lv || '';
    loadHistory();
  });
  $('histQ').addEventListener('input', (e) => {
    clearTimeout(HIST.timer);
    HIST.timer = setTimeout(() => {
      HIST.q = e.target.value.trim();
      loadHistory();
    }, 320);
  });

  // 页签：点击切页；终端统计条右上角「全部终端 ›」直达终端页
  $('tabs').addEventListener('click', (e) => {
    const b = e.target.closest('.tab');
    if (b) setView(b.dataset.view);
  });
  $('sumMore').addEventListener('click', () => setView('clients'));

  let wallQT = null;
  $('wallQ').addEventListener('input', (e) => {
    clearTimeout(wallQT);
    const v = e.target.value.trim();
    wallQT = setTimeout(() => {
      state.wallQ = v;
      state.wallKey = '';
      if (state.snap) renderWall(state.snap.devices || [], state.snap.summary);
    }, 200);
  });

  window.addEventListener('hashchange', () => {
    setView((location.hash || '#overview').slice(1), true);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeSheet();
      $('drawer').classList.remove('open');
      closeVpnModal();
      closeHistory();
      closeCamFocus();
      return;
    }
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
    // 数字键直接切页，挂墙时用遥控/键盘翻页比找鼠标快；范围跟着页签数走
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= VIEWS.length) {
      setView(VIEWS[n - 1]);
      return;
    }
    if (e.key === 't' || e.key === 'T') {
      applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
    } else if (e.key === 'f' || e.key === 'F') {
      // 挂墙大屏用：全屏，去掉浏览器边框
      toggleFullscreen();
    }
  });

  /* ================= 安防监控 =================
     浏览器播不了 RTSP，画面全走 /cam/ 反代到本机 go2rtc：
       网格 = 每 2 秒一张 JPEG 快照（H265 也只需解一帧，CPU 很轻）
       放大 = fMP4 渐进流（H265 那几路由 go2rtc 转 H264 才播得了）
     离开页面就把 img / video 的连接全部断掉，绝不让转码在后台空跑。 */
  const CAM = { list: [], busy: {}, timer: null, tick: 0, loaded: false,
                paused: false, focus: null, interval: 2000, focusTimer: null };

  async function loadCameras() {
    if (CAM.loaded) return;
    try {
      const r = await fetch('/api/cameras');
      const d = await r.json();
      CAM.list = d.cameras || [];
      CAM.interval = Math.max(600, d.interval || 2000);
      CAM.loaded = true;
      const n = $('tabCamN');
      const live = CAM.list.filter((c) => !c.blank).length;   // 空槽位不计入路数
      if (n) n.textContent = live ? String(live) : '';
      buildCamGrid();
    } catch (e) { /* go2rtc 没起也不拖累其他页 */ }
  }

  function buildCamGrid() {
    const grid = $('camGrid'), empty = $('camEmpty');
    if (!grid) return;
    if (!CAM.list.length) {
      grid.innerHTML = '';
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    grid.innerHTML = CAM.list.map((c, i) => c.blank
      ? `<div class="cam-cell cam-blank" aria-hidden="true"></div>`
      : `
      <div class="cam-cell" data-i="${i}">
        <img class="cam-shot" alt="${esc(c.title)}">
        <div class="cam-top"><span class="cam-no">${pad(i + 1)}</span><span class="cam-state">连接中</span></div>
        <div class="cam-bottom"><span class="cam-name">${esc(c.title)}</span><span class="cam-note">${esc(c.note)}</span></div>
        <div class="cam-fail" hidden>无信号</div>
      </div>`).join('');
    grid.querySelectorAll('.cam-cell:not(.cam-blank)').forEach((el) => {
      el.addEventListener('click', () => openCamFocus(+el.dataset.i));
    });
  }

  function refreshCameras(force) {
    // 放大单路时网格不再拉 6 张快照：省带宽，也让 go2rtc 专心供 fMP4
    if (CAM.focus != null) return;
    if (!CAM.list.length || (CAM.paused && !force)) return;
    CAM.tick++;
    const cells = document.querySelectorAll('.cam-cell');
    CAM.list.forEach((c, i) => {
      const cell = cells[i];
      if (c.blank || !cell || CAM.busy[i]) return;
      CAM.busy[i] = true;
      const shot = cell.querySelector('.cam-shot');
      const st = cell.querySelector('.cam-state');
      const fail = cell.querySelector('.cam-fail');
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        CAM.busy[i] = false;
        cell.classList.toggle('bad', !ok);
        fail.hidden = ok;
        st.textContent = ok ? '在线' : '无信号';
      };
      const pre = new Image();
      pre.onload = () => { shot.src = pre.src; done(true); };
      pre.onerror = () => done(false);
      // &width=640 让 go2rtc 直接吐缩略帧：420KB → 49KB，六路轮询几乎不吃带宽
      pre.src = '/cam/api/frame.jpeg?src=' + encodeURIComponent(c.snap)
        + '&width=640&t=' + CAM.tick;
      // RTSP 源挂了时 go2rtc 可能一直不返回，别让这一格永久卡在「连接中」
      setTimeout(() => done(false), 9000);
    });
  }

  function openCamFocus(i) {
    const c = CAM.list[i];
    if (!c) return;
    closeCamFocus(false);       // 换一路时别顺手刷新整墙
    CAM.focus = i;
    $('camFocusTitle').textContent = c.title;
    $('camFocusMode').textContent = '实时';
    const stage = $('camFocusStage');
    stage.innerHTML = '<video id="camLive" autoplay muted playsinline controls></video>';
    const v = $('camLive');
    v.src = '/cam/api/stream.mp4?src=' + encodeURIComponent(c.live);
    v.play().catch(() => {});
    v.onerror = () => useCamFallback(c);
    // 有些环境 fMP4 不报错但一直黑屏，5 秒没起播就退回快照
    setTimeout(() => { if (CAM.focus === i && v.readyState < 2) useCamFallback(c); }, 5000);
    $('camFocus').classList.add('open');
  }

  function useCamFallback(c) {
    const stage = $('camFocusStage');
    if (!stage || CAM.focus == null) return;
    const v = $('camLive');
    if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    stage.innerHTML = '<img class="cam-fallback" alt="实时画面">';
    $('camFocusMode').textContent = '快照模式';
    const im = stage.querySelector('img');
    const paint = () => {
      if (CAM.focus == null) return;
      im.src = '/cam/api/frame.jpeg?src=' + encodeURIComponent(c.snap)
        + '&width=1280&f=' + Date.now();
    };
    paint();
    clearInterval(CAM.focusTimer);
    CAM.focusTimer = setInterval(paint, 1000);
  }

  function closeCamFocus(refreshGrid) {
    clearInterval(CAM.focusTimer);
    CAM.focusTimer = null;
    const stage = $('camFocusStage');
    if (stage) {
      const v = stage.querySelector('video');
      if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
      stage.querySelectorAll('img').forEach((im) => im.removeAttribute('src'));
      stage.innerHTML = '';
    }
    const box = $('camFocus');
    if (box) box.classList.remove('open');
    CAM.focus = null;
    // 关掉弹窗立刻补一帧，别让网格空着等下一轮轮询
    if (refreshGrid !== false && currentView === 'security' && !CAM.paused) refreshCameras(true);
  }

  async function startCameras() {
    await loadCameras();
    if (CAM.timer) return;
    refreshCameras(true);
    CAM.timer = setInterval(() => refreshCameras(false), CAM.interval);
  }

  function stopCameras() {
    if (CAM.timer) { clearInterval(CAM.timer); CAM.timer = null; }
    closeCamFocus(false);
    // 快照是短连接，保留最后一帧不会留后台流量；清掉 src 反而让切回来时整墙黑屏
  }

  $('camRefresh').addEventListener('click', () => refreshCameras(true));
  $('camPause').addEventListener('click', (e) => {
    CAM.paused = !CAM.paused;
    e.currentTarget.textContent = CAM.paused ? '继续' : '暂停';
    e.currentTarget.classList.toggle('active', CAM.paused);
    if (!CAM.paused) refreshCameras(true);
  });
  $('camFocusClose').addEventListener('click', () => closeCamFocus());
  $('camFocus').addEventListener('click', (e) => {
    if (e.target === $('camFocus')) closeCamFocus();   // 点遮罩空白处也能关
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
  // 顶栏版本号以 /api/health 为准，HTML 里不再写死第二份
  fetch('/api/health').then((r) => r.json()).then((d) => {
    const el = $('appVer');
    if (el && d.version) el.textContent = 'v' + d.version;
  }).catch(() => {});
  // 支持 #network / #compute / #clients 直接落地，方便挂墙时固定某一页
  setView((location.hash || '#overview').slice(1), true);
  bootstrapHistory();
  connect();
  window.addEventListener('resize', drawChart);
})();
