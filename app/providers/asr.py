"""语音转写（ASR）。

- whisper_api：调用 OpenAI 兼容的 Whisper 接口（返回带时间码的 segments）
- local_whisper：本机运行 openai/whisper（需 GPU 或耐心，适合短音频）
- demo：规则生成占位逐字稿，无需任何服务即可演示

长音频（>25MB）需要 ffmpeg 做压缩/分段，否则 Whisper API 会拒收；
若环境无 ffmpeg，短音频（≤25MB）仍可正常转写。
"""
import base64
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import requests

from config import (ASR_PROVIDER, ASR_MAX_SECONDS, DEMO_MODE, UPLOAD_DIR,
                    VOLC_ASR_API_KEY, VOLC_ASR_RESOURCE_ID,
                    WHISPER_API_BASE, WHISPER_API_KEY, WHISPER_MODEL, XY_UA)

# 火山引擎「豆包语音」录音文件识别模型2.0：异步 submit + query 轮询
VOLC_ASR_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
VOLC_ASR_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"

_DEMO_LINES = [
    "今天想和大家聊一个最近让我挺感触的话题。",
    "我们总以为努力就会有回报，但现实往往不是线性的。",
    "我采访过很多企业，发现真正拉开差距的，是信息获取的效率。",
    "很多人把忙碌当成了勤奋，其实只是在重复低价值的动作。",
    "如果你能把一件事讲清楚，就已经胜过大多数人了。",
    "所以这一期，我想从一个具体的案例讲起。",
    "他原来在一家大厂做中层，后来决定出来自己做。",
    "最开始所有人都反对，包括他家人。",
    "但他算了一笔账，发现继续待下去的机会成本其实更高。",
    "关键不是冒险，而是把风险量化之后再做决定。",
    "半年后他告诉我，最大的收获不是钱，而是掌控感。",
    "我觉得这就是普通人能抓住的、为数不多的杠杆。",
]


def _download(audio_url: str) -> str:
    resp = requests.get(audio_url, headers={"User-Agent": XY_UA, "Referer": "https://www.xiaoyuzhoufm.com"},
                        stream=True, timeout=180)
    resp.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".audio")
    os.close(fd)
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    return path


def _ffmpeg_compress(src: str) -> str | None:
    if not shutil.which("ffmpeg"):
        return None
    fd, out = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", "-b:a", "48k", out]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    except Exception:
        return None
    return out


def _acquire_audio(audio_url: str):
    """返回 (可直接读取的本地路径, 需清理的临时文件列表)。"""
    if audio_url.startswith("/uploads/"):
        local = os.path.join(UPLOAD_DIR, os.path.basename(audio_url.split("?", 1)[0]))
        if os.path.exists(local):
            return local, []
    path = _download(audio_url)
    compressed = _ffmpeg_compress(path)
    return (compressed or path), [p for p in (path, compressed) if p]


def _to_segments(whisper_segments: list) -> list:
    out = []
    for i, s in enumerate(whisper_segments):
        start = int(getattr(s, "start", 0) * 1000)
        end = int(getattr(s, "end", 0) * 1000)
        text = (getattr(s, "text", "") or "").strip()
        out.append({"id": i + 1, "start_ms": start, "end_ms": end, "speaker": None, "text": text})
    return out


def whisper_api_transcribe(audio_url: str) -> list:
    from openai import OpenAI
    readable, cleanup = _acquire_audio(audio_url)
    try:
        client = OpenAI(api_key=WHISPER_API_KEY, base_url=WHISPER_API_BASE)
        with open(readable, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                language="zh",
            )
        segs = getattr(resp, "segments", None) or []
        return _to_segments(segs)
    finally:
        for p in cleanup:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def local_whisper_transcribe(audio_url: str) -> list:
    import whisper
    from config import LOCAL_WHISPER_MODEL
    readable, cleanup = _acquire_audio(audio_url)
    try:
        model = whisper.load_model(LOCAL_WHISPER_MODEL)
        result = model.transcribe(readable, language="zh", verbose=False)
        segs = result.get("segments", [])
        return _to_segments(segs)
    finally:
        for p in cleanup:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def _guess_audio_format(url: str) -> str:
    """从 URL 推断音频格式，火山 ASR 的 audio.format 需要显式声明。"""
    path = url.split("?", 1)[0].lower()
    for ext in ("wav", "mp3", "ogg", "pcm", "spx", "amr", "aac", "m4a"):
        if path.endswith("." + ext):
            return ext
    return "mp3"


def _find_ffmpeg() -> str | None:
    """找 ffmpeg：优先 PATH，其次用户机器上常见的固定安装位置。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for c in (
        os.path.expanduser("~/.workbuddy/tools/ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ):
        if c and os.path.exists(c):
            return c
    return None


def _upload_to_volc_audio(audio_url: str) -> dict:
    """把本地上传的音频（/uploads/...）压缩并转成火山可直接识别的 base64 data。

    火山 submit 的 audio 对象支持两种来源：
      - 公网 URL（audio.url）
      - 请求体直传（audio.data = base64），无需公网地址 —— 本地上传走这条路。
    """
    local = os.path.join(UPLOAD_DIR, os.path.basename(audio_url.split("?", 1)[0]))
    if not os.path.exists(local):
        raise ValueError(f"本地上传的音频文件不存在：{local}")

    src, cleanup = local, []
    # 优先压缩成 16kHz 单声道 mp3（体积小、转写更快、base64 直传更稳）
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        fd, out = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        cmd = [ffmpeg, "-y", "-i", local, "-ar", "16000", "-ac", "1", "-b:a", "48k", out]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
            src, cleanup = out, [out]
        except Exception:
            pass  # 压缩失败则用原始文件（格式按扩展名推断）

    try:
        size = os.path.getsize(src)
        if size > 90 * 1024 * 1024:
            raise ValueError("音频文件过大（火山转写上限约 90MB），请先裁剪后再上传。")
        with open(src, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        audio = {"data": b64}
        fmt = _guess_audio_format(src)
        if fmt:
            audio["format"] = fmt
        return audio
    finally:
        for p in cleanup:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def volc_asr_transcribe(audio_url: str) -> list:
    """火山引擎「豆包语音」录音文件识别模型2.0（异步 submit + query）。

    支持两种音频来源：
      - 公网链接（http/https）→ audio.url 方式
      - 本地上传（/uploads/...）→ 自动压缩成 16kHz mp3 后 base64 直传（audio.data），无需公网地址
    """
    if not VOLC_ASR_API_KEY:
        raise ValueError("未配置 VOLC_ASR_API_KEY（火山语音控制台创建；与方舟大模型 key 不同）")

    request_id = str(uuid.uuid4())
    common_headers = {
        "Content-Type": "application/json",
        "X-Api-Key": VOLC_ASR_API_KEY,
        "X-Api-Resource-Id": VOLC_ASR_RESOURCE_ID,
        "X-Api-Request-Id": request_id,
    }
    if audio_url.startswith("/uploads/"):
        audio = _upload_to_volc_audio(audio_url)  # {"data": ..., "format": ...}
    elif audio_url.startswith(("http://", "https://")):
        audio = {"url": audio_url, "format": _guess_audio_format(audio_url), "rate": 16000}
    else:
        raise ValueError("无法识别的音频来源（仅支持 http/https 链接，或本地上传的 /uploads/... 路径）")
    payload = {
        "audio": audio,
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
        },
    }

    # 1) 提交任务
    submit = requests.post(VOLC_ASR_SUBMIT, headers=common_headers, json=payload, timeout=60)
    submit.raise_for_status()
    submit_data = submit.json()
    _check_volc_status(submit_data, "submit")

    # 2) 轮询查询结果
    query_payload = {}
    last = None
    for _ in range(120):  # 最多约 10 分钟
        time.sleep(5)
        q = requests.post(VOLC_ASR_QUERY, headers=common_headers, json=query_payload, timeout=60)
        q.raise_for_status()
        qd = q.json()
        last = qd
        print(f"[火山 ASR 轮询] {_ + 1}/120: {_peek_status(qd)}")
        if _check_volc_done(qd):
            print("[火山 ASR] 转写完成")
            break
    else:
        raise ValueError("火山 ASR 转写超时（轮询 10 分钟仍未完成），请稍后重试或更换音频。")

    # 3) 解析结果：兼容 submit/query 不同返回层级
    result = _extract_volc_result(last)
    utterances = result.get("utterances") or []
    out = []
    for i, u in enumerate(utterances):
        text = (u.get("text") or "").strip()
        if not text:
            continue
        speaker = (u.get("additions") or {}).get("speaker")
        out.append({
            "id": len(out) + 1,
            "start_ms": int(u.get("start_time") or 0),
            "end_ms": int(u.get("end_time") or 0),
            "speaker": f"说话人{speaker}" if speaker else None,
            "text": text,
        })
    if not out:
        text = (result.get("text") or "").strip()
        if text:
            out = [{"id": 1, "start_ms": 0, "end_ms": 0, "speaker": None, "text": text}]
    return out


def _check_volc_status(data: dict, stage: str) -> None:
    code = str(data.get("code", ""))
    message = data.get("message") or data.get("msg") or ""
    if code and code not in ("1000", "0", ""):
        raise ValueError(f"火山 ASR {stage} 失败（code={code} {message}）。"
                        f"常见原因：API Key 无效、未开通豆包语音服务，或 Resource-Id 不匹配。")


def _check_volc_done(data: dict) -> bool:
    """判断查询返回是否已转写完成。"""
    if not isinstance(data, dict):
        return False
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    status = str(inner.get("status", "") or data.get("status", "")).lower().strip()
    if status in ("done", "success", "completed", "succeed", "finish", "finished", "2", "3"):
        return True
    # 火山 bigmodel 2.0 的 query 返回经常 status/code 都为 None，但 result 已经就绪
    result = _extract_volc_result(data)
    if result and (result.get("utterances") or result.get("text")):
        return True
    code = str(data.get("code", ""))
    if code in ("1000", "0") and result:
        return True
    return False


def _peek_status(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)[:120]
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    status = inner.get("status") or data.get("status")
    code = data.get("code")
    has_result = bool(_extract_volc_result(data))
    return f"status={status} code={code} has_result={has_result}"


def _extract_volc_result(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    cand = data.get("result")
    if isinstance(cand, dict):
        return cand
    inner = data.get("data")
    if isinstance(inner, dict):
        cand = inner.get("result")
        if isinstance(cand, dict):
            return cand
    return {}


def demo_segments() -> list:
    out = []
    t = 0
    step = 28000
    for i, line in enumerate(_DEMO_LINES):
        out.append({"id": i + 1, "start_ms": t, "end_ms": t + step, "speaker": None, "text": line})
        t += step
    return out


def transcribe(audio_url: str) -> list:
    if DEMO_MODE:
        return demo_segments()
    if ASR_PROVIDER == "volc_asr":
        return volc_asr_transcribe(audio_url)
    if ASR_PROVIDER == "whisper_api":
        return whisper_api_transcribe(audio_url)
    if ASR_PROVIDER == "local_whisper":
        return local_whisper_transcribe(audio_url)
    raise ValueError("未配置可用的转写提供者（ASR_PROVIDER）")
