/* GEOINT Buddy 前端：地图圈选 + SSE 对话 + 图层渲染 */

const $ = (id) => document.getElementById(id);

// ---------- 底图源 ----------
const SOURCES = {
  offline: {
    type: 'raster',
    tiles: ['/tiles/{z}/{x}/{y}.png'],
    tileSize: 256, maxzoom: 18,
    attribution: '离线瓦片 (mbtiles)',
  },
  esri: {
    type: 'raster',
    tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
    tileSize: 256, maxzoom: 18, attribution: 'Esri World Imagery',
  },
  osm: {
    type: 'raster',
    tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
    tileSize: 256, maxzoom: 19, attribution: '© OpenStreetMap',
  },
};

let currentBasemap = 'offline';
const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: { basemap: SOURCES.offline },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#10141c' } },
      { id: 'basemap', type: 'raster', source: 'basemap' },
    ],
  },
  center: [0, 0],
  zoom: 1,
});
map.addControl(new maplibregl.NavigationControl(), 'top-right');
map.addControl(new maplibregl.ScaleControl(), 'bottom-right');
map.boxZoom.disable();   // 默认 Shift+拖拽为框选缩放，与 AOI 圈选冲突，禁用

$('basemap-sel').onchange = (e) => {
  currentBasemap = e.target.value;
  map.getSource('basemap').setTiles
    ? map.getSource('basemap').setTiles(SOURCES[currentBasemap].tiles)
    : location.reload();
};

// ---------- AOI 圈选（Shift + 拖拽，或点「圈选灾区」按钮后拖拽）----------
let aoi = null;          // [minLon,minLat,maxLon,maxLat]
let drawing = false, startPt = null, drawArmed = false;

$('btn-draw').onclick = () => {
  drawArmed = true;
  map.getCanvas().style.cursor = 'crosshair';
  $('aoi-hint').textContent = '在地图上拖拽鼠标圈选待核查区域（ESC 取消）';
};
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && drawArmed) {
    drawArmed = false;
    map.getCanvas().style.cursor = '';
    $('aoi-hint').textContent = '按住 Shift 并拖拽鼠标圈选待核查区域';
  }
});

map.on('load', () => {
  map.addSource('aoi', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'aoi-fill', type: 'fill', source: 'aoi',
    paint: { 'fill-color': '#f5c542', 'fill-opacity': 0.12 },
  });
  map.addLayer({
    id: 'aoi-line', type: 'line', source: 'aoi',
    paint: { 'line-color': '#f5c542', 'line-width': 2, 'line-dasharray': [2, 1.5] },
  });
  map.addSource('results', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'results-fill', type: 'fill', source: 'results',
    filter: ['==', ['geometry-type'], 'Polygon'],
    paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.25 },
  });
  map.addLayer({
    id: 'results-line', type: 'line', source: 'results',
    filter: ['==', ['geometry-type'], 'Polygon'],
    paint: { 'line-color': ['get', 'color'], 'line-width': 1.5 },
  });
  map.addLayer({
    id: 'results-pt', type: 'circle', source: 'results',
    filter: ['==', ['geometry-type'], 'Point'],
    paint: {
      'circle-color': ['get', 'color'],
      'circle-radius': 6,
      'circle-stroke-width': 1.5, 'circle-stroke-color': '#fff',
    },
  });
  map.on('click', 'results-pt', (e) => {
    const p = e.features[0].properties;
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`<b>${p.label || p.ftype || '目标'}</b><br>${p.description || ''}<br>置信度 ${p.confidence ?? '—'}`)
      .addTo(map);
  });
  // DEM 地形统计：点击多边形弹出统计卡（AOI/DEM/研判同范围叠加，需在命中集中找 terrain）
  map.on('click', 'results-fill', (e) => {
    const p = Array.from(e.features).map(f => f.properties).find(pr => pr.ftype === 'terrain');
    if (!p) return;
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`<b>DEM 地形统计</b><br>`
        + `海拔 ${p.elev_min} – ${p.elev_max} m（均值 ${p.elev_mean} m）<br>`
        + `相对高差 ${p.relief} m<br>`
        + `坡度：平均 ${p.slope_mean}°，最大 ${p.slope_max}°<br>`
        + `陡坡（&gt;25°）占比 ${p.steep_pct_over25}%`)
      .addTo(map);
  });
  map.on('mouseenter', 'results-fill', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'results-fill', () => { map.getCanvas().style.cursor = ''; });
  refreshLayers();
});

function emptyFC() { return { type: 'FeatureCollection', features: [] }; }

function bboxPoly(b) {
  return {
    type: 'Feature', properties: {},
    geometry: { type: 'Polygon', coordinates: [[
      [b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]],
    ]] },
  };
}

map.on('mousedown', (e) => {
  if (!e.originalEvent.shiftKey && !drawArmed) return;
  drawing = true; startPt = e.lngLat;
  map.dragPan.disable(); map.getCanvas().style.cursor = 'crosshair';
  e.preventDefault();
});
map.on('mousemove', (e) => {
  if (!drawing) return;
  const b = normBbox(startPt, e.lngLat);
  map.getSource('aoi').setData({ type: 'FeatureCollection', features: [bboxPoly(b)] });
});
map.on('mouseup', (e) => {
  if (!drawing) return;
  drawing = false; drawArmed = false;
  map.dragPan.enable(); map.getCanvas().style.cursor = '';
  $('aoi-hint').textContent = '按住 Shift 并拖拽鼠标圈选待核查区域';
  aoi = normBbox(startPt, e.lngLat);
  // 过小的框视为误触（< 约 10 米）
  if ((aoi[2] - aoi[0]) < 0.0001 || (aoi[3] - aoi[1]) < 0.0001) { aoi = null; return; }
  map.getSource('aoi').setData({ type: 'FeatureCollection', features: [bboxPoly(aoi)] });
  $('btn-new-task').disabled = false;
  addMsg('system', `已圈选 AOI：[${aoi.map(v => v.toFixed(4)).join(', ')}]，点击「新建研判任务」开始。`);
});

function normBbox(a, b) {
  return [Math.min(a.lng, b.lng), Math.min(a.lat, b.lat),
          Math.max(a.lng, b.lng), Math.max(a.lat, b.lat)];
}

$('btn-clear-aoi').onclick = () => {
  aoi = null;
  map.getSource('aoi').setData(emptyFC());
  $('btn-new-task').disabled = true;
};

// ---------- 任务 ----------
let taskId = null;

// ---------- 态势报告导出 ----------
$('btn-report').onclick = async () => {
  if (!taskId) { addMsg('system', '请先选择要导出的研判任务'); return; }
  const btn = $('btn-report');
  btn.disabled = true;
  try {
    const md = await fetch(`/api/tasks/${taskId}/report`).then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.text();
    });
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `态势研判报告_${taskId}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
    addMsg('system', '态势研判报告已生成并下载（Markdown 格式）。');
  } catch (e) {
    addMsg('system', `报告导出失败：${e}`);
  }
  btn.disabled = false;
};

$('btn-new-task').onclick = async () => {
  if (!aoi) return;
  const title = `研判 ${new Date().toLocaleString('zh-CN', { hour12: false })}`;
  const r = await fetch('/api/tasks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, aoi_geojson: bboxPoly(aoi).geometry }),
  }).then(r => r.json());
  taskId = r.id;
  $('btn-send').disabled = false;
  addMsg('system', `任务已创建（${r.id}）。AOI 面积可用自然语言询问，例如：研判圈选区域滑坡风险。`);
  await loadTasks();
  refreshLayers();
};

async function loadTasks() {
  const list = await fetch('/api/tasks').then(r => r.json());
  const sel = $('task-list');
  sel.innerHTML = '<option value="">选择历史任务…</option>';
  for (const t of list) {
    const o = document.createElement('option');
    o.value = t.id; o.textContent = `${t.title} [${t.status}]`;
    sel.appendChild(o);
  }
  sel.value = taskId || '';
}
$('task-list').onchange = (e) => {
  taskId = e.target.value || null;
  $('btn-send').disabled = !taskId;
  $('chat').innerHTML = '';
  if (taskId) loadHistory();
  refreshLayers();
};

async function loadHistory() {
  const msgs = await fetch(`/api/tasks/${taskId}/messages`).then(r => r.json());
  for (const m of msgs) {
    if (m.role === 'tool') continue;
    addMsg(m.role, m.content);
  }
}

// ---------- 聊天（SSE）----------

// 轻量 Markdown 渲染：加粗/斜体/行内代码/代码块/标题/列表/分隔线。
// 先整体 HTML 转义再插入标签，模型输出也不会注入 HTML。
function mdToHtml(md) {
  const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w*])\*([^*\s][^*]*?)\*(?=[^\w*]|$)/g, '$1<em>$2</em>');
  let html = '', listType = null, inCode = false, codeBuf = [];
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  for (const line of String(md).split('\n')) {
    const t = line.trim();
    if (t.startsWith('```')) {
      if (inCode) { html += `<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`; codeBuf = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }
    let m;
    if (!t) { closeList(); continue; }
    if ((m = t.match(/^(#{1,6})\s+(.*)/))) { closeList(); html += `<h4>${inline(m[2])}</h4>`; continue; }
    if (/^([-*_])\s*\1\s*\1[\s\-*_]*$/.test(t)) { closeList(); html += '<hr>'; continue; }
    if ((m = t.match(/^[-*•]\s+(.*)/))) {
      if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
      html += `<li>${inline(m[1])}</li>`; continue;
    }
    if ((m = t.match(/^(\d+)([.、)])\s+(.*)/))) {
      if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
      html += `<li>${inline(m[3])}</li>`; continue;
    }
    closeList();
    html += `<p>${inline(t)}</p>`;
  }
  if (inCode) html += `<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`;
  closeList();
  return html;
}

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (role === 'assistant') div.innerHTML = mdToHtml(text);   // 最终回答渲染 Markdown
  else div.textContent = text;
  $('chat').appendChild(div);
  $('chat').scrollTop = $('chat').scrollHeight;
  return div;
}
function addTrace(text) {
  const div = document.createElement('div');
  div.className = 'msg trace';
  div.textContent = text;
  $('chat').appendChild(div);
  $('chat').scrollTop = $('chat').scrollHeight;
  return div;
}

$('btn-send').onclick = send;
$('msg-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

async function send() {
  const text = $('msg-input').value.trim();
  if (!text || !taskId) return;
  $('msg-input').value = '';
  $('btn-send').disabled = true;
  addMsg('user', text);
  const trace = addTrace('Agent 推理中…');
  let finalDiv = null;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, message: text }),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const ev = JSON.parse(p.slice(6));
        handleEvent(ev, trace);
        if (ev.kind === 'final') {
          if (!finalDiv) finalDiv = addMsg('assistant', ev.content);
        }
      }
    }
  } catch (err) {
    addMsg('system', `请求失败：${err.message}`);
  } finally {
    trace.remove();
    $('btn-send').disabled = false;
    refreshLayers();
    refreshUsage();
  }
}

function handleEvent(ev, trace) {
  switch (ev.kind) {
    case 'step': {
      const phases = {
        plan: '任务规划', reuse_evidence: `复用${ev.evidence || ''}证据`,
        tool: `执行 ${ev.tool || '工具'}`, custom_reason: '自定义补救推理', summary: '证据总结',
      };
      trace.textContent = `步骤 ${ev.step} · ${phases[ev.phase] || ev.phase}…`;
      break;
    }
    case 'round': {
      const eng = ev.usage?.engine_name ? `·${ev.usage.engine_name}` : '';
      trace.textContent = `第 ${ev.round} 次 LLM 调用（${ev.side}${eng}，${ev.usage?.completion_tokens ?? '?'} tok）…`;
      break;
    }
    case 'tool_call':
      addTrace(`〔工具〕${ev.tool} ${JSON.stringify(ev.args)}`);
      if (ev.tool === 'vlm_inspect' && currentEngine === 'gemma4')
        addTrace('Gemma4 为思考模型，影像判读约需 3-5 分钟，请耐心等待…');
      break;
    case 'tool_result': {
      const quality = ev.quality?.status;
      const coverage = Number.isFinite(ev.quality?.coverage)
        ? `，覆盖率 ${(ev.quality.coverage * 100).toFixed(1)}%` : '';
      const qualityText = quality ? `〔质量 ${quality}${coverage}〕` : '';
      const cached = ev.cached ? '（缓存复用）' : `（${ev.elapsed_s}s）`;
      addTrace(`〔结果〕${ev.tool}${cached}${qualityText}${ev.preview}`);
      if (ev.artifacts?.length || ev.tool === 'vlm_inspect' || ev.tool === 'save_layer' || ev.tool === 'terrain_profile')
        refreshLayers();   // 新工具按 artifacts 刷新，保留旧事件兼容
      break;
    }
    case 'tool_error':
      addTrace(`〔失败〕${ev.tool}：${ev.message}`);
      break;
    case 'error':
      addMsg('system', `错误：${ev.message}`);
      break;
  }
}

// ---------- 图层 ----------
const KIND_COLORS = {
  landslide: '#e74c3c', building_damage: '#e67e22', road_blockage: '#9b59b6',
  flood: '#2980b9', barrier_lake: '#6c3483',
  detection: '#e74c3c', aoi: '#f5c542', buffer: '#3498db', marker: '#2ecc71',
  terrain: '#2fa3a0', terrain_point: '#4dc3ff',
};

const layerVisibility = new Map();
let currentLayers = [];

function updateResultLayers() {
  const fc = emptyFC();
  for (const l of currentLayers) {
    if (layerVisibility.get(l.id) === false) continue;
    // geojson 三种形态：FeatureCollection / Feature（如 DEM 统计）/ 裸 geometry
    const g = l.geojson || emptyFC();
    const feats = g.type === 'FeatureCollection' ? g.features
      : g.type === 'Feature' ? [g]
      : [{ type: 'Feature', geometry: g, properties: {} }];
    for (const f of feats) {
      f.properties = f.properties || {};
      // 数值属性兜底（MapLibre 严格类型校验，避免 get 缺省返回 null）
      if (typeof f.properties.confidence !== 'number') f.properties.confidence = 0.5;
      f.properties.color = KIND_COLORS[f.properties.ftype] || KIND_COLORS[l.kind] || '#ccc';
      f.properties.label = l.name;
      fc.features.push(f);
    }
  }
  if (map.getSource('results')) map.getSource('results').setData(fc);
}

async function refreshLayers() {
  const requestedTaskId = taskId;
  const ul = $('layer-list');
  currentLayers = [];
  updateResultLayers();
  if (!requestedTaskId) {
    ul.innerHTML = '<li class="layer-empty">请选择任务以查看对应图层</li>';
    return;
  }

  ul.innerHTML = '<li class="layer-empty">图层加载中…</li>';
  const layers = await fetch(`/api/layers?task_id=${encodeURIComponent(requestedTaskId)}`).then(r => r.json());
  if (requestedTaskId !== taskId) return;  // 忽略切换任务前发出的过期请求
  currentLayers = layers.filter(l => l.task_id === requestedTaskId);
  ul.innerHTML = '';
  if (!currentLayers.length) {
    ul.innerHTML = '<li class="layer-empty">当前任务暂无图层</li>';
    return;
  }

  for (const l of currentLayers) {
    const visible = layerVisibility.get(l.id) !== false;
    const li = document.createElement('li');
    li.classList.toggle('is-hidden', !visible);
    li.innerHTML = `<input type="checkbox" class="layer-visible" data-visible="${l.id}" ${visible ? 'checked' : ''} title="显示或隐藏图层">
      <span class="dot" style="background:${KIND_COLORS[l.kind] || '#ccc'}"></span>
      <span class="layer-name">${l.name}</span>
      <button class="mini" data-del="${l.id}">删除</button>`;
    ul.appendChild(li);
  }
  updateResultLayers();
  ul.querySelectorAll('[data-visible]').forEach(box => {
    box.onchange = () => {
      layerVisibility.set(box.dataset.visible, box.checked);
      box.closest('li').classList.toggle('is-hidden', !box.checked);
      updateResultLayers();
    };
  });
  ul.querySelectorAll('[data-del]').forEach(btn => {
    btn.onclick = async () => {
      const li = btn.closest('li');
      const name = li ? li.querySelector('.layer-name').textContent : '该图层';
      if (!confirm(`确认删除图层「${name}」？删除后不可恢复。`)) return;
      await fetch(`/api/layers/${btn.dataset.del}`, { method: 'DELETE' });
      layerVisibility.delete(btn.dataset.del);
      refreshLayers();
    };
  });
}
$('btn-refresh-layers').onclick = refreshLayers;

// ---------- 网络状态 ----------
$('net-btns').querySelectorAll('button').forEach(btn => {
  btn.onclick = async () => {
    const r = await fetch('/api/network', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: btn.dataset.net }),
    }).then(r => r.json());
    $('net-badge').textContent = r.network_state;
    $('net-badge').className = 'badge ' + r.network_state.toLowerCase();
  };
});

// ---------- 端侧引擎切换 ----------
let currentEngine = 'qwen3vl';
const ENGINE_NAMES = {};

async function loadEngines() {
  try {
    const r = await fetch('/api/engines').then(r => r.json());
    const sel = $('engine-sel');
    sel.innerHTML = '';
    for (const e of r.engines) {
      const o = document.createElement('option');
      o.value = e.id; o.textContent = `端侧·${e.name}`;
      o.title = e.desc;
      sel.appendChild(o);
      ENGINE_NAMES[e.id] = e.name;
    }
    sel.value = r.current;
    currentEngine = r.current;
  } catch (e) { /* 后端旧版无此接口时忽略 */ }
}

$('engine-sel').onchange = async (e) => {
  const r = await fetch('/api/engine', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engine: e.target.value }),
  }).then(r => r.json()).catch(() => ({}));
  if (!r.current) { e.target.value = currentEngine; return; }
  currentEngine = r.current;
  const slow = r.current === 'gemma4' ? '（思考模型，判读约需 3-5 分钟）' : '';
  addMsg('system', `端侧引擎已切换：${ENGINE_NAMES[r.current] || r.current}${slow}`);
};

// ---------- 双引擎对比判读 ----------
$('btn-compare').onclick = async () => {
  if (!taskId) { addMsg('system', '请先新建研判任务并圈选 AOI，再进行双引擎对比'); return; }
  const btn = $('btn-compare');
  btn.disabled = true;
  addMsg('system', '双引擎对比判读进行中：Qwen3VL 与 Gemma4 并行研判同一 AOI。Qwen3VL 约 1-2 分钟，Gemma4 思考模型约 3-5 分钟，请稍候…');
  try {
    const r = await fetch('/api/engines/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, focus: 'general' }),
    }).then(r => r.json());
    if (r.detail) { addMsg('system', `对比失败：${r.detail}`); return; }
    const q = r.qwen3vl, g = r.gemma4;
    const line = (x) => `${x.engine_name}：${x.parse_ok ? `${x.count} 处疑似（${x.elapsed_s}s，${x.completion_tokens} tok）` : '输出解析失败'}`;
    addMsg('system', `双引擎对比完成（AOI ${r.bbox.map(v => v.toFixed(3)).join(', ')}）：\n· ${line(q)}\n· ${line(g)}\n两引擎结果已分别落图（图层名带引擎标识），可在图层列表中开关对比差异。`);
    refreshLayers();
  } catch (e) {
    addMsg('system', `对比判读失败：${e}`);
  }
  btn.disabled = false;
};

// ---------- 云端可达性检测 ----------
async function pingCloud() {
  try {
    const r = await fetch('/api/cloud/ping').then(r => r.json());
    if (!r.reachable && r.reason !== 'OFFLINE 模式已强制端侧')
      addMsg('system', `云端不可达（${r.reason}）。ONLINE 状态下将自动回退端侧研判；如需纯端侧可切换为断网模式。`);
  } catch (e) { /* 忽略 */ }
}

// ---------- Token 用量 ----------
async function refreshUsage() {
  const u = await fetch('/api/usage').then(r => r.json());
  const s = u.summary;
  $('usage-box').textContent = Object.entries(s).map(([side, v]) =>
    `${side}: ${v.calls} 次调用 · 输入 ${v.prompt_tokens} · 输出 ${v.completion_tokens} tok · 失败 ${v.errors}`
  ).join('\n') || '—';
}

async function loadRegion() {
  try {
    const resp = await fetch('/api/region');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const region = await resp.json();
    map.jumpTo({ center: region.center, zoom: region.zoom });
  } catch (e) {
    addMsg('system', `区域配置加载失败：${e.message}`);
  }
}

refreshUsage();
loadTasks();
loadEngines();
loadRegion();
pingCloud();
