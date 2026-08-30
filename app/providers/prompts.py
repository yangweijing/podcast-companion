"""提示词模板与带时间码的逐字稿格式化。

前端依赖 analysis 里的 start_ms 字段来跳转播放，因此要求大模型在引用
金句/问题/脉络时附带 [mm:ss] 时间码，后端再解析成毫秒。
"""
import re


def fmt_time(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    return f"{s // 60}:{s % 60:02d}"


def build_timestamped_transcript(segments: list) -> str:
    """把逐字稿拼成带 [mm:ss] 时间码的文本，供大模型理解时间位置。"""
    lines = []
    for s in segments:
        ts = f"[{fmt_time(s.get('start_ms', 0))}]"
        speaker = f"{s['speaker']}：" if s.get("speaker") else ""
        lines.append(f"{ts} {speaker}{s.get('text', '')}")
    return "\n".join(lines)


def parse_timestamp_tag(text: str) -> int | None:
    """从文本里提取第一个 [mm:ss] 或 [hh:mm:ss] 时间码，返回毫秒。"""
    if not text:
        return None
    m = re.search(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]", text)
    if not m:
        return None
    mm = int(m.group(1))
    ss = int(m.group(2))
    if m.group(3):
        hh = mm
        mm = ss
        ss = int(m.group(3))
        return (hh * 3600 + mm * 60 + ss) * 1000
    return (mm * 60 + ss) * 1000


# ---------------- 分析（导读） ----------------

def analyze_system_prompt() -> str:
    return (
        "你是一位资深的播客内容编辑，擅长把一期访谈节目整理成「伴读导读」。\n"
        "你会拿到带 [mm:ss] 时间码的逐字稿。请输出严格的 JSON，不要任何额外说明。\n"
        "JSON 结构：\n"
        "{\n"
        '  "summary": "用 2-4 句话概括这期节目讲了一件什么事",\n'
        '  "mainline": {"stages": [{"title": "阶段标题", "summary": "这一阶段讲了什么", "start_ms": 数字}]},\n'
        '  "major_questions": [{"question": "节目探讨的核心问题", "discussion": "讨论过程", "conclusion": "结论(可空)", "start_ms": 数字}],\n'
        '  "quotes": [{"text": "一句值得摘抄的原话", "speaker": "说话人(可空)", "start_ms": 数字, "reason": "为什么值得记"}]\n'
        "}\n"
        "重要：stages/questions/quotes 里的 start_ms 必须引用逐字稿中真实出现的时间码 [mm:ss] "
        "（把 mm:ss 换算成毫秒整数，例如 12:30 -> 750000）。每个数组 3-6 条，金句 4-8 条。"
    )


def analyze_user_prompt(transcript_ts: str) -> str:
    return "以下是带时间码的逐字稿，请按要求生成导读 JSON：\n\n" + transcript_ts


# ---------------- 伴读对话 ----------------

def chat_system_prompt(podcast: str, title: str) -> str:
    return (
        "你是「播客伴读」，一位熟悉这期节目的阅读向导。\n"
        f"节目：{title}（来自播客《{podcast}》）。\n"
        "你会拿到这期节目的逐字稿上下文。请用中文、口语化、有信息量的方式回答用户的问题；\n"
        "如果用户想定位某段内容，请在回答末尾附上一个 JSON 片段（放在 ```loc 代码块里）列出相关片段：\n"
        '[{"segment_id": 数字, "time_label": "mm:ss", "excerpt": "对应的原话摘要"}]\n'
        "segment_id 取你能在上下文中对应的逐字稿序号（从 1 开始）；如无法确定就省略该片段。"
    )


def chat_user_prompt(user_message: str, transcript_context: str) -> str:
    return (
        "=== 本期逐字稿（带时间码，序号从 1 开始）===\n"
        + transcript_context
        + "\n=== 用户提问 ===\n"
        + user_message
    )


# ---------------- 连续笔记 ----------------

def note_system_prompt() -> str:
    return (
        "你是播客伴读笔记助手。你会拿到带时间码的逐字稿，以及听众手动标注的「重点/原文/主观想法」。\n"
        "请输出严格的 JSON 数组，作为一篇可扫读的短笔记。数组元素两种类型：\n"
        '  {"type": "ai", "text": "由你整理的一段通顺、有观点的内容（2-4 句）"}\n'
        '  {"type": "annotation", "text": "听众的主观标注原文", "segment_id": 数字}\n'
        "把听众标注自然穿插在 AI 段落之间（位置贴合其所指的内容），AI 段落负责串联与提炼。\n"
        "总共 8-14 个块，整体像一篇有主线的短文，不要列点式。只输出 JSON 数组。"
    )


def note_user_prompt(transcript_ts: str, annotations: list) -> str:
    anno_txt = ""
    if annotations:
        lines = []
        for a in annotations:
            seg = a.get("segment_id")
            kind = "重点" if a.get("is_key") else ("原文" if a.get("include_original") else "想法")
            note = a.get("note_text") or ""
            line = f"- 第{seg}段 听众标记[{kind}]"
            if note:
                line += f"：{note}"
            lines.append(line)
        anno_txt = "听众标注：\n" + "\n".join(lines) + "\n"
    return anno_txt + "逐字稿：\n" + transcript_ts


# ---------------- 片段整理成文章 ----------------

def article_user_prompt(segments_ts: str) -> str:
    return (
        "把下面的片段轻度整理成一篇通顺的小文章（保留原意与口语感，补上必要的衔接与标点，"
        "去掉重复与口误）。直接输出文章正文，不要标题、不要解释：\n\n" + segments_ts
    )
