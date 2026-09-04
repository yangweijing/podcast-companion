"""FastAPI 主应用：实现与原版前端对齐的全部 /api 路由，并托管前端静态页。

启动：uvicorn main:app --port 8000  （在 app/ 目录下）
或直接：python run.py
"""
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import db
from config import (ACCESS_TOKEN, ALLOWED_UPLOAD_EXT, AUTH_COOKIE_DAYS, AUTH_COOKIE_NAME,
                    BASE_DIR, DB_PATH, DEMO_MODE, MAX_UPLOAD_MB, UPLOAD_DIR,
                    has_asr, has_llm)
from providers import parser, asr, llm
from providers import prompts as _prompts

STATIC_DIR = os.path.join(BASE_DIR, "static")


# ---------------- 工具 ----------------

def numbered_transcript(segments: list) -> str:
    """给大模型对话用的带编号逐字稿（编号=数据库 segment id，便于定位）。"""
    lines = []
    for s in segments:
        ts = _prompts.fmt_time(s.get("start_ms", 0))
        speaker = f"{s['speaker']}：" if s.get("speaker") else ""
        lines.append(f"{s['id']}. [{ts}] {speaker}{s.get('text', '')}")
    return "\n".join(lines)


def transcript_full_text(segments: list) -> str:
    return "\n".join(f"[{_prompts.fmt_time(s.get('start_ms', 0))}] {s.get('text', '')}" for s in segments)


# ---------------- 后台任务 ----------------

def _bg_transcribe(ep_id: int):
    try:
        ep = db.get_episode(ep_id)
        if not ep or not ep.get("audio_url"):
            db.update_episode(ep_id, transcript_status="failed", error_message="缺少音频地址")
            return
        segments = asr.transcribe(ep["audio_url"])
        segments = [dict(s, id=None) for s in segments]  # id 交给 db 自增
        db.save_segments(ep_id, segments)
        db.update_episode(ep_id, transcript_status="completed", error_message=None)
    except Exception as e:
        db.update_episode(ep_id, transcript_status="failed", error_message=str(e)[:300])


def _bg_analyze(ep_id: int):
    try:
        segs = db.get_segments(ep_id)
        analysis = llm.analyze(segs)
        db.save_analysis(ep_id, analysis)
        db.update_episode(ep_id, analysis_status="completed", error_message=None)
    except Exception as e:
        db.update_episode(ep_id, analysis_status="failed", error_message=str(e)[:300])


# ---------------- 演示种子数据 ----------------

def seed_demo():
    if db.list_episodes():
        return
    ep_id = db.create_episode(
        source_url="https://www.xiaoyuzhoufm.com/episode/demo-seed",
        title="把努力量化成决策：一个离职创业者的复盘",
        podcast="演示播客",
        cover_url="",
        audio_url=getattr(asr, "DEMO_AUDIO", ""),
        duration_ms=600000,
    )
    raw_segs = asr.demo_segments()
    db.save_segments(ep_id, [dict(s, id=None) for s in raw_segs])
    segs = db.get_segments(ep_id)
    db.save_analysis(ep_id, llm.analyze(segs))  # DEMO_MODE 下走规则生成
    db.update_episode(ep_id, transcript_status="completed", analysis_status="completed")


# ---------------- 生命周期 ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if DEMO_MODE:
        seed_demo()
    yield


app = FastAPI(title="播客伴读（本地复刻版）", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ---------------- 公网访问保护（可选，靠 ACCESS_TOKEN 开关） ----------------
# 为什么要这个：部署到公网后，若不设口令，任何人拿到网址都能用你的 API key
# 调转写和对话，账单算你的。设置 ACCESS_TOKEN 后需口令才能访问。
# 用法：首次打开 https://你的网址/?token=你的口令 ，服务端种 cookie，之后直接访问即可。

def _token_ok(req: Request) -> bool:
    """校验访问口令：query ?token= / 请求头 X-Access-Token / cookie，任一匹配即通过。"""
    if not ACCESS_TOKEN:
        return True
    got = (req.query_params.get("token")
           or req.headers.get("x-access-token")
           or req.cookies.get(AUTH_COOKIE_NAME)
           or "")
    return got == ACCESS_TOKEN


@app.middleware("http")
async def auth_middleware(req: Request, call_next):
    path = req.url.path
    need_auth = path.startswith("/api/") or path.startswith("/uploads/")
    # 健康检查必须放行：云平台探活请求不会带口令
    if need_auth and path.rstrip("/") == "/api/health":
        need_auth = False

    if need_auth and not _token_ok(req):
        return JSONResponse(
            {"error": "需要访问口令：请先用「https://你的网址/?token=你的口令」打开一次，之后同一浏览器免输。"},
            status_code=401,
        )

    resp = await call_next(req)

    # 首页带正确口令访问时种下 cookie，之后直接访问网址即可
    if ACCESS_TOKEN and not path.startswith(("/api/", "/uploads/")):
        if req.query_params.get("token") == ACCESS_TOKEN:
            resp.set_cookie(
                AUTH_COOKIE_NAME, ACCESS_TOKEN,
                max_age=AUTH_COOKIE_DAYS * 86400,
                httponly=True, samesite="lax", secure=req.url.scheme == "https",
            )
    return resp


# ---------------- 健康检查 / 部署自检 ----------------

@app.get("/api/health")
async def health():
    """云端探活与自检。无需密钥、无需口令，部署后浏览器打开即可确认配置是否生效。"""
    try:
        count = len(db.list_episodes(limit=100000))
        db_ok = True
    except Exception:
        count, db_ok = -1, False
    return {
        "status": "ok" if db_ok else "degraded",
        "episodes": count,
        "demo_mode": DEMO_MODE,
        "llm_ready": has_llm(),
        "asr_ready": has_asr(),
        "auth_enabled": bool(ACCESS_TOKEN),
    }


# ---------------- 节目 ----------------

@app.get("/api/podcasts")
async def list_podcasts(q: str = "", limit: int = 50):
    eps = db.list_episodes(q, limit)
    return {"count": len(eps), "episodes": eps}


@app.post("/api/podcasts")
async def add_podcast(req: Request):
    body = await req.json()
    url = (body.get("source_url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少 source_url")
    try:
        info = parser.parse(url)
    except Exception as e:
        raise HTTPException(400, f"解析失败：{e}")
    ep_id = db.create_episode(
        source_url=url, title=info.get("title", ""), podcast=info.get("podcast", ""),
        cover_url=info.get("cover_url"), audio_url=info.get("audio_url"),
        duration_ms=info.get("duration_ms", 0),
    )
    # info["_warn"]：解析成功但不完美（如付费集只拿到带时效的签名直链），
    # 不阻断流程，交给前端用黄色提示条告知用户。
    warn = info.get("_warn")
    resp = {"episode": db.get_episode(ep_id)}
    if warn:
        resp["warn"] = warn
    return resp


@app.post("/api/podcasts/upload")
async def upload_podcast(
    file: UploadFile = File(...),
    title: str = Form(""),
    podcast: str = Form(""),
):
    """上传本地音频文件，绕过小宇宙解析，直接归入节目库。

    演示模式也允许上传（仅存文件 + 建记录，无需 AI key）。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_MB}MB）")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, f"不支持的音频格式：{ext or '（无扩展名）'}")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fname = uuid.uuid4().hex + ext
    with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
        f.write(raw)
    ep_title = (title.strip() or (file.filename or "未命名音频").rsplit(".", 1)[0] or "未命名音频")
    ep_id = db.create_episode(
        source_url="", title=ep_title, podcast=podcast.strip(),
        cover_url="", audio_url=f"/uploads/{fname}", duration_ms=0,
    )
    return {"episode": db.get_episode(ep_id)}


@app.get("/api/podcasts/{ep_id}")
async def get_podcast(ep_id: int):
    ep = db.get_episode(ep_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    analysis = db.get_analysis(ep_id)
    return {"episode": ep, "analysis": analysis}


@app.delete("/api/podcasts/{ep_id}")
async def delete_podcast(ep_id: int):
    if not db.get_episode(ep_id):
        raise HTTPException(404, "节目不存在")
    db.delete_episode(ep_id)
    return {"ok": True}


@app.post("/api/podcasts/{ep_id}/transcribe")
async def transcribe(ep_id: int, bg: BackgroundTasks, background: bool = True):
    ep = db.get_episode(ep_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    if not DEMO_MODE and not has_asr():
        raise HTTPException(400, "未配置转写服务：请设置 WHISPER_API_KEY 或将 DEMO_MODE=true")
    if ep["transcript_status"] == "processing":
        return {"ok": True, "episode": ep}
    db.update_episode(ep_id, transcript_status="processing", error_message=None)
    bg.add_task(_bg_transcribe, ep_id)
    return {"ok": True, "episode": db.get_episode(ep_id)}


@app.post("/api/podcasts/{ep_id}/analyze")
async def analyze(ep_id: int, bg: BackgroundTasks, background: bool = True):
    ep = db.get_episode(ep_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    if ep["transcript_status"] != "completed":
        raise HTTPException(400, "请先完成逐字稿转写")
    if not DEMO_MODE and not has_llm():
        raise HTTPException(400, "未配置大模型：请设置 LLM_API_KEY 或将 DEMO_MODE=true")
    if ep["analysis_status"] == "processing":
        return {"ok": True, "episode": ep}
    db.update_episode(ep_id, analysis_status="processing", error_message=None)
    bg.add_task(_bg_analyze, ep_id)
    return {"ok": True, "episode": db.get_episode(ep_id)}


# ---------------- 逐字稿 ----------------

@app.get("/api/podcasts/{ep_id}/transcript")
async def get_transcript(ep_id: int, format: str = ""):
    if not db.get_episode(ep_id):
        raise HTTPException(404, "节目不存在")
    segs = db.get_segments(ep_id)
    if format == "text":
        text = transcript_full_text(segs)
        return Response(text, media_type="text/plain; charset=utf-8")
    return {"segments": segs}


@app.patch("/api/podcasts/{ep_id}/transcript/annotations/{seg_id}")
async def patch_annotation(ep_id: int, seg_id: int, req: Request):
    body = await req.json()
    updated = db.update_segment_annotation(
        ep_id, seg_id,
        is_key=body.get("is_key"),
        include_original=body.get("include_original"),
        note_text=body.get("note_text"),
    )
    if not updated:
        raise HTTPException(404, "段落不存在")
    return {"annotation": updated}


# ---------------- 伴读对话 ----------------

@app.post("/api/podcasts/{ep_id}/chat")
async def chat(ep_id: int, req: Request):
    ep = db.get_episode(ep_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    body = await req.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "缺少 message")
    if not DEMO_MODE and not has_llm():
        raise HTTPException(400, "未配置大模型：请设置 LLM_API_KEY 或将 DEMO_MODE=true")

    session_id = body.get("session_id") or str(uuid.uuid4())
    db.create_session(session_id, ep_id)
    db.append_message(session_id, "user", message)

    segs = db.get_segments(ep_id)
    context = numbered_transcript(segs)
    history = db.get_messages(session_id)
    result = llm.chat(history, context, message, ep.get("podcast", ""), ep.get("title", ""))

    db.append_message(session_id, "assistant", result["answer"])
    return {"session_id": session_id, "answer": result["answer"], "locations": result.get("locations", [])}


# ---------------- 片段整理成文章 ----------------

@app.post("/api/podcasts/{ep_id}/article")
async def organize_article(ep_id: int, req: Request):
    ep = db.get_episode(ep_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    if not DEMO_MODE and not has_llm():
        raise HTTPException(400, "未配置大模型：请设置 LLM_API_KEY 或将 DEMO_MODE=true")
    body = await req.json()
    target_id = body.get("segment_id")
    segs = db.get_segments(ep_id)
    if not segs:
        raise HTTPException(400, "还没有逐字稿")
    idx = next((i for i, s in enumerate(segs) if s["id"] == target_id), 0)
    window = segs[max(0, idx - 2): idx + 3]
    text = transcript_full_text(window)
    article = llm.organize_article(text)
    return {
        "article": {
            "title": f"{ep.get('title', '片段')} · 片段整理",
            "article": article,
            "start_ms": window[0]["start_ms"],
            "end_ms": window[-1]["end_ms"],
            "segment_ids": [s["id"] for s in window],
        }
    }


# ---------------- 笔记 ----------------

@app.post("/api/podcasts/{ep_id}/notes/generate")
async def generate_note(ep_id: int):
    ep = db.get_episode(ep_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    if not DEMO_MODE and not has_llm():
        raise HTTPException(400, "未配置大模型：请设置 LLM_API_KEY 或将 DEMO_MODE=true")
    segs = db.get_segments(ep_id)
    if not segs:
        raise HTTPException(400, "还没有逐字稿，无法生成笔记")
    annotations = [s for s in segs if s.get("is_key") or s.get("include_original") or s.get("note_text")]
    blocks = llm.generate_note(segs, annotations)
    source_mode = "user_annotations" if annotations else "full_episode"
    note_id = db.create_note(
        ep_id, ep.get("title", ""), f"{ep.get('title', '播客')} · 伴读笔记",
        blocks, is_shared=0, source_mode=source_mode,
    )
    return {"note": db.get_note(note_id)}


@app.get("/api/notes")
async def list_notes(limit: int = 50):
    return {"notes": db.list_notes(limit)}


@app.get("/api/notes/{note_id}")
async def get_note(note_id: int):
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "笔记不存在")
    return {"note": note}


@app.put("/api/notes/{note_id}")
async def update_note(note_id: int, req: Request):
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "笔记不存在")
    body = await req.json()
    db.update_note(note_id, title=body.get("title"), document=body.get("document"),
                   is_shared=body.get("is_shared"))
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: int):
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "笔记不存在")
    db.soft_delete_note(note_id)
    return {"ok": True}


@app.get("/api/notes/{note_id}/export")
async def export_note(note_id: int):
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(404, "笔记不存在")
    parts = []
    for b in note.get("document", []):
        if b.get("type") == "annotation":
            parts.append(f'<p style="background:#EDF1F4;border-left:3px solid #3E5A72;padding:8px 12px;color:#3E5A72">{_esc(b.get("text",""))}</p>')
        else:
            parts.append(f'<p>{_esc(b.get("text",""))}</p>')
    html = (f'<html><head><meta charset="utf-8"></head><body>'
            f'<h1>{_esc(note.get("title",""))}</h1>' + "".join(parts) + "</body></html>")
    return Response(html, media_type="application/msword",
                    headers={"Content-Disposition": f'attachment; filename="note-{note_id}.doc"'})


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- 数据备份与恢复 ----------------
# 重要性：Render 等免费云的磁盘是临时的，重新部署会清空。
# 流程：重新部署前打开 /api/backup 下载一份 JSON → 部署完在页面点「导入备份」传回来。

@app.get("/api/backup")
async def backup():
    """导出全库为 JSON 下载（节目 + 逐字稿 + 分析 + 笔记）。"""
    try:
        data = db.export_all()
    except Exception as e:
        raise HTTPException(500, f"导出失败：{e}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    body = json.dumps(data, ensure_ascii=False)
    return Response(
        body, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="podcast-backup-{stamp}.json"'},
    )


@app.post("/api/restore")
async def restore(req: Request):
    """导入备份 JSON。body: {"data": {...备份内容...}, "replace": true/false}

    replace=true 先清空现有数据再导入（部署后恢复用这个）；
    false 则跳过已存在的节目 id（合并用）。
    """
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    payload = body.get("data") if isinstance(body, dict) else None
    if not isinstance(payload, dict) or "episodes" not in payload:
        raise HTTPException(400, "备份内容无效：缺少 episodes 字段")
    replace = bool((body or {}).get("replace", False))
    try:
        stats = db.import_all(payload, replace=replace)
    except Exception as e:
        raise HTTPException(500, f"导入失败：{e}")
    return {"ok": True, "stats": stats}


# ---------------- 前端静态托管 ----------------

# 本地上传的音频：通过 /uploads/ 提供播放（需在 / 之前挂载）
os.makedirs(UPLOAD_DIR, exist_ok=True)
if os.path.isdir(UPLOAD_DIR):
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
