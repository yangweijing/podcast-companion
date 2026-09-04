#!/bin/bash
# 播客伴读 · 一键启动器（双击本文件即可）
# 会自动选 Python、查依赖、查端口占用、起服务并打开浏览器。
# 保持本窗口开启 = 服务保持运行；关闭窗口 = 停止服务。
#
# 本文件可放在任意位置（项目目录 / 桌面 均可），会自动定位到项目。

# ---------- 定位项目目录 ----------
PROJECT="/Users/mac/Documents/OTHER/podcast/podcast-companion"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 优先：脚本本身就在项目里（旁边有 run.py）→ 用脚本所在目录
# 否则（例如放在桌面）→ 用上面写死的绝对路径
if [ -f "$SCRIPT_DIR/run.py" ]; then
  cd "$SCRIPT_DIR" || { echo "无法进入项目目录"; exit 1; }
elif [ -f "$PROJECT/run.py" ]; then
  cd "$PROJECT" || { echo "无法进入项目目录"; exit 1; }
else
  echo "❌ 找不到项目目录。"
  echo "   请把本文件放回项目，或改脚本里的 PROJECT 路径。"
  read -p "按回车退出..."; exit 1
fi

clear
echo "=========================================="
echo "  播客伴读 · 启动器"
echo "=========================================="
echo "目录: $(pwd)"
echo

# ---------- 1. 选择 Python ----------
PY=""
for cand in \
  "./.venv/bin/python" \
  "$(command -v python3)" \
  "/usr/local/bin/python3" \
  "/opt/homebrew/bin/python3"
do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "❌ 没找到可用的 Python 3，请先安装 Python 3.10+"
  read -p "按回车退出..."; exit 1
fi
echo "使用 Python: $PY"
"$PY" --version

# ---------- 2. 依赖检查 ----------
MISSING=$("$PY" -c "
import importlib.util
need = ['fastapi','uvicorn','multipart','requests']
print(' '.join(m for m in need if importlib.util.find_spec(m) is None))
" 2>/dev/null)

if [ -n "$MISSING" ]; then
  echo "⚠️  缺少依赖: $MISSING → 正在安装..."
  "$PY" -m pip install -q fastapi uvicorn python-multipart requests
  MISSING=$("$PY" -c "
import importlib.util
need = ['fastapi','uvicorn','multipart','requests']
print(' '.join(m for m in need if importlib.util.find_spec(m) is None))
" 2>/dev/null)
  if [ -n "$MISSING" ]; then
    echo "❌ 依赖安装失败，仍缺: $MISSING"
    echo "   请手动执行: $PY -m pip install fastapi uvicorn python-multipart requests"
    read -p "按回车退出..."; exit 1
  fi
  echo "✅ 依赖安装完成"
fi

# ---------- 3. yt-dlp（付费集解析用，缺了不影响免费集）----------
if "$PY" -c "import yt_dlp" 2>/dev/null || command -v yt-dlp >/dev/null 2>&1; then
  echo "yt-dlp : ✅ 可用（付费/会员单集也能解析）"
else
  echo "yt-dlp : ⚠️  未安装 → 免费单集正常解析，付费单集解析不了"
  echo "         需要时执行: \"$PY\" -m pip install yt-dlp"
fi
echo

# ---------- 4. 端口检查 ----------
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  8000 端口已被占用："
  lsof -nP -iTCP:8000 -sTCP:LISTEN | tail -n +2
  echo
  echo "请关掉占用它的程序（通常是另一个服务窗口）后重新双击本文件。"
  echo "或执行: kill $(lsof -nP -iTCP:8000 -sTCP:LISTEN -t 2>/dev/null | head -1)"
  read -p "按回车退出..."; exit 1
fi

# ---------- 5. 启动服务 ----------
echo "正在启动服务..."
"$PY" run.py &
SRV_PID=$!

# 等待就绪（最多 30 秒，用同一 python 探测，避免 curl 差异）
READY=0
for i in $(seq 1 30); do
  if "$PY" -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/podcasts', timeout=2)
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    READY=1; break
  fi
  sleep 1
done

echo
if [ "$READY" = "1" ]; then
  echo "✅ 服务已就绪（PID $SRV_PID）"
  open http://localhost:8000
  echo "   浏览器已打开 http://localhost:8000"
else
  echo "❌ 服务启动超时。请截图本窗口的报错信息反馈。"
  echo "   若上方有 Python 报错，那就是启动失败的原因。"
fi

echo
echo "------------------------------------------"
echo " 保持本窗口开启 = 服务保持运行"
echo " 关闭本窗口 / Ctrl+C = 停止服务"
echo "------------------------------------------"

wait $SRV_PID
