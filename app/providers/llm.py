"""大模型调用（OpenAI 兼容接口，DeepSeek / 通义 / OpenAI 通用）。

四个能力：分析（导读）、伴读对话、连续笔记、片段整理成文章。
无 key 时走 DEMO_MODE 的规则生成，保证全流程可演示。
"""
import json
import re

from config import (DEMO_MODE, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT)
from providers import prompts


def _client():
    from openai import OpenAI
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _complete(system: str, user: str, temperature: float = 0.4) -> str:
    client = _client()
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
        timeout=LLM_TIMEOUT,
    )
    return resp.choices[0].message.content or ""


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    raise ValueError("模型未返回可解析的 JSON")


def _overlap(a: str, b: str) -> int:
    sa = set(a or "")
    return sum(1 for ch in (b or "") if ch in sa)


def _nearest_start_ms(segments: list, text: str) -> int:
    best, best_score = 0, -1
    for s in segments:
        score = _overlap(text, s.get("text", ""))
        if score > best_score:
            best_score, best = score, s.get("start_ms", 0)
    return best


# ---------------- 分析（导读） ----------------

def analyze(segments: list) -> dict:
    if DEMO_MODE:
        return _demo_analysis(segments)
    ts = prompts.build_timestamped_transcript(segments)
    raw = _complete(prompts.analyze_system_prompt(), prompts.analyze_user_prompt(ts), temperature=0.3)
    data = _extract_json(raw)
    for arr in ("major_questions", "quotes"):
        for item in data.get(arr, []):
            if not item.get("start_ms"):
                item["start_ms"] = prompts.parse_timestamp_tag(item.get("text", "")) or _nearest_start_ms(segments, item.get("text", ""))
    for st in data.get("mainline", {}).get("stages", []):
        if not st.get("start_ms"):
            st["start_ms"] = prompts.parse_timestamp_tag(st.get("summary", "")) or _nearest_start_ms(segments, st.get("summary", ""))
    return data


# ---------------- 伴读对话 ----------------

def chat(history: list, transcript_context: str, user_message: str, podcast: str, title: str) -> dict:
    if DEMO_MODE:
        return _demo_chat(user_message, transcript_context)
    messages = [{"role": "system", "content": prompts.chat_system_prompt(podcast, title)}]
    for m in (history or [])[-10:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": prompts.chat_user_prompt(user_message, transcript_context)})
    client = _client()
    resp = client.chat.completions.create(model=LLM_MODEL, messages=messages, temperature=0.5, timeout=LLM_TIMEOUT)
    text = resp.choices[0].message.content or ""
    answer, loc_raw = _split_loc(text)
    return {"answer": answer, "locations": _parse_loc(loc_raw)}


def _split_loc(text: str):
    m = re.search(r"```loc\s*(.*?)```", text, re.S)
    if m:
        return text[: m.start()].strip(), m.group(1)
    return text.strip(), None


def _parse_loc(raw: str) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict) and it.get("segment_id"):
            out.append({"segment_id": int(it["segment_id"]),
                        "time_label": it.get("time_label", ""), "excerpt": it.get("excerpt", "")})
    return out


# ---------------- 连续笔记 ----------------

def generate_note(segments: list, annotations: list) -> list:
    if DEMO_MODE:
        return _demo_note(segments, annotations)
    ts = prompts.build_timestamped_transcript(segments)
    raw = _complete(prompts.note_system_prompt(), prompts.note_user_prompt(ts, annotations), temperature=0.5)
    data = _extract_json(raw)
    if not isinstance(data, list):
        raise ValueError("笔记生成结果应为 JSON 数组")
    blocks = []
    for b in data:
        blocks.append({
            "type": b.get("type") if b.get("type") in ("ai", "annotation") else "ai",
            "text": b.get("text", ""),
            "segment_id": b.get("segment_id"),
        })
    return blocks


# ---------------- 片段整理成文章 ----------------

def organize_article(segments_text: str) -> str:
    if DEMO_MODE:
        return segments_text.strip()
    return _complete(
        "你是文字整理助手，把口语片段轻度整理成通顺短文，保留原意与口语感。",
        prompts.article_user_prompt(segments_text), temperature=0.4,
    ).strip()


# ---------------- 演示模式（规则生成） ----------------

def _demo_analysis(segments: list) -> dict:
    if not segments:
        return {"summary": "（演示）节目内容摘要。", "mainline": {"stages": []}, "major_questions": [], "quotes": []}
    summary = "（演示）" + " ".join(s["text"] for s in segments[:3])
    mid = len(segments) // 2
    stages = [
        {"title": "从困惑出发", "summary": "嘉宾讲述了对「努力=回报」这一假设的怀疑。", "start_ms": segments[0]["start_ms"]},
        {"title": "一个具体案例", "summary": "以一位大厂中层离职创业的选择展开。", "start_ms": segments[mid]["start_ms"]},
    ]
    questions = [{
        "question": "忙碌等于勤奋吗？",
        "discussion": "节目指出很多人把低价值重复当成努力。",
        "conclusion": "要把忙碌量化成有效动作。",
        "start_ms": segments[min(2, len(segments) - 1)]["start_ms"],
    }]
    quotes = [{"text": s["text"], "speaker": "", "start_ms": s["start_ms"], "reason": "（演示）一句话点出一期主旨"}
              for s in segments[:4]]
    return {"summary": summary, "mainline": {"stages": stages}, "major_questions": questions, "quotes": quotes}


def _demo_chat(user_message: str, context: str) -> dict:
    answer = ("（演示模式）我基于这期节目的内容回答你的问题：「" + user_message +
              "」——核心观点是：把信息效率当作杠杆，比单纯增加工作时长更有用。"
              "接好 LLM 的 API key 后，我会给出真正基于逐字稿的回答。")
    m = re.search(r"^(\d+)\.\s*\[(\d+):(\d+)\]", context or "", re.M)
    seg_id = int(m.group(1)) if m else 1
    return {"answer": answer, "locations": [{"segment_id": seg_id, "time_label": "00:00", "excerpt": "（演示）相关片段"}]}


def _demo_note(segments: list, annotations: list) -> list:
    blocks = []
    for s in segments:
        blocks.append({"type": "ai", "text": s["text"]})
        for a in (annotations or []):
            if a.get("segment_id") == s.get("id"):
                blocks.append({"type": "annotation", "text": a.get("note_text") or "（听众标注）", "segment_id": s.get("id")})
    if not blocks:
        blocks = [{"type": "ai", "text": s["text"]} for s in segments[:6]]
    return blocks
