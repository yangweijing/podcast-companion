"""播客 / 音频节目链接解析（多平台）。

当前支持的单集链接来源（按链接域名自动识别，粘贴哪种都行）：
  1. 小宇宙      xiaoyuzhoufm.com   —— episodeId → 官方 webapi / 页面 og 标签
  2. 喜马拉雅    ximalaya.com        —— soundId → 优先走移动端公开接口 tracks/{id}.json
                                        （免登录、快、能区分"已下架/付费"）；
                                        拿不到音频时回退 yt-dlp 签名链路 → 网页 og 抓取
  3. 网易云播客  music.163.com / y.music.163.com —— program id → dj 详情接口拿元信息 +
                                          song/media/outer/url 拿可播放音频

真实模式：请求各平台公开接口/页面，解析标题、播客、封面、音频直链、时长。
演示模式：返回占位节目（音频用公开示例），让链接添加也能跑通流程。

⚠️ 稳定性质检：三个平台都没有面向第三方的官方 API，以下接口都属网页/公开接口抓取，
可能随平台改版或反爬而失效。若解析失败，main.py 会回抛「解析失败：xxx」，前端可引导
用户改用「上传本地音频」兜底。

⚠️ 喜马拉雅：单集页本身是浏览器签名链路（纯 requests 抓不到），但存在免登录的移动端公开
接口 m.ximalaya.com/tracks/{soundId}.json，可直接拿到标题/专辑名/时长/封面/音频直链，因此
把它作为主路径。付费(VIP)集该接口只给元信息不给音频，此时回退到 yt-dlp（其内置 ximalaya
提取器维护了 mpay 签名解密），故 yt-dlp 是"可选增强"而非必需：不装也能解析免费单集。
"""
import os
import re
import shutil
import subprocess
import sys

import requests

from config import DEMO_MODE, XIMALAYA_COOKIE, XY_UA

DEMO_AUDIO = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"

# 各平台请求头（喜马拉雅 / 网易云对小宇宙的 UA 兼容性不同，拆开更稳）
_HEADERS = {
    "xiaoyuzhou": {"User-Agent": XY_UA, "Accept": "application/json"},
    "ximalaya": {
        "User-Agent": XY_UA,
        "Referer": "https://www.ximalaya.com/",
        "Accept": "application/json, text/plain, */*",
    },
    "netease": {
        "User-Agent": XY_UA,
        "Referer": "https://music.163.com/",
        "Accept": "application/json, text/plain, */*",
    },
}


def _get(url: str, host: str = "xiaoyuzhou", timeout: int = 20) -> requests.Response:
    return requests.get(url, headers=_HEADERS.get(host, _HEADERS["xiaoyuzhou"]), timeout=timeout)


# ============================================================
# 一、小宇宙（保留原逻辑）
# ============================================================

def extract_episode_id(url: str) -> str | None:
    m = re.search(r"/episode/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else None


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
                return _normalize_xy(ep, url)
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


def _normalize_xy(ep: dict, url: str) -> dict:
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


# ============================================================
# 二、喜马拉雅（单集）
#
# 实测结论（2026-09）：喜马拉雅单集数据有两条可用链路
#   ① 移动端公开接口 https://m.ximalaya.com/tracks/{soundId}.json
#      —— 免登录、免签名，直接回 title/album_title/duration/cover_url/play_path_64，
#         且能精确区分「已下架(res:false)」与「付费(is_paid, play_path 全 null)」。
#         这是主路径：快（无子进程）、准（能给出正确失败原因）。
#   ② yt-dlp 内置 ximalaya 提取器
#      —— 维护了 VIP 付费集的签名解密链路（mpay.ximalaya.com），
#         主路径拿不到音频时用它兜底。
# 原先只走 ②（子进程 + 60s 超时），既慢又会把「ID 已下架」误报成「付费内容」，
# 因此改为 ① 主 + ② 兜底 + ③ 网页 og 兜底。
# ============================================================

# 移动端 UA：tracks.json 接口对移动 UA 更宽容
_XM_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")


def _https(u: str | None) -> str:
    """喜马拉雅返回的封面/音频常是 http:// 或 // 开头。

    前端页面跑在 https 下时，http 资源会被浏览器按混合内容拦截，这里统一升级。
    """
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://"):
        return "https://" + u[len("http://"):]
    return u


def extract_sound_id(url: str) -> str | None:
    """喜马拉雅单集 sound id。

    兼容形态：
      https://www.ximalaya.com/sound/6551234
      https://www.ximalaya.com/waiyu/3373990/222313675      （/分类/专辑/单集）
      https://www.ximalaya.com/43755913/sound/355188898      （/主播/sound/单集）
      https://xima.tv/1_xxx（短链，会 302 到含 sound id 的真实页；本函数对短链返回 None，
                             由 parse_ximalaya 负责先跟随短链再取 id）
    取链接末尾一段纯数字作为 soundId。
    """
    if not url:
        return None
    m = re.search(r"/sound/(\d+)", url)
    if m:
        return m.group(1)
    m = re.findall(r"(?:^|/)(\d+)(?:/|$|(?=\?))", url)
    return m[-1] if m else None


def _resolve_short(url: str) -> str:
    """跟随喜马拉雅短链/落地链，返回带 sound id 的真实页面 URL。"""
    if "xima.tv" not in url:
        return url
    try:
        r = requests.get(url, headers=_HEADERS["ximalaya"], timeout=15, allow_redirects=True)
        return r.url or url
    except Exception:
        return url


def _find_ytdlp() -> str | None:
    """定位可用的 yt-dlp 二进制。返回绝对路径或 None。"""
    # 1) 环境变量显式指定
    env = os.environ.get("YOUTUBE_DL_PATH") or os.environ.get("YTDLP")
    if env and os.path.isfile(env):
        return env
    # 2) PATH 中查找
    hit = shutil.which("yt-dlp") or shutil.which("youtube-dl")
    if hit:
        return hit
    # 3) 常见绝对路径兜底（含本项目 userspace venv）
    for p in (
        "/Users/mac/.workbuddy/binaries/python/envs/default/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        "/opt/homebrew/bin/yt-dlp",
        "/usr/bin/yt-dlp",
    ):
        if os.path.isfile(p):
            return p
    return None


def _ytdlp_parse(url: str, bin_path: str) -> dict | None:
    """调 yt-dlp 提取单集信息。成功返回归一 dict，失败返回 None。

    只做元信息探测（--skip-download），不实际下载音频。
    """
    try:
        proc = subprocess.run(
            [bin_path, "-J", "--skip-download", "--no-warnings",
             "--socket-timeout", "20", url],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        import json
        d = json.loads(proc.stdout or "{}")
    except Exception:
        return None
    if not d or not d.get("title"):
        return None
    # 优选非该节目封面图的算法：uploader 作为播客名
    audio_url = d.get("url") or ""
    # formats 里若带独立 url 但顶层无 url 时，取首个非 -1 的 url
    if not audio_url:
        for f in d.get("formats") or []:
            u = f.get("url")
            if u and f.get("format_id") != "-1":
                audio_url = u
                break
    duration_s = d.get("duration") or 0
    return {
        "title": (d.get("title") or "").strip(),
        "podcast": (d.get("uploader") or "").strip(),
        "cover_url": d.get("thumbnail") or "",
        # 注意：不要截断 query。喜马拉雅 VIP 直链的签名就在 query 里
        # （sign / token / timestamp / buy_key），截掉就 403 了。
        "audio_url": audio_url or "",
        "duration_ms": int(duration_s) * 1000 if duration_s else 0,
    }


def _fetch_ximalaya_meta(html: str) -> dict:
    """从喜马拉雅节目页抓 og 元信息 + 内嵌时长（配 Cookie 后页面会返回真实内容）。"""
    title = _meta(html, "og:title") or _meta(html, "og:audio:title") or _meta(html, "title")
    cover = _meta(html, "og:image")
    duration = 0
    m = re.search(r'"duration"\s*:\s*(\d+)', html)
    if m:
        v = int(m.group(1))
        duration = v * 1000 if v < 100000 else v
    return {"title": title, "cover_url": cover, "duration_ms": duration}


def _ximalaya_headers(mobile: bool = False):
    h = dict(_HEADERS["ximalaya"])
    if mobile:
        h["User-Agent"] = _XM_MOBILE_UA
    if XIMALAYA_COOKIE:
        h["Cookie"] = XIMALAYA_COOKIE
    return h


def _is_album_url(url: str) -> bool:
    """专辑主页（/album/数字 或 /分类/album/数字）不是单集，需单独提示。"""
    return bool(re.search(r"/album/\d+", url or ""))


def _ximalaya_tracks(sound_id: str) -> dict | None:
    """主路径：请求移动端公开接口拿单集元信息。

    返回归一 dict（付费集 audio_url 为空、_paid=True）；无法解析时返回 None。
    """
    try:
        r = requests.get(f"https://m.ximalaya.com/tracks/{sound_id}.json",
                         headers=_ximalaya_headers(mobile=True), timeout=20)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    # 已下架 / ID 不存在时，接口会返回 {"res": false, ...}
    if not d or d.get("res") is False or not d.get("id"):
        return None

    # 64k 音质优先，其次默认、32k
    audio = d.get("play_path_64") or d.get("play_path") or d.get("play_path_32")
    dur = int(d.get("duration") or 0)
    return {
        "title": (d.get("title") or "").strip() or f"喜马拉雅单集 {sound_id}",
        # 专辑名比主播名更接近「播客」语义，优先取专辑名
        "podcast": (d.get("album_title") or d.get("nickname") or "").strip(),
        "cover_url": _https(d.get("cover_url") or d.get("cover_url_142")),
        "audio_url": _https(audio) if audio else "",
        "duration_ms": dur * 1000 if dur else 0,
        "_paid": bool(d.get("is_paid")) or not audio,
    }


def _signed_link_warn(audio_url: str) -> str | None:
    """付费集走签名链路拿到的直链带 sign/token/timestamp，通常 1 天左右过期。

    转写在添加节目时就会跑完，所以不影响字幕生成；仅提示「久后重播可能失效」。
    """
    if audio_url and re.search(r"[?&]sign=", audio_url):
        return ("该集为付费内容，取到的是带时效签名的音频直链（约 1 天内有效）。"
                "转写不受影响；但隔天后再播放可能需要重新添加。")
    return None


def _ytdlp_try(sound_id: str) -> dict | None:
    """兜底路径：用 yt-dlp 的 ximalaya 提取器再试一次（主要为了 VIP 签名直链）。

    统一用规范 URL https://www.ximalaya.com/sound/{id} —— 提取器只认这种形态，
    原始链接可能是 xima.tv 短链或 m. 端带参长链。
    """
    ytdlp_bin = _find_ytdlp()
    if not ytdlp_bin:
        return None
    info = _ytdlp_parse(f"https://www.ximalaya.com/sound/{sound_id}", ytdlp_bin)
    if not info or not info.get("title"):
        return None
    info["cover_url"] = _https(info.get("cover_url"))
    info["audio_url"] = _https(info.get("audio_url"))
    return info


def parse_ximalaya(url: str) -> dict:
    # 短链先跟随一次，拿到真实 /sound/{id} 页
    real = _resolve_short(url)

    if _is_album_url(real):
        raise ValueError("检测到这是喜马拉雅「专辑主页」链接；请打开具体某一集后，"
                         "复制那一集的单集链接（形如 ximalaya.com/sound/数字）")

    sound_id = extract_sound_id(real)
    if not sound_id:
        raise ValueError("无法从喜马拉雅链接识别单集 ID（soundId），请粘贴某一条音频的单集分享链接")

    # ---- 路径①：tracks.json 公开接口（快，且能区分失败原因）----
    info = _ximalaya_tracks(sound_id)
    if info:
        paid = info.pop("_paid", False)
        if info.get("audio_url"):
            info["source_url"] = url
            info["_warn"] = None
            return info
        # 拿得到元信息但没音频（付费/会员）→ 换 yt-dlp 走签名链路再试
        alt = _ytdlp_try(sound_id)
        if alt and alt.get("audio_url"):
            alt["source_url"] = url
            alt["_warn"] = _signed_link_warn(alt["audio_url"])
            return alt
        info["source_url"] = url
        info["_warn"] = ("该集为喜马拉雅付费/会员内容，未取到可播放音频，仅导入了标题与封面"
                         if paid else "未取到可播放音频，仅导入了标题与封面")
        return info

    # ---- 路径②：yt-dlp（tracks.json 拿不到时的兜底）----
    alt = _ytdlp_try(sound_id)
    if alt and (alt.get("audio_url") or alt.get("title")):
        alt["source_url"] = url
        alt["_warn"] = _signed_link_warn(alt["audio_url"]) if alt.get("audio_url") \
            else "仅取到标题，未取到可播放音频"
        return alt

    # ---- 路径③：网页 og 抓取（带 Cookie 时可能拿到点东西）----
    title = cover = ""
    duration_ms = 0
    try:
        r = requests.get(f"https://www.ximalaya.com/sound/{sound_id}",
                         headers=_ximalaya_headers(), timeout=20)
        if r.ok:
            meta = _fetch_ximalaya_meta(r.text)
            title, cover = meta["title"] or "", _https(meta["cover_url"])
            duration_ms = meta["duration_ms"]
    except Exception:
        pass

    if title:
        return {
            "title": title,
            "podcast": "",
            "cover_url": cover,
            "audio_url": "",
            "duration_ms": duration_ms,
            "source_url": url,
            "_warn": "仅取到标题，未取到可播放音频",
        }

    # ---- 全部失败：给出可操作的准确提示 ----
    if not _find_ytdlp():
        raise ValueError(
            "未能取到该喜马拉雅单集的数据。"
            "兜底方案依赖 yt-dlp（其内置喜马拉雅签名提取器），但当前环境找不到 yt-dlp；"
            "请 pip install yt-dlp 后重启服务。也可直接改用「上传本地音频」导入。"
        )
    raise ValueError(
        f"喜马拉雅单集 {sound_id} 取不到数据，通常是该集已下架/删除，"
        "或链接里的单集 ID 不对。请换一集免费内容重试，或改用「上传本地音频」导入。"
    )


# ============================================================
# 三、网易云音乐·播客（单集 program）
# ============================================================

def extract_program_id(url: str) -> str | None:
    """网易云播客单集链接里的 program id。

    兼容形态：
      https://music.163.com/#/program?id=3721243332
      https://y.music.163.com/m/program?id=3721243332&userid=xxx
      https://music.163.com/#/djradio?id=12  ← 这是电台主页，非单集，会提示用户换成单集链接
    """
    m = re.search(r"[?&]id=(\d+)", url or "")
    return m.group(1) if m else None


def parse_netease_podcast(url: str) -> dict:
    program_id = extract_program_id(url)
    if not program_id:
        raise ValueError("无法从网易云链接识别单集 program id，请粘贴某一条播客单集链接"
                         "（形如 music.163.com/#/program?id=数字）")
    if "#/djradio" in url or "/djradio" in url:
        raise ValueError("检测到这是电台主页链接；请打开具体某一集后，复制那集的节目链接（链接里应含 program?id=）")

    # 方式一：dj 详情接口拿元信息（标题/封面/时长/主播/主歌id）
    detail = None
    try:
        r = _get(f"https://music.163.com/api/dj/program/detail?id={program_id}", host="netease")
        if r.ok:
            d = r.json()
            if d.get("code") == 200 and (d.get("program") or {}).get("id"):
                detail = d["program"]
    except Exception:
        pass
    if not detail:
        raise ValueError("网易云节目详情获取失败（可能该集已下架或接口变动），请改用上传本地音频")

    main_song = detail.get("mainSong") or {}
    song_id = main_song.get("id")
    radio = detail.get("radio") or {}
    cover = detail.get("coverUrl") or main_song.get("album", {}).get("picUrl")
    duration_ms = int(detail.get("duration") or main_song.get("duration") or 0)
    title = detail.get("name") or main_song.get("name") or "（无标题）"
    podcast = radio.get("name") or ""
    if not podcast:
        # 主播名兜底
        anchors = detail.get("dj") or {}
        podcast = anchors.get("nickname") or ""

    # 音频：网易云公开外链播放接口（无需加密签名）
    audio_url = None
    if song_id:
        audio_url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"

    if not audio_url:
        raise ValueError("未能从网易云解析出音频地址")

    return {
        "title": title,
        "podcast": podcast,
        "cover_url": cover,
        "audio_url": audio_url,
        "duration_ms": duration_ms,
        "source_url": url,
    }


# ============================================================
# 四、演示模式 & 统一入口
# ============================================================

def parse_demo(url: str) -> dict:
    label = "小宇宙"
    m = re.search(r"xiaoyuzhoufm", url or "")
    if not m:
        m = re.search(r"ximalaya", url or "")
        label = "喜马拉雅" if m else label
        if not m:
            m = re.search(r"music\.163", url or "")
            label = "网易云播客" if m else label
    return {
        "title": f"示例节目（演示·{label}）",
        "podcast": "演示播客",
        "cover_url": "",
        "audio_url": DEMO_AUDIO,
        "duration_ms": 600000,
        "source_url": url,
    }


def parse(url: str) -> dict:
    if DEMO_MODE:
        return parse_demo(url)
    u = (url or "").strip().lower()
    if "xiaoyuzhoufm.com" in u:
        return parse_xiaoyuzhou(url)
    if "ximalaya.com" in u or "xima.tv" in u:
        return parse_ximalaya(url)
    if "music.163.com" in u or "y.music.163.com" in u or "163cn.tv" in u:
        return parse_netease_podcast(url)
    raise ValueError("暂不支持的平台链接。目前支持：小宇宙(xiaoyuzhoufm.com)、"
                     "喜马拉雅(ximalaya.com)、网易云播客(music.163.com)；或改用上传本地音频。")
