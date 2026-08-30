"""语音转写（ASR）。

- whisper_api：调用 OpenAI 兼容的 Whisper 接口（返回带时间码的 segments）
- local_whisper：本机运行 openai/whisper（需 GPU 或耐心，适合短音频）
- demo：规则生成占位逐字稿，无需任何服务即可演示

长音频（>25MB）需要 ffmpeg 做压缩/分段，否则 Whisper API 会拒收；
若环境无 ffmpeg，短音频（≤25MB）仍可正常转写。
"""
import os
import shutil
import subprocess
import tempfile
import uuid

import requests

from config import (ASR_PROVIDER, ASR_MAX_SECONDS, DEMO_MODE, UPLOAD_DIR,
                    VOLC_ASR_API_KEY, VOLC_ASR_RESOURCE_ID,
                    WHISPER_API_BASE, WHISPER_API_KEY, WHISPER_MODEL, XY_UA)

# 火山引擎「豆包语音」录音文件识别极速版：同步返回，最大 100MB / 2 小时
VOLC_ASR_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"

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
    """返回 (可直接读取的本地路径, 需清理的临时文件列表)。

    - 本地上传文件（audio_url 以 /uploads/ 开头）：直接返回磁盘原文件，cleanup 为空，
      不下载、不删除（前端还要继续播放）。
    - 远程 URL：下载到临时文件，必要时 ffmpeg 压缩，cleanup 含临时文件。
    """
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
    return "mp3"  # 播客音频绝大多数是 mp3


def volc_asr_transcribe(audio_url: str) -> list:
    """火山引擎「豆包语音」录音文件识别极速版。

    接口要求 audio.url 必须是**公网可访问**的链接（火山服务器会去拉取），
    因此本地上传的音频（/uploads/...）无法使用，需改用 local_whisper。
    返回带毫秒时间码与说话人标记的分句结果。
    """
    if not audio_url.startswith(("http://", "https://")):
        raise ValueError(
            "火山 ASR 需要公网可访问的音频链接，本地上传的音频（/uploads/...）无法被火山服务器读取。"
            "解决方式二选一：① 改用小宇宙链接导入节目；② 把 ASR_PROVIDER 设为 local_whisper（本机免费转写）。"
        )
    if not VOLC_ASR_API_KEY:
        raise ValueError("未配置 VOLC_ASR_API_KEY（火山语音控制台创建；与方舟大模型 key 不同）")

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": VOLC_ASR_API_KEY,
        "X-Api-Resource-Id": VOLC_ASR_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    payload = {
        "audio": {"url": audio_url, "format": _guess_audio_format(audio_url), "rate": 16000},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,   # 口语数字/金额转书面格式
            "enable_punc": True,  # 自动加标点
            "show_utterances": True,  # 返回分句 + 时间戳 + 说话人
        },
    }
    resp = requests.post(VOLC_ASR_ENDPOINT, headers=headers, json=payload, timeout=900)
    resp.raise_for_status()
    data = resp.json()
    api_msg = (data.get("headers") or {}).get("X-Api-Message", "")
    api_code = str((data.get("headers") or {}).get("X-Api-Status-Code", ""))
    if api_code and api_code != "20000000":
        raise ValueError(f"火山 ASR 调用失败（{api_code} {api_msg}）。"
                         f"常见原因：API Key 无效、未开通豆包语音服务，或音频链接不可访问。")

    result = (data.get("body") or {}).get("result") or {}
    utterances = result.get("utterances") or []
    out = []
    for i, u in enumerate(utterances):
        text = (u.get("text") or "").strip()
        if not text:
            continue
        speaker = (u.get("additions") or {}).get("speaker")
        out.append({
            "id": len(out) + 1,
            "start_ms": int(u.get("start_time") or 0),   # 火山返回毫秒
            "end_ms": int(u.get("end_time") or 0),
            "speaker": f"说话人{speaker}" if speaker else None,
            "text": text,
        })
    if not out:
        # 兜底：接口只给了整段文本，没有分句信息
        text = (result.get("text") or "").strip()
        if text:
            out = [{"id": 1, "start_ms": 0, "end_ms": 0, "speaker": None, "text": text}]
    return out


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
