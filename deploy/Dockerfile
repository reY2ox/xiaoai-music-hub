# xiaoai-music-hub 容器镜像
#
# 构建（在仓库根目录执行）：
#   docker build -f deploy/Dockerfile -t xiaoai-music-hub:latest .
# 或直接用 deploy/docker-compose.yml。

FROM python:3.12-slim

# 运行期约定：数据全部落在 /data（db / cache / secrets / music）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    XIAOAI_HOST=0.0.0.0 \
    PORT=8010

# ffmpeg：本地曲库转码（flac/m4a/wav → mp3）与下载后写 ID3 标签
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再装项目本身（console script: xiaoai-music-hub）
COPY pyproject.toml README.md LICENSE ./
COPY xiaoai_hub ./xiaoai_hub
COPY app.py ./
RUN pip install --no-cache-dir --no-deps .

# 非 root 用户；/data 是唯一需要持久化的目录
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin xiaoai \
    && mkdir -p /data \
    && chown -R xiaoai:xiaoai /data

USER xiaoai

VOLUME ["/data"]
EXPOSE 8010

# 探活：/health 免鉴权
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health', timeout=3)" || exit 1

CMD ["python", "app.py"]
