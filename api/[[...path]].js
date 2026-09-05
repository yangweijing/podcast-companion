// 播客伴读 · 无状态后端（Vercel Edge Function 版）
//
// 为什么不用 Cloudflare Workers：*.workers.dev 在中国大陆常被墙，浏览器直接 "Failed to fetch"。
// Vercel 的 *.vercel.app 在国内通常可直连，且不需要自有域名。
// 本文件复用 worker/src/worker.js 的全部逻辑（同一套 Web 标准 API），只把环境变量来源从
// Cloudflare 的 env 参数改成 process.env，并适配 Vercel Edge 的默认导出签名。
//
// 部署：Vercel 导入本 GitHub 仓库 → 在 Project Settings → Environment Variables 填
//   LLM_API_KEY / LLM_MODEL / LLM_BASE_URL(可选) / VOLC_ASR_API_KEY /
//   VOLC_ASR_RESOURCE_ID / ACCESS_TOKEN(可选) / XIMALAYA_COOKIE(可选)
// 然后 Deploy。得到 https://<项目名>.vercel.app

export const config = { runtime: "edge" };

import worker from "../worker/src/worker.js";

export default async function (request) {
  const env = {
    LLM_API_KEY: process.env.LLM_API_KEY,
    LLM_MODEL: process.env.LLM_MODEL,
    LLM_BASE_URL: process.env.LLM_BASE_URL,
    VOLC_ASR_API_KEY: process.env.VOLC_ASR_API_KEY,
    VOLC_ASR_RESOURCE_ID: process.env.VOLC_ASR_RESOURCE_ID,
    ACCESS_TOKEN: process.env.ACCESS_TOKEN,
    XIMALAYA_COOKIE: process.env.XIMALAYA_COOKIE,
  };
  return worker.fetch(request, env);
}
