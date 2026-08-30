"""小宇宙 episode 链接解析。

真实模式：从链接提取 episodeId，调用小宇宙公开 webapi 获取标题/播客/封面/音频/时长。
演示模式：返回一份占位节目（音频用公开示例），让链接添加也能跑通流程。

注意：小宇宙接口与防盗链可能随版本变化，真实解析是最脆弱的一环；
若解析失败，README 里提供了可手动填入音频直链的兜底方式（见 config.env 用法）。
"""
import json
import re

import requests

from config import DEMO_MODE, XY_UA

DEMO_AUDIO = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"


def extract_episode_id(url: str) -> str | None:
    m = re.search(r"/episode/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else None


def _get(url: str, timeout: int = 20) -> requests.Response:
    return requests.get(url, headers={"User-Agent": XY_UA, "Accept": "application/json"}, timeout=timeout)


def parse_xiaoyuzhou(url: str) -> dict:
    epid = extract_episode_id(url)
    if not epid:
        raise ValueError("无法从小宇宙链接中识别 episodeId，请确认是 /episode/ 开头的链接")

    # 方式一：webapi（返回结构化 JSON，最稳）
    try:
        r = _get(f"https://www.xiaoyuzhoufm.com/webapi/v1/episode/get?episodeId={epid}")
        if r.ok:
            data = r.json()
            ep = (data.get("data") or {}).get("episode") or (data.get("result") or {}).get("episode")
            if ep:
                return _normalize(ep, url)
    except Exception:
        pass

    # 方式二：抓 episode 页面，解析 og 标签与内嵌 JSON
    r = _get(f"https://www.xiaoyuzhoufm.com/episode/{epid}")
    if not r.ok:
        raise ValueError(f"小宇宙页面请求失败（HTTP {r.status_code}），可能被限流或网络不可达")
    html = r.text
    title = _meta(html, "og:title") or _meta(html, "title")
    cover = _meta(html, "og:image")
    audio = _meta(html, "og:audio") or _find_audio(html)
    podcast = _meta(html, "og:site_name")
    duration = _find_duration(html)
    if not title:
        raise ValueError("解析到页面但没能提取到标题，小宇宙页面结构可能已变动")
    return {
        "title": title,
        "podcast": podcast or "",
        "cover_url": cover,
        "audio_url": audio,
        "duration_ms": duration,
        "source_url": url,
    }


def _normalize(ep: dict, url: str) -> dict:
    podcast = ep.get("podcast") or {}
    pod_name = podcast.get("title") if isinstance(podcast, dict) else ""
    dur = ep.get("duration") or ep.get("durationMs") or 0
    if isinstance(dur, (int, float)):
        dur_ms = int(dur * 1000) if dur < 100000 else int(dur)  # 兼容秒/毫秒
    else:
        dur_ms = 0
    return {
        "title": ep.get("title") or ep.get("name") or "（无标题）",
        "podcast": pod_name,
        "cover_url": ep.get("cover") or ep.get("image") or ep.get("artwork"),
        "audio_url": ep.get("audio") or ep.get("enclosureUrl") or ep.get("mediaUrl"),
        "duration_ms": dur_ms,
        "source_url": url,
    }


def _meta(html: str, prop: str) -> str | None:
    m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]+)"', html, re.I)
    if m:
        return m.group(1)
    m = re.search(rf'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="{re.escape(prop)}"', html, re.I)
    return m.group(1) if m else None


def _find_audio(html: str) -> str | None:
    m = re.search(r'https?://[^\s"\\]+\.(?:m4a|mp3)(?:\?[^"\s\\]*)?', html)
    return m.group(0) if m else None


def _find_duration(html: str) -> int:
    m = re.search(r'"duration"\s*:\s*(\d+)', html)
    if m:
        v = int(m.group(1))
        return v * 1000 if v < 100000 else v
    return 0


def parse_demo(url: str) -> dict:
    epid = extract_episode_id(url) or "demo"
    return {
        "title": f"示例节目（演示）：{epid}",
        "podcast": "演示播客",
        "cover_url": "",
        "audio_url": DEMO_AUDIO,
        "duration_ms": 600000,
        "source_url": url,
    }


def parse(url: str) -> dict:
    if DEMO_MODE:
        return parse_demo(url)
    return parse_xiaoyuzhou(url)
