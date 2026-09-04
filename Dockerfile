# 播客伴读 · 云端运行镜像
#
# 用 Docker 而不是平台自带的 Python 运行时，是为了带上两个非 Python 依赖：
#   - ffmpeg ：长音频（>25MB）转写前做压缩/分段，没有它短音频也能转，长音频会被 API 拒收
#   - yt-dlp ：喜马拉雅「付费/会员」单集的解析兜底（免费单集不需要它）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir yt-dlp

COPY app/ ./app/
COPY static/ ./static/
COPY run.py .

# 数据统一放 /data：部署平台只要把持久卷挂到 /data，重新部署就不会丢数据。
# 不挂卷时 /data 仍是普通目录，能用，只是重新部署会清空（此时靠「导出备份」自救）。
ENV DATABASE_PATH=/data/podcasts.db \
    UPLOAD_DIR=/data/uploads

RUN mkdir -p /data/uploads

# 非 root 运行（容器安全基线）
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# 端口由平台注入 PORT 环境变量；run.py 会读取，缺省 8000
CMD ["python", "run.py"]

HEALTHCHECK --interval=30s --timeout=8s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('PORT','8000'),timeout=6)"
