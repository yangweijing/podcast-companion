/**
 * 播客伴读 · 设备本地版前端逻辑
 *
 * 数据层：全部存本机 IndexedDB（localdb.js）—— 节目、逐字稿、标注、笔记、对话历史、上传的音频。
 * 干活层：链接解析 / 转写 / AI 分析走 Cloudflare Worker（无状态管道，不存数据）。
 * 与旧版（FastAPI 后端）的最大差异：
 *   - 列表/详情/笔记/备份 = 本地读写，零网络
 *   - 转写是 submit + query 两步，task_id 存在本地记录里，刷新/关机后可恢复轮询
 *   - 上传的音频以 Blob 存 IndexedDB，播放时用 objectURL；转写时临时上传
 */
const $ = (id) => document.getElementById(id);
const error = $('error'), notice = $('notice');

let currentEpisodeId = null;
let currentChatSession = null;
let pollTimer = null;
let searchTimer = null;
let currentEpisodeTitle = '';
let currentPodcastName = '';
let currentTranscriptSegments = [];
let currentAnalysis = null;
let transcriptMode = 'listen';
let currentNoteId = null;
let currentAudioObjectUrl = null;

/* ============ 边缘服务配置（存 localStorage） ============ */

const CFG = {
  get worker() { return (localStorage.getItem('pc_worker') || '').replace(/\/+$/, ''); },
  set worker(v) { localStorage.setItem('pc_worker', (v || '').trim().replace(/\/+$/, '')); },
  get token() { return localStorage.getItem('pc_token') || ''; },
  set token(v) { localStorage.setItem('pc_token', (v || '').trim()); },
};

function cfgFillForm() {
  $('workerUrl').value = CFG.worker;
  $('workerToken').value = CFG.token;
  $('cfgStatus').textContent = CFG.worker
    ? '当前服务：' + CFG.worker + '（信息只存在本机）'
    : '尚未配置。链接解析、转写、AI 分析需要它转发（它不保存你的任何数据）。';
}

$('saveCfgBtn').onclick = () => {
  CFG.worker = $('workerUrl').value;
  CFG.token = $('workerToken').value;
  cfgFillForm();
  showNotice('已保存边缘服务设置');
};
$('testCfgBtn').onclick = async () => {
  CFG.worker = $('workerUrl').value;
  CFG.token = $('workerToken').value;
  if (!CFG.worker) { showError('请先填写 Worker 地址'); return; }
  $('cfgStatus').textContent = '测试中…';
  try {
    const r = await fetch(CFG.worker + '/health');
    const j = await r.json();
    $('cfgStatus').textContent = `连通正常 ✓  AI: ${j.llm_ready ? '已配置' : '缺 key'} · 转写: ${j.asr_ready ? '已配置' : '缺 key'}` + (j.auth ? ' · 已启用口令' : '');
  } catch (e) {
    $('cfgStatus').textContent = '连不上：' + e.message;
  }
};

async function apiWorker(path, body, isForm = false) {
  if (!CFG.worker) throw new Error('请先在「边缘服务设置」里填写服务地址');
  const headers = {};
  if (CFG.token) headers.Authorization = 'Bearer ' + CFG.token;
  if (!isForm) headers['Content-Type'] = 'application/json';
  const r = await fetch(CFG.worker + path, { method: 'POST', headers, body: isForm ? body : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) throw new Error('访问口令不正确，请在「边缘服务设置」里检查');
  if (!r.ok) throw new Error(j.error || ('请求失败（HTTP ' + r.status + '）'));
  return j;
}

/* ============ 通用 ============ */

function esc(s) { return (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function fmtTime(ms) { const s = Math.max(0, Math.floor((ms || 0) / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function showError(msg) { error.textContent = msg || ''; error.className = 'error show'; }
function hideError() { error.className = 'error'; notice.className = 'notice'; }
function showNotice(msg) { notice.textContent = msg || ''; notice.className = 'notice show'; }
function hideNotice() { notice.className = 'notice'; }

const STATUS_LABEL = { pending: '待处理', processing: '处理中', completed: '已完成', failed: '失败' };
function badge(status) {
  if (!status) return '';
  const cls = status === 'completed' ? 'ok' : status === 'failed' ? 'err' : status === 'processing' ? 'run' : '';
  return `<span class="badge ${cls}">${STATUS_LABEL[status] || status}</span>`;
}

function switchView(name) {
  document.querySelectorAll('.view-panel').forEach((p) => p.classList.toggle('active', p.id === `view-${name}`));
  document.querySelectorAll('.nav-tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (name === 'library') loadNoteLibrary();
}
document.querySelectorAll('.nav-tab').forEach((tab) => tab.addEventListener('click', () => switchView(tab.dataset.view)));

/* ============ 音频播放（本地 Blob / 远程直链，直链失败自动走代理） ============ */

async function setAudioSource(ep) {
  if (currentAudioObjectUrl) { URL.revokeObjectURL(currentAudioObjectUrl); currentAudioObjectUrl = null; }
  if (ep.audio_url && ep.audio_url.startsWith('idb:')) {
    const blob = await LocalDB.getBlob(ep.audio_url.slice(4));
    if (blob) {
      currentAudioObjectUrl = URL.createObjectURL(blob);
      player.src = currentAudioObjectUrl;
      return;
    }
    player.src = '';
    return;
  }
  player.src = ep.audio_url || '';
  player.onerror = async () => {
    if (!ep.audio_url || player.dataset.proxied === '1') return;
    player.dataset.proxied = '1';
    try {
      const r = await fetch(CFG.worker + '/proxy?url=' + encodeURIComponent(ep.audio_url), {
        headers: CFG.token ? { Authorization: 'Bearer ' + CFG.token } : {},
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const blob = await r.blob();
      currentAudioObjectUrl = URL.createObjectURL(blob);
      player.src = currentAudioObjectUrl;
      player.play().catch(() => {});
    } catch (e) {
      showError('音频播放失败：平台直链被拦且代理不可用（' + e.message + '）');
    }
  };
}

/* ============ 节目列表 ============ */

async function loadEpisodes(keepSelection) {
  const query = $('podcastSearch').value.trim();
  const eps = await LocalDB.listEpisodes(query);
  $('libraryCount').textContent = `已收录 ${eps.length} 期`;
  $('resultCount').textContent = query ? `${eps.length} 个结果` : `${eps.length} 期`;
  const box = $('episodeList');
  box.innerHTML = '';
  if (!eps.length) {
    box.innerHTML = `<div class="empty-panel">${query ? '没有找到匹配的节目' : '还没有节目，先在上面添加一个吧。'}</div>`;
    if (!keepSelection) { $('detailSection').style.display = 'none'; $('detailEmpty').style.display = 'block'; currentEpisodeId = null; }
    return;
  }
  for (const ep of eps) {
    const div = document.createElement('div');
    div.className = 'ep-item' + (ep.id === currentEpisodeId ? ' active' : '');
    const cover = ep.cover_url
      ? `<img class="ep-thumb" src="${esc(ep.cover_url)}" alt="" loading="lazy">`
      : `<div class="ep-thumb ep-thumb--empty" aria-hidden="true">${esc((ep.title || ep.podcast || '播')[0])}</div>`;
    const dur = ep.duration_ms ? `<span class="ep-dur">${fmtTime(ep.duration_ms)}</span>` : '';
    div.innerHTML = `${cover}
      <div class="ep-item-body">
        <div class="ep-title">${esc(ep.title || '（无标题）')}</div>
        <div class="ep-podcast">${esc(ep.podcast || '')}</div>
        <div class="ep-meta-line">${dur}<div class="badges">${badge(ep.transcript_status)}${badge(ep.analysis_status)}</div></div>
      </div>
      <button class="ep-del" data-del="${ep.id}" title="删除这期节目" aria-label="删除这期节目">删除</button>`;
    div.onclick = () => selectEpisode(ep.id);
    div.querySelector('.ep-del').onclick = (e) => { e.stopPropagation(); confirmDeleteEpisode(ep); };
    box.appendChild(div);
  }
}
$('podcastSearch').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadEpisodes(true), 250);
});

async function confirmDeleteEpisode(ep) {
  const name = ep.title || ep.podcast || '（无标题）';
  if (!confirm(`确定删除《${name}》吗？\n\n该节目的逐字稿、分析与笔记会从本机删除，且无法撤销。`)) return;
  hideError(); hideNotice();
  await LocalDB.deleteEpisode(ep.id);
  showNotice(`已删除《${name}》`);
  if (currentEpisodeId === ep.id) {
    clearInterval(pollTimer);
    currentEpisodeId = null;
    $('detailSection').style.display = 'none';
    $('detailEmpty').style.display = 'block';
  }
  await loadEpisodes();
}

/* ============ 选择节目 ============ */

async function selectEpisode(id) {
  currentEpisodeId = id;
  currentChatSession = null;
  currentNoteId = null;
  $('copyAllNoteBtn').disabled = true;
  $('noteEditor').classList.remove('show');
  $('noteEmpty').style.display = 'block';
  clearInterval(pollTimer);
  $('chatMessages').innerHTML = '<div class="hint">可以定位节目片段，也可以继续探讨节目内容。</div>';
  const ep = await LocalDB.getEpisode(id);
  if (!ep) { showError('节目不存在'); return; }
  currentEpisodeTitle = ep.title || '无标题节目';
  currentPodcastName = ep.podcast || '';
  $('epCover').src = ep.cover_url || '';
  $('epTitle').textContent = ep.title || '（无标题）';
  $('epPodcast').textContent = ep.podcast || '';
  $('epDuration').textContent = ep.duration_ms ? '时长 ' + fmtTime(ep.duration_ms) : '';
  await setAudioSource(ep);
  $('detailEmpty').style.display = 'none';
  $('detailSection').style.display = 'block';
  loadEpisodes(true);
  renderStatus(ep);
  currentAnalysis = ep.analysis;
  if (ep.analysis) renderOutline(ep.analysis); else $('outlineContent').innerHTML = '<div class="empty-panel">完成「分析节目」后查看</div>';
  if (ep.transcript_status === 'completed') loadTranscript();
  else {
    currentTranscriptSegments = [];
    $('copyBtn').disabled = true; $('exportBtn').disabled = true; $('generateNoteBtn').disabled = true; $('copyAllTranscriptBtn').disabled = true;
    $('transcriptContent').innerHTML = '<div class="empty-panel">这期节目还没有逐字稿</div>';
  }
  // 转写进行中（页面刷新/关机后回来）→ 自动恢复轮询
  if (ep.transcript_status === 'processing' && ep.transcript_task_id) pollTranscribe(ep.transcript_task_id);
  switchView(ep.transcript_status === 'completed' ? 'chat' : 'detail');
}

function renderStatus(ep) {
  const line = $('statusLine');
  let html = `转写：${badge(ep.transcript_status)}  分析：${badge(ep.analysis_status)}`;
  if (ep.error_message) html += `<div class="err-text">⚠️ ${esc(ep.error_message)}</div>`;
  line.innerHTML = html;
  const transcribing = ep.transcript_status === 'processing';
  const analyzing = ep.analysis_status === 'processing';
  $('transcribeBtn').disabled = transcribing || analyzing || ep.transcript_status === 'completed';
  $('transcribeBtn').textContent = transcribing ? '转写中…' : (ep.transcript_status === 'completed' ? '逐字稿已生成' : '生成逐字稿');
  $('analyzeBtn').disabled = ep.transcript_status !== 'completed' || transcribing || analyzing;
  $('analyzeBtn').textContent = analyzing ? '分析中…' : (ep.analysis_status === 'completed' ? '重新分析' : '分析节目');
}

/* ============ 添加节目 ============ */

$('addBtn').onclick = async () => {
  const url = $('urlInput').value.trim();
  if (!url) { showError('请输入链接'); return; }
  hideError(); $('loading').className = 'loading show'; $('addBtn').disabled = true;
  try {
    const j = await apiWorker('/parse', { url });
    const ep = await LocalDB.createEpisode(j.episode);
    $('urlInput').value = '';
    if (j.warn) showNotice(j.warn); else hideNotice();
    await loadEpisodes();
    await selectEpisode(ep.id);
  } catch (e) {
    showError(e.message);
  } finally {
    $('loading').className = 'loading'; $('addBtn').disabled = false;
  }
};
$('urlInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('addBtn').click(); });

$('uploadBtn').onclick = async () => {
  const f = $('fileInput').files[0];
  if (!f) { showError('请先选择一个音频文件'); return; }
  hideError(); hideNotice(); $('loading').className = 'loading show'; $('uploadBtn').disabled = true;
  try {
    const key = await LocalDB.saveBlob(f);
    const ep = await LocalDB.createEpisode({
      source_url: '',
      title: $('titleInput').value.trim() || (f.name || '未命名音频').replace(/\.[^.]+$/, ''),
      podcast: $('podInput').value.trim(),
      audio_url: 'idb:' + key,
    });
    $('fileInput').value = ''; $('titleInput').value = ''; $('podInput').value = '';
    await loadEpisodes();
    await selectEpisode(ep.id);
  } catch (e) {
    showError('保存失败: ' + e.message);
  } finally {
    $('loading').className = 'loading'; $('uploadBtn').disabled = false;
  }
};

/* ============ 数据备份（导出/导入本机 JSON） ============ */

$('backupBtn').onclick = async () => {
  hideError(); hideNotice();
  try {
    const data = await LocalDB.exportAll();
    const blob = new Blob([JSON.stringify(data)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'podcast-backup-' + new Date().toISOString().slice(0, 10) + '.json';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    showNotice('备份已导出，请妥善保存这个文件');
  } catch (e) { showError('导出备份失败: ' + e.message); }
};

$('restoreBtn').onclick = () => $('restoreInput').click();
$('restoreInput').onchange = async () => {
  const f = $('restoreInput').files[0];
  if (!f) return;
  hideError(); hideNotice();
  if (!confirm('导入将「覆盖」本机现有全部数据，确定继续吗？\n建议先导出一份当前备份。')) { $('restoreInput').value = ''; return; }
  $('loading').className = 'loading show'; $('restoreBtn').disabled = true;
  try {
    const data = JSON.parse(await f.text());
    const stats = await LocalDB.importAll(data);
    showNotice(`导入完成：节目 ${stats.episodes} 期、逐字稿段落 ${stats.segments} 条、笔记 ${stats.notes} 篇`);
    await loadEpisodes();
  } catch (e) {
    showError('导入失败: ' + e.message);
  } finally {
    $('loading').className = 'loading'; $('restoreBtn').disabled = false; $('restoreInput').value = '';
  }
};

/* ============ 转写（submit + query，可断点恢复） ============ */

async function startTranscribe() {
  if (!currentEpisodeId) return;
  const ep = await LocalDB.getEpisode(currentEpisodeId);
  if (!ep || !ep.audio_url) { showError('这期节目没有音频，无法转写'); return; }
  hideError();
  $('transcribeBtn').disabled = true; $('transcribeBtn').textContent = '提交中…';
  try {
    let taskId;
    if (/^https?:\/\//.test(ep.audio_url)) {
      ({ task_id: taskId } = await apiWorker('/transcribe/submit', { audio_url: ep.audio_url }));
    } else {
      const blob = await LocalDB.getBlob(ep.audio_url.slice(4));
      if (!blob) throw new Error('本地音频文件丢失（可能被浏览器清理），请删除后重新上传');
      const fd = new FormData();
      fd.append('file', blob, 'audio' + (ep.title ? '' : ''));
      ({ task_id: taskId } = await apiWorker('/transcribe/submit', fd, true));
    }
    await LocalDB.updateEpisode(ep.id, { transcript_status: 'processing', transcript_task_id: taskId, error_message: null });
    renderStatus(await LocalDB.getEpisode(ep.id));
    loadEpisodes(true);
    pollTranscribe(taskId);
  } catch (e) {
    await LocalDB.updateEpisode(ep.id, { transcript_status: 'failed', error_message: e.message });
    renderStatus(await LocalDB.getEpisode(ep.id));
    showError(e.message);
  }
}
$('transcribeBtn').onclick = startTranscribe;

function pollTranscribe(taskId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!currentEpisodeId) { clearInterval(pollTimer); return; }
    try {
      const j = await apiWorker('/transcribe/query', { task_id: taskId });
      if (j.status === 'done' && j.segments) {
        clearInterval(pollTimer);
        const ep = await LocalDB.updateEpisode(currentEpisodeId, { transcript_status: 'completed', segments: j.segments, error_message: null });
        renderStatus(ep);
        loadEpisodes(true);
        loadTranscript();
      } else if (j.status === 'failed') {
        clearInterval(pollTimer);
        const ep = await LocalDB.updateEpisode(currentEpisodeId, { transcript_status: 'failed', error_message: j.error || '转写失败' });
        renderStatus(ep);
        loadEpisodes(true);
      }
      // processing → 继续轮
    } catch (e) {
      // 网络抖动不中断轮询；连续失败由用户手动刷新
    }
  }, 5000);
}

/* ============ 分析（同步调用，LLM 耗时数十秒） ============ */

$('analyzeBtn').onclick = async () => {
  if (!currentEpisodeId || !currentTranscriptSegments.length) return;
  hideError();
  await LocalDB.updateEpisode(currentEpisodeId, { analysis_status: 'processing', error_message: null });
  renderStatus(await LocalDB.getEpisode(currentEpisodeId));
  loadEpisodes(true);
  try {
    const j = await apiWorker('/ai/analyze', { segments: currentTranscriptSegments });
    currentAnalysis = j.analysis;
    await LocalDB.updateEpisode(currentEpisodeId, { analysis_status: 'completed', analysis: j.analysis });
    renderOutline(j.analysis);
    renderStatus(await LocalDB.getEpisode(currentEpisodeId));
    showNotice('分析完成');
  } catch (e) {
    await LocalDB.updateEpisode(currentEpisodeId, { analysis_status: 'failed', error_message: e.message });
    renderStatus(await LocalDB.getEpisode(currentEpisodeId));
    showError(e.message);
  }
  loadEpisodes(true);
};

$('refreshBtn').onclick = async () => {
  if (!currentEpisodeId) return;
  const ep = await LocalDB.getEpisode(currentEpisodeId);
  renderStatus(ep);
  if (ep.analysis) { currentAnalysis = ep.analysis; renderOutline(ep.analysis); }
  if (ep.transcript_status === 'completed') loadTranscript();
};

/* ============ 逐字稿 ============ */

async function loadTranscript() {
  const ep = await LocalDB.getEpisode(currentEpisodeId);
  const segs = (ep && ep.segments) || [];
  currentTranscriptSegments = segs;
  const box = $('transcriptContent');
  box.innerHTML = '';
  $('copyBtn').disabled = !segs.length;
  $('exportBtn').disabled = !segs.length;
  $('generateNoteBtn').disabled = !segs.length;
  $('copyAllTranscriptBtn').disabled = !segs.length;
  if (!segs.length) { box.innerHTML = '<div class="empty-panel">暂无逐字稿。</div>'; return; }
  for (const s of segs) {
    const wrap = document.createElement('div');
    wrap.className = 'seg-wrap';
    wrap.dataset.segmentId = s.id;
    const actions = document.createElement('div');
    actions.className = 'seg-actions';
    actions.innerHTML = `<button class="key-action ${s.is_key ? 'on' : ''}">重点</button><button class="original-action ${s.include_original ? 'on' : ''}">原文</button>`;
    const div = document.createElement('div');
    div.className = `seg${s.is_key ? ' key' : ''}${s.include_original ? ' quote-source' : ''}`;
    div.innerHTML = `<div class="seg-row"><span class="t">${fmtTime(s.start_ms)}</span>` +
      `<div class="seg-body">` +
      (s.speaker ? `<span class="s">${esc(s.speaker)}</span>` : '') +
      `<span class="txt">${esc(s.text)}</span></div></div>` +
      `<div class="seg-note"><div class="seg-note-view">${esc(s.note_text || '')}</div></div>` +
      `<div class="seg-tools"><button class="edit-action">编辑</button><button class="copy-segment-action">复制原文</button></div>`;
    div.onclick = (event) => {
      if (event.target.closest('button,textarea')) return;
      if (transcriptMode === 'listen') seek(s.start_ms);
      else wrap.classList.remove('actions-open');
    };
    actions.querySelector('.key-action').onclick = () => updateAnnotation(s, { is_key: !Boolean(s.is_key) });
    actions.querySelector('.original-action').onclick = () => updateAnnotation(s, { include_original: !Boolean(s.include_original) });
    div.querySelector('.edit-action').onclick = () => editSegmentNote(s, div);
    div.querySelector('.copy-segment-action').onclick = async (event) => {
      await writeClipboard(s.text); event.currentTarget.textContent = '已复制'; setTimeout(() => (event.currentTarget.textContent = '复制原文'), 1000);
    };
    let touchX = null;
    div.addEventListener('touchstart', (event) => { touchX = event.touches[0].clientX; }, { passive: true });
    div.addEventListener('touchend', (event) => {
      if (transcriptMode !== 'note' || touchX === null) return;
      const dx = event.changedTouches[0].clientX - touchX;
      if (dx > 45) wrap.classList.add('actions-open');
      if (dx < -35) wrap.classList.remove('actions-open');
      touchX = null;
    }, { passive: true });
    wrap.append(actions, div); box.appendChild(wrap);
  }
}

function setTranscriptMode(mode) {
  transcriptMode = mode;
  $('listenModeBtn').classList.toggle('active', mode === 'listen');
  $('noteModeBtn').classList.toggle('active', mode === 'note');
  $('transcriptContent').classList.toggle('note-mode', mode === 'note');
  $('swipeHint').style.display = mode === 'note' ? 'block' : 'none';
  $('transcriptModeHint').textContent = mode === 'listen' ? '伴听模式：点击句子跳转播放' : '笔记模式：点击不跳转，右滑标记，编辑蓝色主观笔记';
  document.querySelectorAll('.seg-wrap').forEach((n) => n.classList.remove('actions-open'));
}
$('listenModeBtn').onclick = () => setTranscriptMode('listen');
$('noteModeBtn').onclick = () => setTranscriptMode('note');

async function updateAnnotation(segment, changes) {
  const ep = await LocalDB.getEpisode(currentEpisodeId);
  if (!ep) return;
  const seg = (ep.segments || []).find((x) => x.id === segment.id);
  if (!seg) return;
  seg.is_key = Boolean(changes.is_key ?? seg.is_key);
  seg.include_original = Boolean(changes.include_original ?? seg.include_original);
  seg.note_text = changes.note_text ?? seg.note_text ?? '';
  await LocalDB.updateEpisode(currentEpisodeId, { segments: ep.segments });
  Object.assign(segment, { is_key: seg.is_key, include_original: seg.include_original, note_text: seg.note_text });
  await loadTranscript(); setTranscriptMode('note');
}

function editSegmentNote(segment, segmentNode) {
  const zone = segmentNode.querySelector('.seg-note');
  zone.innerHTML = `<textarea aria-label="主观标注"></textarea><div class="seg-tools"><button class="save-inline-note">保存标注</button><button class="cancel-inline-note">取消</button></div>`;
  const textarea = zone.querySelector('textarea'); textarea.value = segment.note_text || ''; textarea.focus();
  zone.querySelector('.save-inline-note').onclick = async () => {
    try { await updateAnnotation(segment, { note_text: textarea.value }); }
    catch (e) { showError(e.message); }
  };
  zone.querySelector('.cancel-inline-note').onclick = () => loadTranscript();
}

function transcriptText(segs) {
  return segs.map((s) => {
    const speaker = s.speaker ? ` ${s.speaker}` : '';
    return `[${fmtTime(s.start_ms)}]${speaker} ${s.text}`;
  }).join('\n');
}

async function copyTranscript() {
  const button = $('copyBtn');
  button.disabled = true; $('copyStatus').textContent = '复制中';
  try {
    const text = transcriptText(currentTranscriptSegments);
    if (!text.trim()) throw new Error('逐字稿为空');
    await writeClipboard(text);
    $('copyStatus').textContent = '已复制';
    setTimeout(() => { $('copyStatus').textContent = ''; }, 1800);
  } catch (e) { $('copyStatus').textContent = ''; showError(e.message); }
  finally { button.disabled = false; }
}
$('copyBtn').onclick = copyTranscript;

async function exportTranscript() {
  const button = $('exportBtn');
  button.disabled = true; $('copyStatus').textContent = '导出中';
  try {
    if (!currentTranscriptSegments.length) throw new Error('逐字稿为空');
    const header = `${currentEpisodeTitle}\n${currentPodcastName ? currentPodcastName + '\n' : ''}\n`;
    const content = `${header}${transcriptText(currentTranscriptSegments)}\n`;
    const blob = new Blob(['\ufeff', content], { type: 'text/plain;charset=utf-8' });
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const safeTitle = currentEpisodeTitle.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').replace(/\s+/g, ' ').trim().slice(0, 80) || '播客逐字稿';
    link.href = href; link.download = `${safeTitle}-逐字稿.txt`; link.style.display = 'none';
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(href), 1000);
    $('copyStatus').textContent = '已导出';
    setTimeout(() => { $('copyStatus').textContent = ''; }, 1800);
  } catch (e) {
    $('copyStatus').textContent = ''; showError(e.message);
  } finally { button.disabled = false; }
}
$('exportBtn').onclick = exportTranscript;

async function copyAllText(text, btn, okText) {
  if (!text || !text.trim()) { showError('内容为空，无可复制'); return; }
  const old = btn.textContent;
  btn.disabled = true;
  try {
    await writeClipboard(text);
    btn.textContent = okText || '已复制';
    btn.classList.add('done');
    setTimeout(() => { btn.textContent = old; btn.classList.remove('done'); }, 1500);
  } catch (e) {
    showError(e.message); btn.textContent = old;
  } finally {
    btn.disabled = false;
  }
}

$('copyAllTranscriptBtn').onclick = function () {
  copyAllText(transcriptText(currentTranscriptSegments), this, '已复制');
};

$('copyAllNoteBtn').onclick = function () {
  const blocks = collectNoteDocument();
  copyAllText(blocks.map((b) => b.text).join('\n\n'), this, '已复制');
};

/* ============ 导读 ============ */

function renderOutline(a) {
  const box = $('outlineContent');
  if (!a) { box.innerHTML = '<div class="hint">还没有分析结果。</div>'; return; }
  let html = '';
  if (a.summary) html += `<div class="out-summary">${esc(a.summary)}</div>`;
  const stages = (a.mainline && a.mainline.stages) || [];
  if (stages.length) {
    html += '<div class="out-block"><div class="h">主线脉络</div>';
    for (const st of stages) {
      html += `<div class="d"><b>${esc(st.title || '')}</b> ${esc(st.summary || '')}</div>` +
        (st.start_ms != null ? `<span class="play-chip" onclick="seek(${st.start_ms})">▶ ${fmtTime(st.start_ms)} 从这里听</span>` : '');
    }
    html += '</div>';
  }
  const qs = a.major_questions || [];
  if (qs.length) {
    html += '<div class="out-block"><div class="h">主要问题</div>';
    for (const q of qs) {
      html += `<div class="d"><b>${esc(q.question || '')}</b> ${esc(q.discussion || '')} ${q.conclusion ? '→ ' + esc(q.conclusion) : ''}</div>` +
        (q.start_ms != null ? `<span class="play-chip" onclick="seek(${q.start_ms})">▶ ${fmtTime(q.start_ms)} 从这里听</span>` : '');
    }
    html += '</div>';
  }
  const quotes = a.quotes || [];
  if (quotes.length) {
    html += '<div class="out-block"><div class="h">金句</div>';
    for (const q of quotes) {
      html += `<div class="quote"><div class="q">「${esc(q.text || '')}」</div>
        <div class="meta">${esc(q.speaker || '')} · ${q.start_ms != null ? fmtTime(q.start_ms) : ''} ${esc(q.reason || '')}</div>` +
        (q.start_ms != null ? `<span class="play-chip" onclick="seek(${q.start_ms})">▶ 从这里听</span>` : '') + '</div>';
    }
    html += '</div>';
  }
  box.innerHTML = html || '<div class="hint">还没有分析结果。</div>';
}

async function seek(startMs) {
  try {
    player.currentTime = Math.max(0, Number(startMs) / 1000);
    await player.play(); switchView('detail');
  } catch (e) { showError('播放失败: ' + e.message); }
}
window.seek = seek;

/* ============ 伴读对话（历史存本机） ============ */

const chatMessages = $('chatMessages'), chatInput = $('chatInput'), chatSend = $('chatSend');
function appendChat(role, text) {
  const div = document.createElement('div');
  div.className = 'chat-msg' + (role === 'user' ? ' me' : '');
  div.innerHTML = `<div class="who">${role === 'user' ? '我' : '伴读'}</div><div class="bubble">${esc(text)}</div>`;
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}
function appendLocation(loc) {
  const div = document.createElement('div');
  div.className = 'loc-card';
  div.innerHTML = `<div class="lc-text"><span class="lc-time">${esc(loc.time_label || '')}</span> ${esc(loc.excerpt || '')}</div>
    <div class="loc-actions"><button class="transcript-action">跳到逐字稿</button><button class="article-action">整理成文章</button></div>`;
  div.querySelector('.transcript-action').onclick = () => viewTranscriptAt(loc.segment_id);
  div.querySelector('.article-action').onclick = (event) => organizeArticle(loc, event.currentTarget);
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function viewTranscriptAt(segmentId) {
  if (!currentTranscriptSegments.length) await loadTranscript();
  switchView('transcript');
  setTranscriptMode('listen');
  requestAnimationFrame(() => {
    const target = document.querySelector(`.seg-wrap[data-segment-id="${Number(segmentId)}"]`);
    if (target) { target.scrollIntoView({ behavior: 'smooth', block: 'center' }); target.querySelector('.seg').classList.add('quote-source'); }
  });
}

async function writeClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const area = document.createElement('textarea');
  area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
  document.body.appendChild(area); area.select(); const copied = document.execCommand('copy'); area.remove();
  if (!copied) throw new Error('浏览器未允许复制');
}

function appendArticle(article) {
  const card = document.createElement('div');
  card.className = 'article-card';
  card.innerHTML = `<div class="article-title">${esc(article.title || '整理文稿')}</div><div class="article-body">${esc(article.article || '')}</div>
    <div class="article-meta">来源 ${fmtTime(article.start_ms)}–${fmtTime(article.end_ms)} · 仅做轻度结构整理</div>
    <div class="article-tools"><button class="article-copy">复制文章</button><button class="article-export">导出文章</button></div>`;
  const articleText = `${article.title || '整理文稿'}\n\n${article.article || ''}\n`;
  card.querySelector('.article-copy').onclick = async (event) => {
    const button = event.currentTarget; button.disabled = true;
    try { await writeClipboard(articleText); button.textContent = '已复制'; }
    catch (e) { showError(e.message); }
    finally { setTimeout(() => { button.disabled = false; button.textContent = '复制文章'; }, 1400); }
  };
  card.querySelector('.article-export').onclick = () => {
    const blob = new Blob(['\ufeff', articleText], { type: 'text/plain;charset=utf-8' });
    const href = URL.createObjectURL(blob); const link = document.createElement('a');
    const safeTitle = String(article.title || '整理文稿').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').trim().slice(0, 80) || '整理文稿';
    link.href = href; link.download = `${safeTitle}.txt`; link.style.display = 'none'; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  };
  chatMessages.appendChild(card);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function organizeArticle(loc, button) {
  if (!loc.segment_id || !currentEpisodeId) return;
  const oldLabel = button.textContent; button.disabled = true; button.textContent = '整理中…';
  try {
    const idx = currentTranscriptSegments.findIndex((s) => s.id === loc.segment_id);
    const i = idx >= 0 ? idx : 0;
    const window = currentTranscriptSegments.slice(Math.max(0, i - 2), i + 3);
    const r = await apiWorker('/ai/article', { segments: window, title: currentEpisodeTitle });
    appendArticle(r.article);
    const ep = await LocalDB.getEpisode(currentEpisodeId);
    ep.articles = ep.articles || [];
    ep.articles.push(r.article);
    await LocalDB.updateEpisode(currentEpisodeId, { articles: ep.articles });
  } catch (e) {
    showError(e.message);
  } finally { button.disabled = false; button.textContent = oldLabel; }
}

async function sendChat() {
  const message = chatInput.value.trim();
  if (!message || !currentEpisodeId) return;
  appendChat('user', message);
  chatInput.value = '';
  chatSend.disabled = true;
  try {
    if (!currentChatSession) {
      currentChatSession = 'ep' + currentEpisodeId + '-' + Date.now().toString(36);
      await LocalDB.saveChat({ id: currentChatSession, episode_id: currentEpisodeId, messages: [] });
    }
    const chat = (await LocalDB.getChat(currentChatSession)) || { id: currentChatSession, episode_id: currentEpisodeId, messages: [] };
    const history = chat.messages.slice(-10);
    const j = await apiWorker('/ai/chat', {
      segments: currentTranscriptSegments,
      history,
      message,
      podcast: currentPodcastName,
      title: currentEpisodeTitle,
    });
    chat.messages.push({ role: 'user', content: message });
    chat.messages.push({ role: 'assistant', content: j.answer || '' });
    await LocalDB.saveChat(chat);
    appendChat('assistant', j.answer || '（没有回答）');
    (j.locations || []).forEach(appendLocation);
  } catch (e) {
    appendChat('assistant', '出错了：' + e.message);
  } finally {
    chatSend.disabled = false;
  }
}
chatSend.onclick = sendChat;
chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

/* ============ 连续笔记 ============ */

function renderNoteDocument(documentBlocks) {
  const box = $('noteDocument'); box.innerHTML = '';
  (documentBlocks || []).forEach((block) => {
    const node = document.createElement('div');
    node.className = `note-block ${block.type === 'annotation' ? 'annotation' : 'ai'}`;
    node.dataset.type = block.type === 'annotation' ? 'annotation' : 'ai';
    if (block.segment_id != null) node.dataset.segmentId = block.segment_id;
    node.contentEditable = 'true';
    node.spellcheck = true;
    node.textContent = block.text || '';
    box.appendChild(node);
  });
}

function collectNoteDocument() {
  return [...$('noteDocument').querySelectorAll('.note-block')].map((node) => ({
    type: node.dataset.type === 'annotation' ? 'annotation' : 'ai',
    text: node.textContent || '',
    ...(node.dataset.segmentId ? { segment_id: Number(node.dataset.segmentId) } : {}),
  })).filter((b) => b.text.trim());
}

function openNote(note) {
  currentNoteId = note.id;
  $('noteTitle').value = note.title || '播客笔记';
  renderNoteDocument(note.document || []);
  $('shareNote').checked = Boolean(note.is_shared);
  $('noteEditor').classList.add('show');
  $('copyAllNoteBtn').disabled = false;
  $('noteEmpty').style.display = 'none';
  switchView('notes');
}

async function generateNote() {
  if (!currentEpisodeId) return;
  const button = $('generateNoteBtn');
  button.disabled = true; button.textContent = 'AI 正在整理…'; hideError();
  try {
    const j = await apiWorker('/ai/notes', { segments: currentTranscriptSegments });
    const note = await LocalDB.createNote({
      episode_id: currentEpisodeId,
      episode_title: currentEpisodeTitle,
      title: `${currentEpisodeTitle} · 伴读笔记`,
      document: j.blocks,
      is_shared: 0,
      source_mode: j.blocks.some((b) => b.type === 'annotation') ? 'user_annotations' : 'full_episode',
    });
    openNote(note);
  } catch (e) {
    showError(e.message);
  } finally {
    button.disabled = false; button.textContent = '生成笔记';
  }
}
$('generateNoteBtn').onclick = generateNote;

async function saveCurrentNote() {
  if (!currentNoteId) return false;
  await LocalDB.updateNote(currentNoteId, {
    title: $('noteTitle').value,
    document: collectNoteDocument(),
    is_shared: $('shareNote').checked,
  });
  await loadNoteLibrary();
  return true;
}

$('saveNoteBtn').onclick = async (event) => {
  const button = event.currentTarget; button.disabled = true;
  try { await saveCurrentNote(); button.textContent = '已保存'; }
  catch (e) { showError(e.message); }
  finally { setTimeout(() => { button.disabled = false; button.textContent = '保存'; }, 1200); }
};

$('exportNoteBtn').onclick = async () => {
  if (!currentNoteId) return;
  const button = $('exportNoteBtn'); button.disabled = true;
  try {
    const note = await LocalDB.getNote(currentNoteId);
    const escHtml = (s) => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const parts = [];
    for (const b of note.document || []) {
      if (b.type === 'annotation') parts.push(`<p style="background:#EDF1F4;border-left:3px solid #3E5A72;padding:8px 12px;color:#3E5A72">${escHtml(b.text)}</p>`);
      else parts.push(`<p>${escHtml(b.text)}</p>`);
    }
    const html = `<html><head><meta charset="utf-8"></head><body><h1>${escHtml(note.title)}</h1>${parts.join('')}</body></html>`;
    const blob = new Blob(['\ufeff', html], { type: 'application/msword' });
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href; link.download = `podcast-note-${note.id}.doc`; link.style.display = 'none';
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  } catch (e) { showError(e.message); }
  finally { button.disabled = false; }
};

$('deleteNoteBtn').onclick = async () => {
  if (!currentNoteId || !confirm('删除这篇笔记？删除后无法恢复。')) return;
  await LocalDB.deleteNote(currentNoteId);
  currentNoteId = null; $('noteEditor').classList.remove('show'); $('noteEmpty').style.display = 'block';
  await loadNoteLibrary();
};

async function loadNoteLibrary() {
  const notes = await LocalDB.listNotes(50);
  const box = $('noteLibrary'); box.innerHTML = '';
  if (!notes.length) { box.innerHTML = '<div class="empty-panel">还没有笔记，逐字稿页点「生成笔记」创建</div>'; return; }
  notes.forEach((note) => {
    const item = document.createElement('div'); item.className = 'note-library-item';
    item.innerHTML = `<div class="title">${esc(note.title)}</div><div class="meta">${esc(note.episode_title || '')} · ${note.source_mode === 'full_episode' ? '整期简要整理' : '用户标注整合'}</div>`;
    item.onclick = async () => { const n = await LocalDB.getNote(note.id); if (n) openNote(n); };
    box.appendChild(item);
  });
}
$('refreshNotesBtn').onclick = loadNoteLibrary;

/* ============ 删除节目（详情页按钮） ============ */

$('delBtn').onclick = async () => {
  if (!currentEpisodeId) return;
  if (!confirm('确定删除这个节目？本机的逐字稿、分析和笔记会一起删除。')) return;
  const deletedEpisodeId = currentEpisodeId;
  await LocalDB.deleteEpisode(currentEpisodeId);
  currentEpisodeId = null; currentChatSession = null;
  $('detailSection').style.display = 'none';
  $('detailEmpty').style.display = 'block'; $('copyBtn').disabled = true; $('exportBtn').disabled = true;
  $('generateNoteBtn').disabled = true;
  currentEpisodeTitle = ''; currentPodcastName = '';
  currentTranscriptSegments = [];
  $('transcriptContent').innerHTML = '<div class="empty-panel">选择已转写的节目后查看</div>';
  await loadEpisodes();
  switchView('library');
  void deletedEpisodeId;
};

/* ============ 启动：恢复进行中的转写任务 ============ */

(async () => {
  cfgFillForm();
  await loadEpisodes();
  await loadNoteLibrary();
  // 找出还在「处理中」的转写任务，静默恢复轮询
  try {
    const eps = await LocalDB.listEpisodes();
    for (const meta of eps) {
      if (meta.transcript_status === 'processing' && meta.transcript_task_id) {
        // 不切视图，后台轮询即可；轮到 done 时写库
        (async () => {
          for (let i = 0; i < 240; i++) {
            await new Promise((r) => setTimeout(r, 5000));
            try {
              const j = await apiWorker('/transcribe/query', { task_id: meta.transcript_task_id });
              if (j.status === 'done' && j.segments) {
                await LocalDB.updateEpisode(meta.id, { transcript_status: 'completed', segments: j.segments, error_message: null });
                loadEpisodes(true);
                if (meta.id === currentEpisodeId) { renderStatus(await LocalDB.getEpisode(meta.id)); loadTranscript(); }
                return;
              }
              if (j.status === 'failed') {
                await LocalDB.updateEpisode(meta.id, { transcript_status: 'failed', error_message: j.error || '转写失败' });
                loadEpisodes(true);
                if (meta.id === currentEpisodeId) renderStatus(await LocalDB.getEpisode(meta.id));
                return;
              }
            } catch (_) { /* 网络抖动继续 */ }
          }
        })();
      }
    }
  } catch (_) { /* 忽略 */ }
})();
