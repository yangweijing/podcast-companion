/**
 * 播客伴读 · 浏览器本地存储层（IndexedDB）
 *
 * 数据全部存在当前设备的浏览器里：换设备、换浏览器互不相通 —— 这是有意为之。
 * 结构沿用服务端备份 JSON 的形状：每期节目一条记录，内嵌 segments / analysis / notes。
 * 另有 blobs（本地上传的音频文件）、chats（伴读对话历史）、meta（自增计数器）。
 *
 * 注意：audio_url 若以 "idb:" 开头，表示音频本体存在本地 blobs 表（上传的文件）；
 *       否则是远程直链（平台解析来的），直接交给 <audio> 播放。
 */
const LocalDB = (() => {
  const DB_NAME = 'podcast-companion';
  const DB_VERSION = 1;
  let dbPromise = null;

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains('episodes')) db.createObjectStore('episodes', { keyPath: 'id' });
          if (!db.objectStoreNames.contains('notes')) {
            const s = db.createObjectStore('notes', { keyPath: 'id' });
            s.createIndex('episode_id', 'episode_id');
          }
          if (!db.objectStoreNames.contains('blobs')) db.createObjectStore('blobs');
          if (!db.objectStoreNames.contains('chats')) db.createObjectStore('chats', { keyPath: 'id' });
          if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta');
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    }
    return dbPromise;
  }

  function request(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function store(name, mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(name, mode);
      const out = fn(t.objectStore(name));
      t.oncomplete = () => resolve(out && out.__v !== undefined ? out.__v : out);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  }

  /* ---------- 自增 id ---------- */

  async function nextId(kind) {
    let v;
    await store('meta', 'readwrite', (s) => {
      const get = s.get(kind);
      get.onsuccess = () => {
        const val = (get.result || 0) + 1;
        s.put(val, kind);
        v = val;
      };
    });
    return v;
  }

  /* ---------- 节目 ---------- */

  function stripHeavy(ep) {
    const { segments, analysis, articles, ...rest } = ep;
    return { ...rest, has_segments: !!(segments && segments.length), has_analysis: !!analysis };
  }

  async function listEpisodes(q = '') {
    const all = await store('episodes', 'readonly', (s) => request(s.getAll()));
    const kw = q.trim().toLowerCase();
    const filtered = kw
      ? all.filter((e) => (e.title || '').toLowerCase().includes(kw) || (e.podcast || '').toLowerCase().includes(kw))
      : all;
    filtered.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    return filtered.map(stripHeavy);
  }

  async function getEpisode(id) {
    return store('episodes', 'readonly', (s) => request(s.get(id)));
  }

  async function createEpisode(data) {
    const id = await nextId('ep');
    const ep = {
      id,
      source_url: data.source_url || '',
      title: data.title || '',
      podcast: data.podcast || '',
      cover_url: data.cover_url || '',
      audio_url: data.audio_url || '',
      duration_ms: data.duration_ms || 0,
      transcript_status: 'pending',
      analysis_status: 'pending',
      error_message: null,
      transcript_task_id: null,
      created_at: new Date().toISOString(),
      segments: [],
      analysis: null,
      articles: [],
    };
    await store('episodes', 'readwrite', (s) => s.put(ep));
    return ep;
  }

  async function updateEpisode(id, patch) {
    const ep = await getEpisode(id);
    if (!ep) throw new Error('节目不存在');
    Object.assign(ep, patch);
    await store('episodes', 'readwrite', (s) => s.put(ep));
    return ep;
  }

  async function deleteEpisode(id) {
    const ep = await getEpisode(id);
    if (ep && ep.audio_url && ep.audio_url.startsWith('idb:')) {
      await store('blobs', 'readwrite', (s) => s.delete(ep.audio_url.slice(4)));
    }
    // 级联：该节目的笔记与对话
    const notes = await store('notes', 'readonly', (s) => request(s.index('episode_id').getAll(id)));
    for (const n of notes) await store('notes', 'readwrite', (s) => s.delete(n.id));
    const chats = await store('chats', 'readonly', (s) => request(s.getAll()));
    for (const c of chats) if (c.episode_id === id) await store('chats', 'readwrite', (s) => s.delete(c.id));
    await store('episodes', 'readwrite', (s) => s.delete(id));
  }

  /* ---------- 本地音频 ---------- */

  async function saveBlob(blob) {
    const key = 'a' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    await store('blobs', 'readwrite', (s) => s.put(blob, key));
    return key;
  }

  async function getBlob(key) {
    return store('blobs', 'readonly', (s) => request(s.get(key)));
  }

  /* ---------- 笔记 ---------- */

  async function listNotes(limit = 50) {
    const all = await store('notes', 'readonly', (s) => request(s.getAll()));
    all.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    return all.slice(0, limit);
  }

  async function getNote(id) {
    return store('notes', 'readonly', (s) => request(s.get(id)));
  }

  async function createNote({ episode_id, episode_title, title, document: doc, is_shared, source_mode }) {
    const id = await nextId('note');
    const note = {
      id,
      episode_id,
      episode_title: episode_title || '',
      title: title || '播客笔记',
      document: doc || [],
      is_shared: is_shared ? 1 : 0,
      source_mode: source_mode || 'full_episode',
      created_at: new Date().toISOString(),
    };
    await store('notes', 'readwrite', (s) => s.put(note));
    return note;
  }

  async function updateNote(id, patch) {
    const note = await getNote(id);
    if (!note) throw new Error('笔记不存在');
    if (patch.title !== undefined) note.title = patch.title;
    if (patch.document !== undefined) note.document = patch.document;
    if (patch.is_shared !== undefined) note.is_shared = patch.is_shared ? 1 : 0;
    await store('notes', 'readwrite', (s) => s.put(note));
  }

  async function deleteNote(id) {
    await store('notes', 'readwrite', (s) => s.delete(id));
  }

  /* ---------- 伴读对话 ---------- */

  async function getChat(sessionId) {
    return store('chats', 'readonly', (s) => request(s.get(sessionId)));
  }

  async function saveChat(chat) {
    await store('chats', 'readwrite', (s) => s.put(chat));
  }

  /* ---------- 备份 / 恢复 ---------- */

  async function exportAll() {
    const episodes = await store('episodes', 'readonly', (s) => request(s.getAll()));
    const notes = await store('notes', 'readonly', (s) => request(s.getAll()));
    // 远程音频直链保留；本地音频文件本体不导出（备份里标记 blob:true，导入后需重新上传）
    for (const ep of episodes) {
      if (ep.audio_url && ep.audio_url.startsWith('idb:')) {
        ep._audio_was_local = true;
      }
    }
    return { version: 2, exported_at: new Date().toISOString(), episodes, notes };
  }

  async function importAll(data) {
    if (!data || !Array.isArray(data.episodes)) throw new Error('不是有效的备份文件');
    let maxEp = 0, maxNote = 0, maxSeg = 0;
    for (const ep of data.episodes) maxEp = Math.max(maxEp, ep.id || 0);
    for (const n of data.notes || []) maxNote = Math.max(maxNote, n.id || 0);
    for (const ep of data.episodes) {
      for (const s of ep.segments || []) maxSeg = Math.max(maxSeg, s.id || 0);
    }
    await store('meta', 'readwrite', (s) => { s.put(Math.max(maxEp, 0), 'ep'); s.put(Math.max(maxNote, 0), 'note'); });
    for (const ep of data.episodes) {
      if (ep._audio_was_local) { ep.audio_url = ''; ep._audio_was_local = undefined; }
      await store('episodes', 'readwrite', (s) => s.put(ep));
    }
    for (const n of data.notes || []) await store('notes', 'readwrite', (s) => s.put(n));
    return {
      episodes: data.episodes.length,
      segments: data.episodes.reduce((a, e) => a + (e.segments || []).length, 0),
      notes: (data.notes || []).length,
      max_segment_id: maxSeg,
    };
  }

  return {
    listEpisodes, getEpisode, createEpisode, updateEpisode, deleteEpisode,
    saveBlob, getBlob,
    listNotes, getNote, createNote, updateNote, deleteNote,
    getChat, saveChat,
    exportAll, importAll,
  };
})();
