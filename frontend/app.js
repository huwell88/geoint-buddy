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
  resetRun();
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
  resetRun();
  $('btn-send').disabled = !taskId;
  $('chat').innerHTML = '';
  if (taskId) loadHistory();
  refreshLayers();
};

async function loadHistory() {
  const msgs = await fetch(`/api/tasks/${taskId}/messages`).then(r => r.json());
  for (const m of msgs) {
    if (m.role === 'tool') continue;
    addMsg(m.role, m.content, false);
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

function addMsg(role, text, animate = true) {
  const div = document.createElement('div');
  div.className = `msg ${role}${animate ? ' msg-enter' : ''}`;
  if (role === 'assistant') div.innerHTML = mdToHtml(text);   // 最终回答渲染 Markdown
  else div.textContent = text;
  $('chat').appendChild(div);
  $('chat').scrollTop = $('chat').scrollHeight;
  return div;
}
const RUN_PHASES = ['plan', 'tool', 'summary', 'final'];
const TOOL_NAMES = {
  geo_area: '计算 AOI 面积', dem_stats: '提取 DEM 地形证据', vlm_inspect: '视觉影像判读',
  save_layer: '保存研判图层', terrain_profile: '生成地形剖面', list_layers: '读取任务图层',
};
let runState = null;

function resetRun() {
  runState = {
    status: 'idle', phase: null, rounds: 0, toolResults: 0, artifacts: new Set(),
    traceCount: 0, gap: null, budget: null, engineLabel: null,
    current: { kicker: '当前节点', name: '等待 Agent 启动', meta: '发送研判任务后，这里将展示真实执行事件', state: '待命' },
  };
  $('trace-log').innerHTML = '';
  $('run-details').open = false;
  renderRun();
  $('agent-run').hidden = true;
}

function beginRun() {
  resetRun();
  runState.status = 'running';
  runState.current = { kicker: '任务已提交', name: '正在连接 Agent', meta: '等待后端返回第一个执行事件', state: '启动中' };
  $('agent-run').hidden = false;
  renderRun();
}

function setRunPhase(phase) {
  runState.phase = phase;
  if (runState.status !== 'complete' && runState.status !== 'warning') runState.status = 'running';
}

function renderRun() {
  if (!runState) return;
  const root = $('agent-run');
  root.className = `is-${runState.status}`;
  const engineLabel = runState.engineLabel || modelLabel(currentModel);
  $('run-engine').textContent = engineLabel;
  $('run-engine').classList.toggle('is-cloud', engineLabel.startsWith('云端'));
  const statusText = { idle: '等待任务', running: 'Agent 正在执行', complete: '本轮研判完成', warning: '有限结论 · 证据不足', error: '执行中断或工具失败' };
  $('run-status').textContent = statusText[runState.status] || 'Agent 执行状态';

  const currentIndex = RUN_PHASES.indexOf(runState.phase);
  document.querySelectorAll('.run-phase').forEach((node, index) => {
    node.classList.remove('is-active', 'is-done', 'is-error');
    node.removeAttribute('aria-current');
    if (currentIndex < 0) return;
    const finalDone = currentIndex === RUN_PHASES.length - 1 && ['complete', 'warning'].includes(runState.status);
    if (index < currentIndex || finalDone) node.classList.add('is-done');
    else if (index === currentIndex) {
      node.classList.add(runState.status === 'error' ? 'is-error' : 'is-active');
      node.setAttribute('aria-current', 'step');
    }
  });

  const current = runState.current;
  const toolTone = runState.status === 'error' ? 'error'
    : runState.status === 'warning' ? 'warning'
    : runState.status === 'complete' || ['已完成', '缓存复用', '已返回'].includes(current.state) ? 'done'
    : runState.status === 'running' ? 'active' : 'idle';
  document.querySelector('.run-tool').dataset.state = toolTone;
  $('run-tool-kicker').textContent = current.kicker;
  $('run-tool-name').textContent = current.name;
  $('run-tool-meta').textContent = current.meta;
  $('run-tool-state').textContent = current.state;
  const started = runState.status !== 'idle';
  $('run-rounds').textContent = started ? runState.rounds : '—';
  $('run-tools').textContent = started ? runState.toolResults : '—';
  $('run-artifacts').textContent = started ? runState.artifacts.size : '—';
  $('run-trace-count').textContent = runState.traceCount;
  $('run-gap').hidden = !runState.gap;
  if (runState.gap) $('run-gap').textContent = runState.gap;
}

function addTrace(text) {
  if (!runState) resetRun();
  const div = document.createElement('div');
  div.className = 'msg trace';
  div.textContent = text;
  $('trace-log').appendChild(div);
  runState.traceCount += 1;
  $('run-trace-count').textContent = runState.traceCount;
  if ($('run-details').open) $('trace-log').scrollTop = $('trace-log').scrollHeight;
  return div;
}

function compactArgs(args) {
  const data = args || {};
  const parts = [];
  if (Array.isArray(data.bbox) && data.bbox.length === 4 && data.bbox.every(Number.isFinite))
    parts.push(`AOI [${data.bbox.map(v => v.toFixed(4)).join(', ')}]`);
  const focus = { landslide: '滑坡迹象', general: '综合灾害迹象' }[data.focus] || data.focus;
  if (focus) parts.push(`目标：${focus}`);
  if (parts.length) return parts.join(' · ');
  const text = JSON.stringify(data);
  return text.length > 120 ? `${text.slice(0, 117)}…` : text;
}

function artifactKey(item) {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return String(item);
  return item.id || item.layer_id || item.path || item.url || JSON.stringify(item);
}

$('btn-send').onclick = send;
function resizeComposer() {
  const input = $('msg-input');
  input.style.height = '38px';
  input.style.height = `${Math.min(input.scrollHeight, 104)}px`;
  input.style.overflowY = input.scrollHeight > 104 ? 'auto' : 'hidden';
}
$('msg-input').addEventListener('input', resizeComposer);
$('msg-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

async function send() {
  const text = $('msg-input').value.trim();
  if (!text || !taskId) return;
  $('msg-input').value = '';
  resizeComposer();
  $('btn-send').disabled = true;
  addMsg('user', text);
  beginRun();
  let finalDiv = null;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, message: text }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
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
        handleEvent(ev);
        if (ev.kind === 'final' && !finalDiv) {
          finalDiv = addMsg('assistant', ev.content);
          finalDiv.classList.add('is-final');
          if (runState.gap) finalDiv.classList.add('is-limited');
        }
      }
    }
    if (!finalDiv && runState.status === 'running') {
      runState.status = 'error';
      runState.current = { kicker: '连接中断', name: '未收到最终研判结果', meta: 'SSE 流已结束，请检查后端日志后重试', state: '中断' };
      addTrace('〔中断〕SSE 流结束，但未收到 final 事件');
    }
  } catch (err) {
    runState.status = 'error';
    runState.current = { kicker: '请求失败', name: 'Agent 连接中断', meta: err.message, state: '失败' };
    addTrace(`〔请求失败〕${err.message}`);
    addMsg('system', `请求失败：${err.message}`);
  } finally {
    renderRun();
    $('btn-send').disabled = false;
    refreshLayers();
    refreshUsage();
  }
}

function handleEvent(ev) {
  switch (ev.kind) {
    case 'start':
      setRunPhase('plan');
      runState.current = { kicker: '执行开始', name: '任务规划', meta: 'Agent 已接收任务，正在进入规划节点', state: '执行中' };
      break;
    case 'step': {
      const phase = ['tool', 'review', 'reuse_evidence', 'custom_reason'].includes(ev.phase) ? 'tool'
        : ev.phase === 'summary' ? 'summary' : 'plan';
      setRunPhase(phase);
      const phaseText = {
        plan: '任务规划', reuse_evidence: `复用${ev.evidence || ''}证据`, review: '第二引擎条件复核',
        tool: `执行 ${TOOL_NAMES[ev.tool] || ev.tool || '工具'}`, custom_reason: '自定义补救推理', summary: '证据汇总',
      };
      runState.current = { kicker: `步骤 ${ev.step}`, name: phaseText[ev.phase] || ev.phase, meta: '阶段由后端实时事件驱动', state: '执行中' };
      addTrace(`〔步骤 ${ev.step}〕${phaseText[ev.phase] || ev.phase}`);
      break;
    }
    case 'round': {
      runState.rounds = Math.max(runState.rounds, Number(ev.round) || 0);
      const sideText = ev.side === 'cloud' ? '云端' : '端侧';
      const modelName = ev.usage?.engine_name || ev.usage?.model || '';
      if (modelName) runState.engineLabel = `${sideText}·${modelName}`;
      const eng = modelName ? ` · ${modelName}` : '';
      const tokens = ev.usage?.completion_tokens ?? '—';
      runState.current = { kicker: '模型调用已返回', name: `LLM 第 ${ev.round} 轮 · ${ev.phase || '推理'}`, meta: `${sideText}${eng} · 输出 ${tokens} tokens`, state: '已返回' };
      addTrace(`〔模型〕第 ${ev.round} 轮 · ${sideText}${eng} · 输出 ${tokens} tokens`);
      break;
    }
    case 'tool_call':
      setRunPhase('tool');
      runState.current = { kicker: '工具调用', name: TOOL_NAMES[ev.tool] || ev.tool, meta: compactArgs(ev.args), state: '执行中' };
      addTrace(`〔工具〕${ev.tool} ${JSON.stringify(ev.args)}`);
      if (ev.tool === 'vlm_inspect' && currentModel === 'gemma4')
        addTrace('Gemma4 为思考模型，影像判读约需 3-5 分钟，请耐心等待…');
      break;
    case 'tool_result': {
      runState.status = 'running';
      runState.toolResults += 1;
      for (const item of ev.artifacts || []) runState.artifacts.add(artifactKey(item));
      const quality = ev.quality?.status;
      const coverage = Number.isFinite(ev.quality?.coverage)
        ? ` · 覆盖率 ${(ev.quality.coverage * 100).toFixed(1)}%` : '';
      const elapsed = ev.cached ? '缓存复用' : `${ev.elapsed_s ?? '—'} 秒`;
      const qualityText = quality ? ` · 质量 ${quality}${coverage}` : '';
      runState.current = {
        kicker: '工具结果已返回', name: TOOL_NAMES[ev.tool] || ev.tool,
        meta: `${elapsed}${qualityText}${ev.preview ? ` · ${ev.preview}` : ''}`,
        state: ev.cached ? '缓存复用' : '已完成',
      };
      addTrace(`〔结果〕${ev.tool} · ${elapsed}${qualityText}${ev.preview ? ` · ${ev.preview}` : ''}`);
      if (ev.artifacts?.length || ev.tool === 'vlm_inspect' || ev.tool === 'save_layer' || ev.tool === 'terrain_profile')
        refreshLayers();
      break;
    }
    case 'tool_error':
      runState.status = 'error';
      runState.current = { kicker: '工具执行失败', name: TOOL_NAMES[ev.tool] || ev.tool, meta: ev.message, state: '失败' };
      addTrace(`〔失败〕${ev.tool}：${ev.message}`);
      break;
    case 'evidence_gap': {
      const gap = ev.gap || {};
      const coverage = Number.isFinite(gap.coverage) ? `DEM 覆盖率 ${(gap.coverage * 100).toFixed(1)}%` : 'DEM 未返回合格数据';
      runState.gap = `地形证据不足：${coverage}。本次不输出区域整体定量风险结论。`;
      runState.status = 'warning';
      runState.current = { kicker: '证据门控已触发', name: '输出有限结论', meta: runState.gap, state: '证据不足' };
      addTrace(`〔证据缺口〕${runState.gap}`);
      break;
    }
    case 'final': {
      runState.budget = ev.budget || null;
      if (ev.evidence_gap && !runState.gap) runState.gap = '本次存在证据缺口，仅输出有限结论。';
      setRunPhase('final');
      runState.status = runState.gap ? 'warning' : 'complete';
      const b = runState.budget;
      runState.current = {
        kicker: '本轮执行完成', name: runState.gap ? '有限研判结论已生成' : '研判结果与证据已生成',
        meta: b ? `${b.steps} 个步骤 · ${b.tool_calls} 次工具调用 · ${b.llm_calls} 次 LLM 调用` : '最终结果已返回',
        state: runState.gap ? '证据受限' : '已完成',
      };
      addTrace(`〔完成〕${runState.current.meta}`);
      break;
    }
    case 'error':
      runState.status = 'error';
      runState.current = { kicker: '后端错误', name: 'Agent 执行中断', meta: ev.message, state: '失败' };
      addTrace(`〔错误〕${ev.message}`);
      addMsg('system', `错误：${ev.message}`);
      break;
  }
  renderRun();
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
  const previousLayerIds = new Set(currentLayers.map(l => l.id));
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
    if (previousLayerIds.size && !previousLayerIds.has(l.id)) li.classList.add('layer-new');
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

// ---------- 模型切换（云端 / 端侧引擎，切换前自动探测可用性） ----------
let currentModel = 'cloud';
const ENGINE_NAMES = {};

function modelLabel(id) {
  return `${id === 'cloud' ? '云端' : '端侧'}·${ENGINE_NAMES[id] || id}`;
}

async function loadEngines() {
  try {
    const r = await fetch('/api/engines').then(r => r.json());
    const sel = $('engine-sel');
    sel.innerHTML = '';
    for (const e of r.engines) {
      ENGINE_NAMES[e.id] = e.name;
      const o = document.createElement('option');
      o.value = e.id; o.textContent = modelLabel(e.id);
      o.title = e.desc;
      sel.appendChild(o);
    }
    sel.value = r.current;
    currentModel = r.current;
  } catch (e) { /* 后端旧版无此接口时忽略 */ }
}

$('engine-sel').onchange = async (e) => {
  const target = e.target.value;
  const prev = currentModel;
  e.target.disabled = true;
  addMsg('system', `正在探测「${modelLabel(target)}」可用性…`);
  try {
    const resp = await fetch('/api/engine', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ engine: target }),
    });
    const r = await resp.json().catch(() => ({}));
    if (!resp.ok || !r.current) {
      addMsg('system', `切换失败：${r.detail || `HTTP ${resp.status}`}，已保持「${modelLabel(prev)}」`);
      e.target.value = prev;
      return;
    }
    currentModel = r.current;
    renderRun();
    const slow = r.current === 'gemma4' ? '（思考模型，判读约需 3-5 分钟）' : '';
    addMsg('system', `模型已切换：${modelLabel(r.current)}${slow}`);
  } catch (err) {
    addMsg('system', `切换失败：${err.message}，已保持「${modelLabel(prev)}」`);
    e.target.value = prev;
  } finally {
    e.target.disabled = false;
  }
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

resetRun();
refreshUsage();
loadTasks();
loadEngines();
loadRegion();
