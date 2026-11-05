# open-super-agent 服务镜像：后端、构建好的前端，以及应用（MCP）要用的 Node（npx）和 uv（uvx）。
# 一般不用直接构建，用 docker compose up -d，见 docs/docker.md。
# 国内网络可以加 --build-arg NPM_REGISTRY=https://registry.npmmirror.com --build-arg PYPI_INDEX=https://mirrors.ustc.edu.cn/pypi/simple
# 这两个源也会写进镜像，运行时 npx / uvx 下载应用时同样走它们。

FROM node:24-bookworm-slim AS web
ARG NPM_REGISTRY=
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi && npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build    # 产物在 /src/osa/static/dist

FROM node:24-bookworm-slim AS node
FROM docker:29-cli AS docker-cli

FROM python:3.13-slim-bookworm
ARG NPM_REGISTRY=
ARG PYPI_INDEX=
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    UV_CACHE_DIR=/cache/uv \
    UV_PYTHON_INSTALL_DIR=/cache/python \
    UV_LINK_MODE=copy \
    npm_config_cache=/cache/npm \
    npm_config_update_notifier=false

# Node 和 npx：直接从官方 Node 镜像拷，两边都是 Debian bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
 && node --version && npx --version
# Docker 命令行：本地电脑由服务通过宿主机的 Docker 创建和操作（compose 里挂了 docker.sock）
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# uv：装 Python 依赖，也给 uvx 应用用
RUN pip install --no-cache-dir --root-user-action=ignore ${PYPI_INDEX:+--index-url "$PYPI_INDEX"} uv==0.13.6 \
 && if [ -n "$PYPI_INDEX" ]; then mkdir -p /etc/uv && printf '[[index]]\nurl = "%s"\ndefault = true\n' "$PYPI_INDEX" > /etc/uv/uv.toml; fi

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements.txt -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY config.yaml LICENSE ./
COPY osa/ osa/
COPY --from=web /src/osa/static/dist osa/static/dist

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/manifest.json', timeout=4)"
# 反向代理转发来的真实 IP 和 https：信任哪些来源由环境变量 FORWARDED_ALLOW_IPS 决定（compose 里设好了）
CMD ["python", "-m", "uvicorn", "osa.server:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
