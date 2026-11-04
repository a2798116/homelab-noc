// 用 DOM 桩执行 app.js：主渲染 + 全部二级面板 + 主题切换
const fs = require('fs');

const snap = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const code = fs.readFileSync(process.argv[3], 'utf8');

const errors = [];
const handlers = {};       // id/selector → [fn]
const sheetTriggers = [];  // .sbi.clickable 元素

function makeEl(tag) {
  const el = {
    tagName: tag,
    _id: '',
    textContent: '',
    innerHTML: '',
    className: '',
    width: 0, height: 0,
    clientWidth: 400, clientHeight: 60,
    style: new Proxy({}, { get: () => '', set: () => true }),
    dataset: {},
    children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {},
    addEventListener(ev, fn) { (el._h = el._h || {})[ev] = fn; },
    remove() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return makeEl('div'); },
    requestFullscreen() { return Promise.resolve(); },
    getContext() {
      const noop = () => {};
      return new Proxy({}, {
        get: (t, k) => {
          if (k === 'createLinearGradient') return () => ({ addColorStop: noop });
          if (k === 'measureText') return () => ({ width: 10 });
          return noop;
        },
      });
    },
  };
  return el;
}

const registry = {};
function reg(id) {
  if (!registry[id]) {
    registry[id] = makeEl('div');
    registry[id]._id = id;
  }
  return registry[id];
}

// 状态带里的可点击槽位
['env', 'lights', 'climate', 'openings', 'presence'].forEach((k) => {
  const el = makeEl('div');
  el.className = 'sbi clickable';
  el.dataset.sheet = k;
  el.classList.add('sbi');
  el.classList.add('clickable');
  el.closest = () => el;
  sheetTriggers.push(el);
});
// 让 sbiLights / sbiOpen 这类 id 指到真实槽位（setSbi 会用到）
reg('sbiLights').classList.add('sbi');
reg('sbiOpen').classList.add('sbi');

global.document = {
  getElementById: reg,
  createElement: makeEl,
  createElementNS: (ns, tag) => makeEl(tag),
  addEventListener() {},
  querySelector(sel) {
    if (sel === 'meta[name="theme-color"]') return makeEl('meta');
    return null;
  },
  querySelectorAll(sel) {
    if (sel === '.sbi.clickable') return sheetTriggers;
    return [];
  },
  body: makeEl('body'),
  documentElement: makeEl('html'),
  fullscreenElement: null,
  exitFullscreen() {},
};

global.window = {
  devicePixelRatio: 1,
  addEventListener() {},
  EventSource: function (url) {
    this.url = url;
    this.close = () => {};
    global.__es = this;
  },
};
global.EventSource = global.window.EventSource;
global.fetch = () => Promise.resolve({ json: () => Promise.resolve([]) });
global.setInterval = () => 0;
global.setTimeout = (fn) => { try { fn(); } catch (e) {} return 0; };
global.getComputedStyle = () => ({
  getPropertyValue: (n) => ({
    '--violet': '#9d7cf5', '--cyan': '#35b6f2', '--grid-line': 'rgba(0,0,0,.05)',
  }[n] || ''),
});
global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem(k, v) { this._d[k] = v; },
};

process.on('uncaughtException', (e) => {
  errors.push('uncaught: ' + e.message + '\n' + (e.stack || '').split('\n')[1]);
});

try {
  eval(code);
} catch (e) {
  errors.push('load: ' + e.message + '\n' + (e.stack || '').split('\n').slice(0, 3).join('\n'));
}

function push(s) {
  if (!global.__es || !global.__es.onmessage) { errors.push('EventSource 未初始化'); return; }
  try {
    global.__es.onmessage({ data: JSON.stringify(s) });
  } catch (e) {
    errors.push('render: ' + e.message + '\n' + (e.stack || '').split('\n').slice(0, 4).join('\n'));
  }
}

push(snap);
push(snap);
console.log('✓ 主渲染完成');

console.log('\n--- 状态带 ---');
[['sbTemp', '室内'], ['sbHum', '湿度'], ['sbOutdoor', '室外'],
 ['sbLights', '灯光'], ['sbAc', '空调'], ['sbOpen', '门窗'],
 ['sbPresence', '有人'], ['sbWifi', '无线'], ['sbHome', '在家'],
 ['sbL2', '内网'], ['sbHa', 'HA']].forEach(([id, label]) => {
  const el = registry[id];
  console.log('  ' + label.padEnd(4, ' '), el ? JSON.stringify(el.textContent) : '未访问');
});

console.log('\n--- 二级面板 ---');
const sheetBody = reg('sheetBody');
let okCount = 0;
for (const trig of sheetTriggers) {
  const kind = trig.dataset.sheet;
  sheetBody.dataset.key = '';
  try {
    trig._h.click();
  } catch (e) {
    errors.push('openSheet(' + kind + '): ' + e.message);
    continue;
  }
  const html = sheetBody.innerHTML || '';
  const good = html.length > 60 && !/undefined|\[object Object\]|NaN/.test(html);
  console.log('  ' + (good ? '✓' : '✗'), kind.padEnd(9), html.length + ' 字符');
  if (!good) errors.push('sheet ' + kind + ' 内容异常: ' + html.slice(0, 80));
  else okCount++;
  if (kind === 'lights' && process.env.DUMP) console.log(html);
}
// 健康清单
sheetBody.dataset.key = '';
registry.healthBox._h.click();
console.log('  ✓ health    ' + (sheetBody.innerHTML || '').length + ' 字符');

console.log('\n--- 主题切换 ---');
// 真实页面由 index.html 的内联脚本先定下 data-theme，桩里手动补上
document.documentElement.setAttribute('data-theme', 'dark');
const before = document.documentElement['data-theme'];
registry.themeBtn._h.click();
const mid = document.documentElement['data-theme'];
registry.themeBtn._h.click();
const after = document.documentElement['data-theme'];
console.log('  ' + before + ' → ' + mid + ' → ' + after,
  mirror(mid !== before && after === before) ? '✓' : '✗');
function mirror(b) { return b; }

if (process.env.DUMP) {
  console.log('\n--- lights HTML ---');
  sheetBody.dataset.key = '';
  sheetTriggers.find((t) => t.dataset.sheet === 'lights')._h.click();
  console.log(sheetBody.innerHTML);
  console.log('\n--- openings HTML ---');
  sheetBody.dataset.key = '';
  sheetTriggers.find((t) => t.dataset.sheet === 'openings')._h.click();
  console.log(sheetBody.innerHTML);
}

console.log('\n--- 抽查 ---');
const checks = [
  ['portList', (v) => v.innerHTML.includes('port-no')],
  ['apList', (v) => v.innerHTML.includes('ap-item')],
  ['vmList', (v) => v.innerHTML.includes('<li')],
  ['wallBody', (v) => v.innerHTML.includes('wall-cell')],
  ['eventList', (v) => v.innerHTML.length > 0],
];
for (const [id, fn] of checks) {
  const el = registry[id];
  const ok = el && fn(el);
  console.log('  ' + (ok ? '✓' : '✗'), id);
  if (!ok) errors.push('渲染缺内容: ' + id);
}

if (errors.length) {
  console.log('\n!!! 错误 ' + errors.length + ' 条');
  errors.forEach((e) => console.log('  ' + e));
  process.exit(1);
}
console.log('\n全部通过');
