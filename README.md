# 播客伴读（本地复刻版）

把小宇宙播客节目「读」成可扫读笔记的工具——自动转写逐字稿、AI 生成导读（摘要/脉络/金句）、可对话的「伴读」、以及可编辑分享的连续笔记。

本仓库是 `http://39.106.14.19:5001/` 这个网站的**功能复刻**：前端界面基本 1:1 还原，后端用 Python 重写，并把「听音频→写文字」的 AI 能力做成可插拔接口，方便你接自己的服务。

> ⚠️ 这个网站的「聪明」不在网页本身，而在后台的 **语音转写（ASR）** 和 **大模型（LLM）**。本仓库已把这两块抽象成接口，你填上自己的 API key 就能真正转写/对话；不填也能用**演示模式**看完整交互（用规则生成的内容占位）。

---

## 一、快速开始（演示模式，零配置）

适合先看看界面和交互长什么样，什么都不用准备。

```bash
# 1. 安装依赖（需要 Python 3.10+）
pip install -r requirements.txt

# 2. 启动（演示模式默认开启）
python run.py

# 3. 浏览器打开
#    http://localhost:8000
```

打开后节目库里已经有一期**示例节目**（带完整逐字稿和导读），点进去就能体验：逐字稿标注、导读跳转播放、伴读对话、生成笔记。
所有 AI 结果都是本地规则生成的占位内容，不会联网、不花钱。

---

## 二、真实模式（接上你自己的 AI）

演示看够了，想让它**真的转写、真的对话**，按下面做：

1. 复制配置模板并填写：
   ```bash
   cp config.env.example config.env
   ```
2. 在 `config.env` 里把 `DEMO_MODE` 改为 `false`，并填入：
   - **`LLM_API_KEY`**：大模型 key（DeepSeek / 通义 / OpenAI 都行，接口兼容）
   - **`ASR_PROVIDER` + `WHISPER_API_KEY`**：语音转写（默认用 OpenAI Whisper API）
3. 重新启动：`python run.py`，打开 `http://localhost:8000`。

### 各服务怎么填（示例）

**大模型最省事的方式**：只填服务商 + key，`base_url` 和 `model` 自动配好：
```bash
LLM_PROVIDER=deepseek      # 可选 deepseek / qwen(通义千问) / moonshot(Kimi) / doubao(火山方舟) / openai
LLM_API_KEY=sk-你的key
```
（这等价于手动写 `LLM_BASE_URL` / `LLM_MODEL`；若手动填了则以手动值为准。）

| 用途 | 变量 | 示例值 |
|------|------|--------|
| 大模型（便捷） | `LLM_PROVIDER` / `LLM_API_KEY` | `deepseek` / `sk-...` |
| 大模型（手动） | `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | DeepSeek：`https://api.deepseek.com/v1` / `deepseek-chat` / `sk-...` |
| 大模型（火山方舟） | `LLM_PROVIDER=doubao` / `LLM_API_KEY` / `LLM_MODEL` | `doubao` / `ek-...` / `ep-2024xxxx-xxxxx`（**推理接入点 ID**） |
| 语音转写 | `ASR_PROVIDER` / `WHISPER_API_KEY` / `WHISPER_MODEL` | `whisper_api` / `sk-...` / `whisper-1` |
| 语音转写（火山） | `ASR_PROVIDER=volc_asr` / `VOLC_ASR_API_KEY` | `volc_asr` / 火山语音 key（**与方舟 key 不同**） |
| 本机转写（免费，吃算力） | `ASR_PROVIDER=local_whisper` | 需另装 `openai-whisper`，建议有 GPU |

### 方案：火山引擎（豆包）一家搞定 LLM + ASR

如果你已有豆包/字节生态账号，可以只注册火山引擎一家，同时拿到大模型和语音转写。完整配置：

```bash
DEMO_MODE=false

# 1) 大模型：火山方舟（ARK）
LLM_PROVIDER=doubao
LLM_API_KEY=ek-你的方舟key          # 方舟控制台「API 密钥管理」创建
LLM_MODEL=ep-2024xxxxxx-xxxxx      # 推理接入点 ID，见下方获取步骤

# 2) 语音转写：豆包语音
ASR_PROVIDER=volc_asr
VOLC_ASR_API_KEY=你的火山语音key     # 语音控制台创建，与上面的方舟 key 不是同一个
```

**大模型 key 获取步骤**（约 10 分钟）：
1. 注册 [火山引擎](https://www.volcengine.com/) → 右上角账号管理完成**实名认证**（个人即可，不实名无法用 API）。
2. 进入 [方舟控制台](https://console.volcengine.com/ark) → **API 密钥管理** → 创建 API Key（`ek-` 开头，**只显示一次，立即保存**）。
3. **模型管理中心** → 开通豆包系列模型（如 Doubao-lite-128k / Doubao-pro-256k）。
4. **在线推理** → 创建推理接入点（地区选 `cn-beijing`）→ 状态变「运行中」后复制 `ep-` 开头的 Endpoint ID，填进 `LLM_MODEL`。

> ⚠️ 方舟的 `model` 参数**推荐填推理接入点 ID（`ep-` 开头）**，而不是模型名。直接填 `doubao-pro-32k` 这类模型名若报 `404 / Model not found`，换成 `ep-` ID 即可。启动时若检测到你填的不是 `ep-` ID，控制台会打印一条提示。

**语音转写 key 获取**：[语音控制台 → API Key 管理](https://console.volcengine.com/speech/new/setting/apikeys)，需先开通语音识别服务。新用户通常有免费额度。

> ⚠️ **火山 ASR 的硬性限制**：它要求音频是**公网可访问**的链接（火山服务器主动去拉取）。
> - 小宇宙链接导入的节目（音频是公网 CDN 地址）→ ✅ 用 `volc_asr`，快且准，同步返回带毫秒时间戳和说话人标记。
> - 通过「上传本地音频」添加的节目（`localhost` 私有地址）→ ❌ 火山服务器读不到，会明确报错。这种情况请改用 `ASR_PROVIDER=local_whisper`（本机免费转写，但慢、吃算力）。
> - 接口上限：单文件 100MB / 2 小时以内，支持 wav、mp3、ogg 等。

> 小宇宙链接解析、转写、分析、对话、笔记**全部在本地运行**，只有调用你填的 AI 服务时才会对外请求。

---

## 三、目录结构

```
podcast-companion/
├── run.py                 # 启动入口
├── requirements.txt
├── config.env.example      # 配置模板（密钥都在这里，不进代码）
├── app/
│   ├── main.py             # FastAPI：全部 /api 路由 + 托管前端
│   ├── config.py           # 读环境变量（LLM/ASR/演示模式/端口）
│   ├── db.py               # SQLite 持久层（节目/逐字稿/分析/对话/笔记）
│   └── providers/
│       ├── parser.py       # 多平台单集链接解析：小宇宙 / 网易云播客 / 喜马拉雅(经yt-dlp)
│       ├── asr.py          # 语音转写（火山ASR / Whisper API / 本地 / 演示）
│       ├── llm.py          # 大模型（分析/对话/笔记/文章，真/演示）
│       └── prompts.py      # 提示词模板
├── static/
│   └── index.html          # 前端（还原原站，已去埋点）
└── data/                   # 运行时生成的SQLite库（自动创建）
```

---

## 四、功能对照

| 模块 | 功能 | 依赖 |
|------|------|------|
| 节目库 | 粘贴链接解析节目（小宇宙 / 网易云播客 / 喜马拉雅）、或上传本地音频 | 平台解析（联网）/ 本地上传（离线） |
| 详情 | 音频播放器 + 状态 | 音频直链 |
| 导读 | 摘要 / 主线脉络 / 主要问题 / 金句（点时间码跳转播放） | LLM |
| 逐字稿 | 带时间码稿、标「重点/原文」、主观笔记、复制/导出 txt | ASR（先转写） |
| 伴读 | 基于节目内容的对话、定位片段、整理成文章 | LLM |
| 笔记 | AI 生成结构化笔记、可编辑、分享、导出 doc | LLM |

---

## 五、重要注意事项（务必读）

1. **一定要用 `http://localhost:8000` 打开，不要双击 `index.html`。**
   双击是 `file://` 协议，前端用相对路径 `/api/...` 请求会失败。必须经 uvicorn 起的地址访问。

2. **小宇宙解析是最脆弱的一环。** 它依赖小宇宙公开接口，可能随版本/反爬/地区网络变化而失效。若解析失败：
   - 检查网络与 UA；
   - 或暂时手动把音频直链等塞进数据库（见 `app/db.py` 的 `create_episode`）；
   - 演示模式不受影响（用占位节目）。

2.1 **喜马拉雅：默认开箱可用，yt-dlp 仅用于付费集兜底。** 实测（2026-09）喜马拉雅存在免登录的移动端公开接口 `m.ximalaya.com/tracks/{soundId}.json`，可直接取到标题/专辑名/时长/封面/音频直链，因此**免费单集无需任何额外依赖**（约 0.2s 完成解析）。解析链路为：

   | 优先级 | 链路 | 说明 |
   |---|---|---|
   | ① | `m.ximalaya.com/tracks/{id}.json` | 主路径。免登录、免签名、快；能区分「已下架」与「付费」 |
   | ② | `yt-dlp` 内置 ximalaya 提取器 | 付费(VIP)集走 mpay 签名解密拿直链；**仅①拿不到音频时才用** |
   | ③ | 网页 og 抓取 | 最后兜底 |

   `yt-dlp` 是**可选增强**：不装也能解析免费单集；装了才能解析付费集。需要时 `pip install yt-dlp`（解析器会探测 `PATH`、`YOUTUBE_DL_PATH`/`YTDLP` 环境变量及几个常见绝对路径）。网易云播客、小宇宙完全不需要 yt-dlp。

   支持的链接形态：`www.ximalaya.com/sound/数字`、`m.ximalaya.com/主播id/sound/数字`、`xima.tv` 短链（自动跟随重定向）。专辑主页（`/album/数字`）会被识别并提示改用单集链接。

   ⚠️ 付费集经 yt-dlp 取到的是**带时效签名的直链**（约 1 天内有效，URL 含 `sign`/`token`/`timestamp`）。添加节目时会立即转写，所以不影响生成逐字稿；但隔天后再播放可能失效，此时前端会显示黄色提示条提醒。

3. **音频防盗链。** 小宇宙 CDN 可能对播放器来源做校验，`localhost` 直接播可能播不出。这是使用时的网络限制，与代码无关；可对音频做本地代理或下载后播放。

4. **长音频（>25MB）需 ffmpeg。** OpenAI Whisper API 限制单文件 25MB。代码会自动尝试用 `ffmpeg` 压缩/分段；若环境无 `ffmpeg`，短音频（≤25MB）仍可正常转写。安装：`apt install ffmpeg`（Linux）/ `brew install ffmpeg`（Mac）。

5. **合规。** 仅建议个人学习/自用。抓取与转写他人播客涉及版权与平台条款，对外分发或商用请自行评估风险。

6. **本地上传音频可绕开小宇宙解析与防盗链。** 前端「添加节目」卡片里的「上传本地音频」按钮，会把文件存入 `data/uploads/`，`audio_url` 记为 `/uploads/xxx` 由本机直接播放与转写，适合小宇宙解析失败、或其音频在 `localhost` 播不出来的场景，也可处理你**自己的**音频。
   - 单文件上限默认 `200MB`（环境变量 `MAX_UPLOAD_MB` 可调），仅接受常见音频扩展名（mp3/m4a/wav/ogg/webm/flac 等）。
   - 上传的节目 `duration_ms` 记为 0，播放器不显示总时长，但**不影响播放与转写**。
   - 演示模式下上传同样走通，只是转写是规则占位稿；接真实 key 后才会真正转你上传的音频。
   - ⚠️ **搭配 `ASR_PROVIDER=volc_asr` 时，上传的本地音频无法转写**（火山需要公网链接，读不到你本机的 `/uploads/`）。想转本地音频请用 `ASR_PROVIDER=local_whisper`，或改用小宇宙链接导入。

---

## 六、常用 API（供二次开发）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/podcasts?q=&limit=` | 节目列表 |
| POST | `/api/podcasts` | 添加节目 `{source_url}` |
| POST | `/api/podcasts/upload` | 上传本地音频（`file` + 可选 `title`/`podcast`），绕开解析 |
| GET | `/api/podcasts/{id}` | 节目详情 + 分析结果 |
| POST | `/api/podcasts/{id}/transcribe?background=1` | 触发转写（后台） |
| POST | `/api/podcasts/{id}/analyze?background=1` | 触发分析（后台） |
| GET | `/api/podcasts/{id}/transcript?format=text` | 逐字稿 JSON / 纯文本 |
| PATCH | `/api/podcasts/{id}/transcript/annotations/{seg}` | 保存段落标注 |
| POST | `/api/podcasts/{id}/chat` | 伴读对话 `{message, session_id}` |
| POST | `/api/podcasts/{id}/article` | 片段整理成文章 `{segment_id}` |
| POST | `/api/podcasts/{id}/notes/generate` | 生成笔记 |
| GET/PUT/DELETE | `/api/notes/{id}` | 笔记读取/保存/删除 |
| GET | `/api/notes/{id}/export` | 导出 .doc |
