// 播客伴读 · 腾讯云 SCF Web 函数入口
//
// SCF「Web 函数」模式：本文件作为 HTTP 服务器监听 9000 端口，
// SCF 会把外部 HTTP 请求转发到这个端口。我们把每个 Node 原生请求
// 转成 Web 标准 Request，交给 worker.fetch() 处理，再把 Web Response 写回。
//
// 这样 worker.js 里那套 Web 标准 API（Request/Response/fetch/FormData...）
// 可以原样复用，无需为腾讯云改写业务逻辑。

import http from 'http';
import { webcrypto } from 'node:crypto';
import worker from './worker.js';

const PORT = 9000;

// 腾讯云 SCF 的 Node 18 运行时不像 Cloudflare Worker 那样内置全局 crypto
// （worker.js 第 449 行用到了 crypto.randomUUID()）。
// 这里把 Node 自带的 WebCrypto 挂到 globalThis.crypto，worker 代码无需改动。
if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}
// 兜底：万一运行时 WebCrypto 没实现 randomUUID，自己生成 UUID v4。
if (!globalThis.crypto.randomUUID) {
  globalThis.crypto.randomUUID = () => {
    const b = globalThis.crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };
}

// 环境变量来自 SCF 控制台「函数配置 → 环境变量」
function buildEnv() {
  return {
    LLM_API_KEY: process.env.LLM_API_KEY,
    LLM_MODEL: process.env.LLM_MODEL,
    LLM_BASE_URL: process.env.LLM_BASE_URL,
    VOLC_ASR_API_KEY: process.env.VOLC_ASR_API_KEY,
    VOLC_ASR_RESOURCE_ID: process.env.VOLC_ASR_RESOURCE_ID,
    ACCESS_TOKEN: process.env.ACCESS_TOKEN,
    XIMALAYA_COOKIE: process.env.XIMALAYA_COOKIE,
  };
}

// 腾讯云 Web 函数的访问地址可能带发布前缀（如 /release/… 或函数名）。
// worker 的路由认 /health /parse /transcribe /ai /proxy 这几个根路径；
// 这里把"首段是未知前缀"的路径归一到根路径，两种访问方式都能命中。
const KNOWN_FIRST_SEGMENTS = ['health', 'parse', 'transcribe', 'ai', 'proxy'];

function normalizePath(rawPath) {
  const segs = rawPath.split('/');
  const first = segs[1] || '';
  if (first && !KNOWN_FIRST_SEGMENTS.includes(first)) {
    const rest = segs.slice(2);
    return '/' + rest.join('/');
  }
  return rawPath;
}

const server = http.createServer(async (req, res) => {
  try {
    const host = req.headers.host || 'localhost';
    const raw = req.url || '/';
    const qIdx = raw.indexOf('?');
    const rawPath = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
    const query = qIdx >= 0 ? raw.slice(qIdx) : '';
    const url = `https://${host}${normalizePath(rawPath)}${query}`;

    // 读请求体（GET/HEAD 无 body）
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const bodyBuf = Buffer.concat(chunks);

    const init = { method: req.method, headers: req.headers };
    if (req.method !== 'GET' && req.method !== 'HEAD' && bodyBuf.length > 0) {
      init.body = bodyBuf; // Buffer 兼容 Web Request 的 BodyInit
    }

    const request = new Request(url, init);
    const response = await worker.fetch(request, buildEnv());

    res.statusCode = response.status;
    response.headers.forEach((value, key) => res.setHeader(key, value));

    const out = Buffer.from(await response.arrayBuffer());
    res.end(out);
  } catch (err) {
    res.statusCode = 500;
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify({
      error: 'scf_internal_error',
      detail: String((err && err.stack) || err),
    }));
  }
});

server.listen(PORT, () => {
  console.log(`podcast-companion scf web function listening on :${PORT}`);
});
