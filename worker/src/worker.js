/**
 * 播客伴读 · 无状态边缘后端（Cloudflare Worker）
 *
 * 设计原则：本 Worker 只是「管道」，不存储任何用户数据。
 *   - 节目信息/逐字稿/笔记全部存在浏览器 IndexedDB（各设备各自独立）
 *   - 这里只做三件事：解析平台链接、转发转写、转发大模型
 *   - API key 存在 Worker 环境变量里，永不暴露给前端
 *
 * 接口一览（除 health 外均要求 POST JSON）：
 *   GET  /health                 探活
 *   POST /parse                  {url}                          → {episode, warn?}
 *   POST /transcribe/submit      {audio_url} | multipart(file)  → {task_id}
 *   POST /transcribe/query       {task_id}                      → {status, segments?, error?}
 *   POST /ai/analyze             {segments}                     → {analysis}
 *   POST /ai/chat                {segments, history, message, podcast, title} → {answer, locations}
 *   POST /ai/article             {segments, title}              → {article}
 *   POST /ai/notes               {segments, annotations}        → {blocks}
 */

const XY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const XM_MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
  "Access-Control-Max-Age": "86400",
};

/* ============================================================
 * 通用工具
 * ============================================================ */

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS },
  });
}

function fail(message, status = 400) {
  return json({ error: message }, status);
}

function fmtTime(ms) {
  const s = Math.max(0, Math.floor((ms || 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function buildTimestampedTranscript(segments) {
  return (segments || [])
    .map((s) => {
      const speaker = s.speaker ? `${s.speaker}：` : "";
      return `[${fmtTime(s.start_ms)}] ${speaker}${s.text || ""}`;
    })
    .join("\n");
}

function numberedTranscript(segments) {
  return (segments || [])
    .map((s) => {
      const speaker = s.speaker ? `${s.speaker}：` : "";
      return `${s.id}. [${fmtTime(s.start_ms)}] ${speaker}${s.text || ""}`;
    })
    .join("\n");
}

function transcriptFullText(segments) {
  return (segments || []).map((s) => `[${fmtTime(s.start_ms)}] ${s.text || ""}`).join("\n");
}

function parseTimestampTag(text) {
  if (!text) return null;
  const m = String(text).match(/\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]/);
  if (!m) return null;
  let mm = parseInt(m[1], 10), ss = parseInt(m[2], 10);
  if (m[3]) { const hh = mm; mm = ss; ss = parseInt(m[3], 10); return (hh * 3600 + mm * 60 + ss) * 1000; }
  return (mm * 60 + ss) * 1000;
}

function overlap(a, b) {
  const set = new Set(a || "");
  let n = 0;
  for (const ch of b || "") if (set.has(ch)) n++;
  return n;
}

function nearestStartMs(segments, text) {
  let best = 0, bestScore = -1;
  for (const s of segments || []) {
    const score = overlap(text, s.text || "");
    if (score > bestScore) { bestScore = score; best = s.start_ms || 0; }
  }
  return best;
}

/* ============================================================
 * 提示词（自 prompts.py 移植，保持输出格式一致）
 * ============================================================ */

const ANALYZE_SYSTEM = `你是一位资深的播客内容编辑，擅长把一期访谈节目整理成「伴读导读」。
你会拿到带 [mm:ss] 时间码的逐字稿。请输出严格的 JSON，不要任何额外说明。
JSON 结构：
{
  "summary": "用 2-4 句话概括这期节目讲了一件什么事",
  "mainline": {"stages": [{"title": "阶段标题", "summary": "这一阶段讲了什么", "start_ms": 数字}]},
  "major_questions": [{"question": "节目探讨的核心问题", "discussion": "讨论过程", "conclusion": "结论(可空)", "start_ms": 数字}],
  "quotes": [{"text": "一句值得摘抄的原话", "speaker": "说话人(可空)", "start_ms": 数字, "reason": "为什么值得记"}]
}
重要：stages/questions/quotes 里的 start_ms 必须引用逐字稿中真实出现的时间码 [mm:ss] （把 mm:ss 换算成毫秒整数，例如 12:30 -> 750000）。每个数组 3-6 条，金句 4-8 条。`;

const NOTE_SYSTEM = `你是播客伴读笔记助手。你会拿到带时间码的逐字稿，以及听众手动标注的「重点/原文/主观想法」。
请输出严格的 JSON 数组，作为一篇可扫读的短笔记。数组元素两种类型：
  {"type": "ai", "text": "由你整理的一段通顺、有观点的内容（2-4 句）"}
  {"type": "annotation", "text": "听众的主观标注原文", "segment_id": 数字}
把听众标注自然穿插在 AI 段落之间（位置贴合其所指的内容），AI 段落负责串联与提炼。
总共 8-14 个块，整体像一篇有主线的短文，不要列点式。只输出 JSON 数组。`;

function chatSystem(podcast, title) {
  return `你是「播客伴读」，一位熟悉这期节目的阅读向导。
节目：${title || ""}（来自播客《${podcast || ""}》）。
你会拿到这期节目的逐字稿上下文。请用中文、口语化、有信息量的方式回答用户的问题；
如果用户想定位某段内容，请在回答末尾附上一个 JSON 片段（放在 \`\`\`loc 代码块里）列出相关片段：
[{"segment_id": 数字, "time_label": "mm:ss", "excerpt": "对应的原话摘要"}]
segment_id 取你能在上下文中对应的逐字稿序号（从 1 开始）；如无法确定就省略该片段。`;
}

const ARTICLE_INSTRUCTION = "把下面的片段轻度整理成一篇通顺的小文章（保留原意与口语感，补上必要的衔接与标点，去掉重复与口误）。直接输出文章正文，不要标题、不要解释：\n\n";

/* ============================================================
 * 大模型（OpenAI 兼容 → 豆包方舟）
 * ============================================================ */

async function llmComplete(env, system, user, temperature = 0.4) {
  const base = (env.LLM_BASE_URL || "https://ark.cn-beijing.volces.com/api/v3").replace(/\/$/, "");
  const resp = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.LLM_API_KEY}` },
    body: JSON.stringify({
      model: env.LLM_MODEL,
      messages: [{ role: "system", content: system }, { role: "user", content: user }],
      temperature,
    }),
  });
  if (!resp.ok) {
    const t = await resp.text().catch(() => "");
    throw new Error(`大模型请求失败（HTTP ${resp.status}）：${t.slice(0, 300)}`);
  }
  const data = await resp.json();
  return data.choices?.[0]?.message?.content || "";
}

function extractJson(text) {
  let t = String(text || "").trim();
  if (t.startsWith("```")) {
    t = t.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "");
  }
  try { return JSON.parse(t); } catch (_) { /* fallthrough */ }
  const m = t.match(/\{[\s\S]*\}|\[[\s\S]*\]/);
  if (m) { try { return JSON.parse(m[0]); } catch (_) { /* fallthrough */ } }
  throw new Error("模型未返回可解析的 JSON");
}

function splitLoc(text) {
  const m = String(text || "").match(/```loc\s*([\s\S]*?)```/);
  if (m) return [text.slice(0, m.index).trim(), m[1]];
  return [String(text || "").trim(), null];
}

function parseLoc(raw) {
  if (!raw) return [];
  let data;
  try { data = JSON.parse(raw); } catch (_) { return []; }
  const out = [];
  for (const it of Array.isArray(data) ? data : []) {
    if (it && typeof it === "object" && it.segment_id) {
      out.push({ segment_id: parseInt(it.segment_id, 10), time_label: it.time_label || "", excerpt: it.excerpt || "" });
    }
  }
  return out;
}

/* ============================================================
 * 链接解析（自 parser.py 移植；Worker 无子进程，喜马拉雅付费集不做 yt-dlp 兜底）
 * ============================================================ */

function httpsFix(u) {
  if (!u) return "";
  if (u.startsWith("//")) return "https:" + u;
  if (u.startsWith("http://")) return "https://" + u.slice(7);
  return u;
}

function meta(html, prop) {
  const p = prop.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  let m = html.match(new RegExp(`<meta[^>]+(?:property|name)="${p}"[^>]+content="([^"]+)"`, "i"));
  if (m) return m[1];
  m = html.match(new RegExp(`<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="${p}"`, "i"));
  return m ? m[1] : null;
}

function findAudio(html) {
  const m = html.match(/https?:\/\/[^\s"\\]+\.(?:m4a|mp3)(?:\?[^"\s\\]*)?/);
  return m ? m[0] : null;
}

function findDuration(html) {
  const m = html.match(/"duration"\s*:\s*(\d+)/);
  if (!m) return 0;
  const v = parseInt(m[1], 10);
  return v < 100000 ? v * 1000 : v;
}

function normalizeDur(d) {
  if (typeof d === "number" && isFinite(d)) return d < 100000 ? Math.round(d * 1000) : Math.round(d);
  return 0;
}

// ---------- 小宇宙 ----------

function extractEpisodeId(url) {
  const m = (url || "").match(/\/episode\/([A-Za-z0-9]+)/);
  return m ? m[1] : null;
}

async function parseXiaoyuzhou(url) {
  const epid = extractEpisodeId(url);
  if (!epid) throw new Error("无法从小宇宙链接中识别 episodeId，请确认是 /episode/ 开头的链接");

  const headers = { "User-Agent": XY_UA, Accept: "application/json" };

  // 方式一：webapi
  try {
    const r = await fetch(`https://www.xiaoyuzhoufm.com/webapi/v1/episode/get?episodeId=${epid}`, { headers });
    if (r.ok) {
      const data = await r.json().catch(() => null);
      const ep = data?.data?.episode || data?.result?.episode;
      if (ep) return normalizeXY(ep, url);
    }
  } catch (_) { /* fallthrough */ }

  // 方式二：抓页面 og 标签
  const r = await fetch(`https://www.xiaoyuzhoufm.com/episode/${epid}`, { headers });
  if (!r.ok) throw new Error(`小宇宙页面请求失败（HTTP ${r.status}），可能被限流或网络不可达`);
  const html = await r.text();
  const title = meta(html, "og:title") || meta(html, "title");
  const cover = meta(html, "og:image");
  const audio = meta(html, "og:audio") || findAudio(html);
  const podcast = meta(html, "og:site_name");
  if (!title) throw new Error("解析到页面但没能提取到标题，小宇宙页面结构可能已变动");
  return { title, podcast: podcast || "", cover_url: cover || "", audio_url: audio || "", duration_ms: findDuration(html), source_url: url };
}

function normalizeXY(ep, url) {
  const pod = ep.podcast || {};
  const podName = typeof pod === "object" ? pod.title || "" : "";
  return {
    title: ep.title || ep.name || "（无标题）",
    podcast: podName,
    cover_url: ep.cover || ep.image || ep.artwork || "",
    audio_url: ep.audio || ep.enclosureUrl || ep.mediaUrl || "",
    duration_ms: normalizeDur(ep.duration ?? ep.durationMs),
    source_url: url,
  };
}

// ---------- 喜马拉雅 ----------

function extractSoundId(url) {
  if (!url) return null;
  const m = url.match(/\/sound\/(\d+)/);
  if (m) return m[1];
  const all = url.match(/(?:^|\/)(\d+)(?:\/|$|\?)/g);
  if (!all) return null;
  const nums = all.map((s) => s.replace(/[^0-9]/g, "")).filter(Boolean);
  return nums.length ? nums[nums.length - 1] : null;
}

function isAlbumUrl(url) {
  return /\/album\/\d+/.test(url || "");
}

async function resolveShort(url) {
  if (!(url || "").includes("xima.tv")) return url;
  try {
    const r = await fetch(url, { headers: { "User-Agent": XY_UA, Referer: "https://www.ximalaya.com/" }, redirect: "follow" });
    return r.url || url;
  } catch (_) {
    return url;
  }
}

async function ximalayaTracks(soundId, env) {
  const headers = { "User-Agent": XM_MOBILE_UA, Referer: "https://www.ximalaya.com/", Accept: "application/json, text/plain, */*" };
  if (env.XIMALAYA_COOKIE) headers.Cookie = env.XIMALAYA_COOKIE;
  let d;
  try {
    const r = await fetch(`https://m.ximalaya.com/tracks/${soundId}.json`, { headers });
    if (!r.ok) return null;
    d = await r.json().catch(() => null);
  } catch (_) {
    return null;
  }
  if (!d || d.res === false || !d.id) return null;
  const audio = d.play_path_64 || d.play_path || d.play_path_32 || "";
  return {
    title: (d.title || "").trim() || `喜马拉雅单集 ${soundId}`,
    podcast: (d.album_title || d.nickname || "").trim(),
    cover_url: httpsFix(d.cover_url || d.cover_url_142 || ""),
    audio_url: audio ? httpsFix(audio) : "",
    duration_ms: d.duration ? parseInt(d.duration, 10) * 1000 : 0,
    _paid: !!d.is_paid || !audio,
  };
}

function signedLinkWarn(audioUrl) {
  if (audioUrl && /[?&]sign=/.test(audioUrl)) {
    return "该集为付费内容，取到的是带时效签名的音频直链（约 1 天内有效）。转写不受影响；但隔天后再播放可能需要重新添加。";
  }
  return null;
}

async function parseXimalaya(url, env) {
  const real = await resolveShort(url);
  if (isAlbumUrl(real)) {
    throw new Error("检测到这是喜马拉雅「专辑主页」链接；请打开具体某一集后，复制那一集的单集链接（形如 ximalaya.com/sound/数字）");
  }
  const soundId = extractSoundId(real);
  if (!soundId) throw new Error("无法从喜马拉雅链接识别单集 ID（soundId），请粘贴某一条音频的单集分享链接");

  const info = await ximalayaTracks(soundId, env);
  if (info) {
    const paid = info._paid; delete info._paid;
    if (info.audio_url) return { ...info, source_url: url };
    return {
      ...info, audio_url: "", source_url: url,
      _warn: paid
        ? "该集为喜马拉雅付费/会员内容，云端版暂无法解析付费音频，仅导入了标题与封面"
        : "未取到可播放音频，仅导入了标题与封面",
    };
  }

  // 兜底：网页 og 抓取
  try {
    const headers = { "User-Agent": XY_UA, Referer: "https://www.ximalaya.com/", Accept: "application/json, text/plain, */*" };
    if (env.XIMALAYA_COOKIE) headers.Cookie = env.XIMALAYA_COOKIE;
    const r = await fetch(`https://www.ximalaya.com/sound/${soundId}`, { headers });
    if (r.ok) {
      const html = await r.text();
      const title = meta(html, "og:title") || meta(html, "og:audio:title") || meta(html, "title");
      const cover = httpsFix(meta(html, "og:image") || "");
      if (title) {
        return { title, podcast: "", cover_url: cover, audio_url: "", duration_ms: findDuration(html), source_url: url, _warn: "仅取到标题，未取到可播放音频" };
      }
    }
  } catch (_) { /* fallthrough */ }

  throw new Error(`喜马拉雅单集 ${soundId} 取不到数据，通常是该集已下架/删除，或链接里的单集 ID 不对。请换一集免费内容重试，或改用「上传本地音频」导入。`);
}

// ---------- 网易云播客 ----------

function extractProgramId(url) {
  const m = (url || "").match(/[?&]id=(\d+)/);
  return m ? m[1] : null;
}

async function parseNetease(url) {
  const pid = extractProgramId(url);
  if (!pid) throw new Error("无法从网易云链接识别单集 program id，请粘贴某一条播客单集链接（形如 music.163.com/#/program?id=数字）");
  if (url.includes("#/djradio") || url.includes("/djradio")) {
    throw new Error("检测到这是电台主页链接；请打开具体某一集后，复制那集的节目链接（链接里应含 program?id=）");
  }
  const headers = { "User-Agent": XY_UA, Referer: "https://music.163.com/", Accept: "application/json, text/plain, */*" };
  let detail = null;
  try {
    const r = await fetch(`https://music.163.com/api/dj/program/detail?id=${pid}`, { headers });
    if (r.ok) {
      const d = await r.json().catch(() => null);
      if (d?.code === 200 && d?.program?.id) detail = d.program;
    }
  } catch (_) { /* fallthrough */ }
  if (!detail) throw new Error("网易云节目详情获取失败（可能该集已下架或接口变动），请改用上传本地音频");

  const mainSong = detail.mainSong || {};
  const songId = mainSong.id;
  const radio = detail.radio || {};
  const cover = detail.coverUrl || mainSong?.album?.picUrl || "";
  let podcast = radio.name || "";
  if (!podcast) podcast = detail.dj?.nickname || "";

  if (!songId) throw new Error("未能从网易云解析出音频地址");
  return {
    title: detail.name || mainSong.name || "（无标题）",
    podcast,
    cover_url: cover,
    audio_url: `https://music.163.com/song/media/outer/url?id=${songId}.mp3`,
    duration_ms: parseInt(detail.duration || mainSong.duration || 0, 10),
    source_url: url,
  };
}

// ---------- 统一入口 ----------

async function parseUrl(url, env) {
  const u = (url || "").trim().toLowerCase();
  if (u.includes("xiaoyuzhoufm.com")) return parseXiaoyuzhou(url);
  if (u.includes("ximalaya.com") || u.includes("xima.tv")) return parseXimalaya(url, env);
  if (u.includes("music.163.com") || u.includes("y.music.163.com") || u.includes("163cn.tv")) return parseNetease(url);
  throw new Error("暂不支持的平台链接。目前支持：小宇宙(xiaoyuzhoufm.com)、喜马拉雅(ximalaya.com)、网易云播客(music.163.com)；或改用上传本地音频。");
}

/* ============================================================
 * 火山 ASR（录音文件识别·极速版，URL 与 Base64 双模式，异步 submit/query）
 * ============================================================ */

function volcHeaders(env, requestId) {
  return {
    "Content-Type": "application/json",
    "X-Api-Key": env.VOLC_ASR_API_KEY,
    "X-Api-Resource-Id": env.VOLC_ASR_RESOURCE_ID || "volc.bigasr.auc_turbo",
    "X-Api-Request-Id": requestId,
    "X-Api-Sequence": "-1",
  };
}

function guessAudioFormat(nameOrUrl) {
  const m = String(nameOrUrl || "").toLowerCase().match(/\.(mp3|m4a|wav|aac|flac|ogg|opus)(?:\?|$)/);
  return m ? m[1] : "";
}

function bufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  let bin = "";
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

async function volcSubmit(env, audioObj) {
  if (!env.VOLC_ASR_API_KEY) throw new Error("服务端未配置 VOLC_ASR_API_KEY（火山语音控制台创建；与方舟大模型 key 不同）");
  const requestId = crypto.randomUUID();
  const payload = {
    user: { uid: "podcast_companion" },
    audio: audioObj,
    request: { model_name: "bigmodel", enable_itn: true, enable_punc: true, show_utterances: true },
  };
  const r = await fetch("https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit", {
    method: "POST",
    headers: volcHeaders(env, requestId),
    body: JSON.stringify(payload),
  });
  const data = await r.json().catch(() => ({}));
  const code = String(data.code ?? "");
  const message = data.message || data.msg || "";
  if (code && !["1000", "0", ""].includes(code)) {
    throw new Error(`火山 ASR 提交失败（code=${code} ${message}）。常见原因：API Key 无效、未开通豆包语音服务，或 Resource-Id 不匹配。`);
  }
  return requestId;
}

function extractVolcResult(data) {
  if (!data || typeof data !== "object") return {};
  let cand = data.result;
  if (cand && typeof cand === "object") return cand;
  const inner = data.data;
  if (inner && typeof inner === "object") {
    cand = inner.result;
    if (cand && typeof cand === "object") return cand;
  }
  return {};
}

function volcDone(data) {
  if (!data || typeof data !== "object") return false;
  const inner = typeof data.data === "object" ? data.data : data;
  const status = String(inner.status ?? data.status ?? "").toLowerCase().trim();
  if (["done", "success", "completed", "succeed", "finish", "finished", "2", "3"].includes(status)) return true;
  const result = extractVolcResult(data);
  if (result && (result.utterances || result.text)) return true;
  return false;
}

async function volcQuery(env, taskId) {
  const r = await fetch("https://openspeech.bytedance.com/api/v3/auc/bigmodel/query", {
    method: "POST",
    headers: volcHeaders(env, taskId),
    body: JSON.stringify({}),
  });
  const headerCode = r.headers.get("x-api-status-code") || "";
  const data = await r.json().catch(() => ({}));

  // 失败码（参考火山文档：20000003 静音，450001xx 参数/格式问题，550xxx 服务端错误）
  const code = String(data.code ?? headerCode ?? "");
  if (code.startsWith("45") || code.startsWith("55") || code === "20000003") {
    return { status: "failed", error: `火山 ASR 转写失败（code=${code}）` };
  }
  if (volcDone(data) || headerCode === "20000000") {
    const result = extractVolcResult(data);
    const utterances = result.utterances || [];
    const segments = [];
    for (const u of utterances) {
      const text = (u.text || "").trim();
      if (!text) continue;
      const speaker = u.additions?.speaker;
      segments.push({
        id: segments.length + 1,
        start_ms: parseInt(u.start_time || 0, 10),
        end_ms: parseInt(u.end_time || 0, 10),
        speaker: speaker ? `说话人${speaker}` : null,
        text,
      });
    }
    if (!segments.length && (result.text || "").trim()) {
      segments.push({ id: 1, start_ms: 0, end_ms: 0, speaker: null, text: result.text.trim() });
    }
    if (!segments.length) return { status: "processing" };
    return { status: "done", segments };
  }
  return { status: "processing" };
}

/* ============================================================
 * AI 四能力（自 llm.py 移植）
 * ============================================================ */

async function aiAnalyze(env, segments) {
  const ts = buildTimestampedTranscript(segments);
  const raw = await llmComplete(env, ANALYZE_SYSTEM, "以下是带时间码的逐字稿，请按要求生成导读 JSON：\n\n" + ts, 0.3);
  const data = extractJson(raw);
  for (const arr of ["major_questions", "quotes"]) {
    for (const item of data[arr] || []) {
      if (!item.start_ms) {
        item.start_ms = parseTimestampTag(item.text) || nearestStartMs(segments, item.text);
      }
    }
  }
  for (const st of data.mainline?.stages || []) {
    if (!st.start_ms) {
      st.start_ms = parseTimestampTag(st.summary) || nearestStartMs(segments, st.summary);
    }
  }
  return data;
}

async function aiChat(env, { segments, history, message, podcast, title }) {
  const messages = [{ role: "system", content: chatSystem(podcast, title) }];
  for (const m of (history || []).slice(-10)) messages.push({ role: m.role, content: m.content });
  messages.push({
    role: "user",
    content: "=== 本期逐字稿（带时间码，序号从 1 开始）===\n" + numberedTranscript(segments) + "\n=== 用户提问 ===\n" + message,
  });
  const base = (env.LLM_BASE_URL || "https://ark.cn-beijing.volces.com/api/v3").replace(/\/$/, "");
  const resp = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.LLM_API_KEY}` },
    body: JSON.stringify({ model: env.LLM_MODEL, messages, temperature: 0.5 }),
  });
  if (!resp.ok) {
    const t = await resp.text().catch(() => "");
    throw new Error(`大模型请求失败（HTTP ${resp.status}）：${t.slice(0, 300)}`);
  }
  const data = await resp.json();
  const text = data.choices?.[0]?.message?.content || "";
  const [answer, locRaw] = splitLoc(text);
  return { answer, locations: parseLoc(locRaw) };
}

async function aiNotes(env, segments, annotations) {
  const ts = buildTimestampedTranscript(segments);
  let annoTxt = "";
  if (annotations && annotations.length) {
    const lines = annotations.map((a) => {
      const kind = a.is_key ? "重点" : a.include_original ? "原文" : "想法";
      let line = `- 第${a.segment_id}段 听众标记[${kind}]`;
      if (a.note_text) line += `：${a.note_text}`;
      return line;
    });
    annoTxt = "听众标注：\n" + lines.join("\n") + "\n";
  }
  const raw = await llmComplete(env, NOTE_SYSTEM, annoTxt + "逐字稿：\n" + ts, 0.5);
  const data = extractJson(raw);
  if (!Array.isArray(data)) throw new Error("笔记生成结果应为 JSON 数组");
  return data.map((b) => ({
    type: ["ai", "annotation"].includes(b.type) ? b.type : "ai",
    text: b.text || "",
    segment_id: b.segment_id ?? null,
  }));
}

/* ============================================================
 * 路由
 * ============================================================ */

async function readJson(req) {
  try { return await req.json(); } catch (_) { return {}; }
}

function checkAuth(req, env) {
  if (!env.ACCESS_TOKEN) return true;
  const h = req.headers.get("Authorization") || "";
  return h === `Bearer ${env.ACCESS_TOKEN}`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    if (url.pathname === "/health" && request.method === "GET") {
      return json({
        ok: true,
        llm_ready: !!env.LLM_API_KEY,
        asr_ready: !!env.VOLC_ASR_API_KEY,
        auth: !!env.ACCESS_TOKEN,
      });
    }

    if (!checkAuth(request, env)) return fail("访问口令不正确", 401);

    try {
      // ---- 解析链接 ----
      if (url.pathname === "/parse" && request.method === "POST") {
        const body = await readJson(request);
        if (!body.url) return fail("缺少 url");
        const episode = await parseUrl(body.url, env);
        const out = { episode };
        if (episode._warn) out.warn = episode._warn;
        delete episode._warn;
        return json(out);
      }

      // ---- 转写：提交任务（URL 或上传文件均可）----
      if (url.pathname === "/transcribe/submit" && request.method === "POST") {
        const ctype = request.headers.get("Content-Type") || "";
        let audioObj;
        if (ctype.includes("multipart/form-data")) {
          const form = await request.formData();
          const file = form.get("file");
          if (!file || typeof file === "string") return fail("缺少音频文件（字段名 file）");
          const buf = await file.arrayBuffer();
          if (!buf.byteLength) return fail("文件为空");
          if (buf.byteLength > 90 * 1024 * 1024) return fail("文件过大（云端转写上限 90MB）");
          const audio = { data: bufferToBase64(buf) };
          const fmt = guessAudioFormat(file.name);
          if (fmt) audio.format = fmt;
          audioObj = audio;
        } else {
          const body = await readJson(request);
          if (!body.audio_url || !/^https?:\/\//.test(body.audio_url)) {
            return fail("缺少可公网访问的 audio_url（上传文件请用 multipart/form-data 的 file 字段）");
          }
          audioObj = { url: body.audio_url };
          const fmt = guessAudioFormat(body.audio_url);
          if (fmt) audioObj.format = fmt;
        }
        const taskId = await volcSubmit(env, audioObj);
        return json({ task_id: taskId });
      }

      // ---- 转写：查询结果 ----
      if (url.pathname === "/transcribe/query" && request.method === "POST") {
        const body = await readJson(request);
        if (!body.task_id) return fail("缺少 task_id");
        return json(await volcQuery(env, body.task_id));
      }

      // ---- AI：导读分析 ----
      if (url.pathname === "/ai/analyze" && request.method === "POST") {
        const body = await readJson(request);
        if (!Array.isArray(body.segments) || !body.segments.length) return fail("缺少逐字稿 segments");
        if (!env.LLM_API_KEY) return fail("服务端未配置 LLM_API_KEY");
        return json({ analysis: await aiAnalyze(env, body.segments) });
      }

      // ---- AI：伴读对话 ----
      if (url.pathname === "/ai/chat" && request.method === "POST") {
        const body = await readJson(request);
        if (!body.message) return fail("缺少 message");
        if (!env.LLM_API_KEY) return fail("服务端未配置 LLM_API_KEY");
        const out = await aiChat(env, body);
        return json(out);
      }

      // ---- AI：片段整理成文章 ----
      if (url.pathname === "/ai/article" && request.method === "POST") {
        const body = await readJson(request);
        const segs = body.segments || [];
        if (!segs.length) return fail("缺少逐字稿片段");
        if (!env.LLM_API_KEY) return fail("服务端未配置 LLM_API_KEY");
        const article = await llmComplete(env, "", ARTICLE_INSTRUCTION + transcriptFullText(segs), 0.4);
        return json({
          article: {
            title: `${body.title || "片段"} · 片段整理`,
            article,
            start_ms: segs[0].start_ms,
            end_ms: segs[segs.length - 1].end_ms,
            segment_ids: segs.map((s) => s.id),
          },
        });
      }

      // ---- AI：生成伴读笔记 ----
      if (url.pathname === "/ai/notes" && request.method === "POST") {
        const body = await readJson(request);
        if (!Array.isArray(body.segments) || !body.segments.length) return fail("还没有逐字稿，无法生成笔记");
        if (!env.LLM_API_KEY) return fail("服务端未配置 LLM_API_KEY");
        const annotations = (body.segments || []).filter((s) => s.is_key || s.include_original || s.note_text);
        return json({ blocks: await aiNotes(env, body.segments, annotations) });
      }

      // ---- 音频播放代理（平台直链防盗链时兜底；流式转发不落盘）----
      if (url.pathname === "/proxy" && request.method === "GET") {
        const target = url.searchParams.get("url");
        if (!target || !/^https?:\/\//.test(target)) return fail("缺少 url 参数");
        const range = request.headers.get("Range");
        const upstream = await fetch(target, {
          headers: range ? { Range: range, "User-Agent": XY_UA } : { "User-Agent": XY_UA },
        });
        const headers = { ...CORS, "Content-Type": upstream.headers.get("Content-Type") || "application/octet-stream" };
        const len = upstream.headers.get("Content-Length");
        const cr = upstream.headers.get("Content-Range");
        const ar = upstream.headers.get("Accept-Ranges");
        if (len) headers["Content-Length"] = len;
        if (cr) headers["Content-Range"] = cr;
        if (ar) headers["Accept-Ranges"] = ar;
        return new Response(upstream.body, { status: upstream.status, headers });
      }

      return fail(`未知接口：${url.pathname}`, 404);
    } catch (e) {
      return fail(e?.message || String(e), 500);
    }
  },
};
