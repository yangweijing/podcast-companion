"""运行时配置：从环境变量读取 LLM / ASR / 演示模式等开关。

所有密钥都通过环境变量注入，绝不写死在代码或前端里（前端也拿不到）。
复制本文件为 config.env 并填入你自己的 key 即可；不填则走演示模式。
"""
import os

# 读取 .env（若存在），方便本地开发
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.env")
if os.path.exists(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
    except Exception:
        # dotenv 未安装时退化：手动解析简单的 KEY=VALUE
        with open(_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---------- 演示模式 ----------
# 开启后无需任何 API key 即可看到完整交互（解析/转写/分析/对话均用规则生成）。
DEMO_MODE = _bool("DEMO_MODE", False)

# ---------- 大模型 LLM（OpenAI 兼容接口，DeepSeek/通义/OpenAI 通用）----------
# 便捷预设：填 LLM_PROVIDER 即可自动选用对应服务商的 base_url 与默认模型，
# 无需手动写 base_url。可选：deepseek / qwen(通义千问) / moonshot(Kimi) / doubao(火山方舟) / openai
# 若同时显式设置了 LLM_BASE_URL / LLM_MODEL，则以显式值为准。
LLM_API_KEY = _get("LLM_API_KEY")
LLM_PROVIDER = _get("LLM_PROVIDER", "").lower()
_LLM_PRESETS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    # 火山方舟（豆包）：OpenAI 兼容域名；注意 model 建议填「推理接入点 ID」（ep- 开头），
    # 在方舟控制台「在线推理」创建接入点后获得。直接填模型名若报 404，请改用 ep- ID。
    "doubao":   ("https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k"),
    "openai":   ("https://api.openai.com/v1", "gpt-4o-mini"),
}
_PRESET_BASE, _PRESET_MODEL = _LLM_PRESETS.get(LLM_PROVIDER, (None, None))
LLM_BASE_URL = _get("LLM_BASE_URL") or _PRESET_BASE or "https://api.openai.com/v1"
LLM_MODEL = _get("LLM_MODEL") or _PRESET_MODEL or "gpt-4o-mini"
if LLM_PROVIDER == "doubao" and not LLM_MODEL.startswith("ep-"):
    print("[配置提示] 火山方舟(doubao) 建议把 LLM_MODEL 设为「推理接入点 ID」(ep- 开头)；"
          "当前填的是模型名，若调用报 404 / Model not found，请到方舟控制台创建接入点后改填 ep- ID。")
# 单次请求最长等待（秒）
LLM_TIMEOUT = int(_get("LLM_TIMEOUT", "120"))

# ---------- 语音转写 ASR ----------
# 可选：volc_asr（火山引擎豆包语音，同步返回，需公网音频链接）
#       whisper_api（调 OpenAI Whisper API）
#       local_whisper（本机跑 Whisper，免费但慢）
#       demo（规则占位）
ASR_PROVIDER = _get("ASR_PROVIDER", "whisper_api")
# 火山「豆包语音」的 API Key，从语音控制台获取（与方舟大模型的 key 不同，两处分别创建）：
# https://console.volcengine.com/speech/new/setting/apikeys
VOLC_ASR_API_KEY = _get("VOLC_ASR_API_KEY")
VOLC_ASR_RESOURCE_ID = _get("VOLC_ASR_RESOURCE_ID", "volc.bigasr.auc_turbo")
WHISPER_API_KEY = _get("WHISPER_API_KEY", LLM_API_KEY)  # 默认复用 LLM key（若同一家）
WHISPER_API_BASE = _get("WHISPER_API_BASE", "https://api.openai.com/v1")
WHISPER_MODEL = _get("WHISPER_MODEL", "whisper-1")
LOCAL_WHISPER_MODEL = _get("LOCAL_WHISPER_MODEL", "base")  # local_whisper 用
# 转写前音频最大时长（秒），超出则自动分段，避免超 API 限制/超时
ASR_MAX_SECONDS = int(_get("ASR_MAX_SECONDS", "1200"))

# ---------- 存储 ----------
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DB_PATH = _get("DATABASE_PATH", os.path.join(BASE_DIR, "data", "podcasts.db"))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = _get("UPLOAD_DIR", os.path.join(DATA_DIR, "uploads"))

# ---------- 网络 ----------
PORT = int(_get("PORT", "8000"))
# 抓取小宇宙时使用的 UA，避免被直接拦截
XY_UA = _get(
    "XY_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

# ---------- 本地音频上传 ----------
# 单次上传上限（MB）；本地上传绕过小宇宙解析，文件存于 UPLOAD_DIR
MAX_UPLOAD_MB = int(_get("MAX_UPLOAD_MB", "200"))
ALLOWED_UPLOAD_EXT = [".mp3", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".mpga", ".aac", ".oga"]


def has_llm() -> bool:
    return bool(LLM_API_KEY) and not DEMO_MODE


def has_asr() -> bool:
    if DEMO_MODE or ASR_PROVIDER == "demo":
        return False  # 演示/占位模式走本地规则生成，不依赖真实 ASR
    if ASR_PROVIDER == "whisper_api":
        return bool(WHISPER_API_KEY)  # 需填 key，否则视为未配置
    if ASR_PROVIDER == "local_whisper":
        return True  # 本机运行，无需 key（缺 whisper 包时运行期会友好报错）
    if ASR_PROVIDER == "volc_asr":
        return bool(VOLC_ASR_API_KEY)  # 火山语音 key（独立于方舟大模型 key）
    return False
