# 用 Docker Compose 部署

服务、前端、应用（MCP）要用的 Node 和 uv 都打包在镜像里，不用自己装 Python 和 Node，也不用构建前端。

## 快速开始

需要 Docker 和 Compose v2.24 以上（Docker Desktop、OrbStack、Linux 上的 Docker Engine 都可以）。

```bash
git clone https://github.com/kadaliao/open-super-agent.git && cd open-super-agent
cp .env.example .env     # 可以先填上 DEEPSEEK_API_KEY，也可以打开后在 设置 → 模型 里填
docker compose up -d     # 第一次要构建镜像，大约 5–10 分钟
```

打开 http://127.0.0.1:8765 。

`up` 会构建两个镜像：服务（`open-super-agent:local`）和本地电脑（`osa-computer:1`）。电脑不会马上启动，Bot 第一次用电脑时才创建容器 `osa-computer`，闲置 15 分钟后自动暂停。

| 做什么 | 命令 |
|---|---|
| 看日志 | `docker compose logs -f osa` |
| 改了 `config.yaml` | `docker compose restart osa` |
| 改了 `.env` | `docker compose up -d`（`restart` 不会重新读 `.env`） |
| 更新到新版本 | `git pull && docker compose up -d --build` |
| 停止 | `docker compose down`；电脑容器不归 compose 管，要停的话再 `docker stop osa-computer` |

## 数据放在哪

| 内容 | 位置 |
|---|---|
| 数据库、保险库主密钥、登录密钥、本机的应用清单、文件夹应用的目录 | 项目目录下的 `data/`，和不用 Docker 运行时是同一份 |
| 电脑里的文件和浏览器登录状态（电脑里的 `/data`） | 命名卷 `osa-computer-data` |
| npx / uvx 下载的应用 | 命名卷 `osa_cache`，删了只是重新下载 |

备份：设置 → **备份与迁移** 导出一个加密文件，或者用命令行（口令也可以放在 `.env` 的 `OSA_BACKUP_PASSPHRASE` 里）：

```bash
docker compose exec osa python -m osa.backup export -o /app/data    # 备份文件出现在宿主机的 data/ 里
```

恢复在网页上传文件就行，服务会自动重启（`restart: unless-stopped`）。说明见 [备份与迁移](configuration.md#备份与迁移)。
备份文件不含电脑里的文件；电脑里的文件可以这样打包出来：

```bash
docker run --rm -v osa-computer-data:/data -v "$PWD":/backup osa-computer:1 tar czf /backup/computer-data.tgz -C /data .
```

直接复制 `data/` 也行（先 `docker compose down`）。`data/` 里的 `vault.key` 和 `session.key` 是密钥，要放在安全的地方。

## 部署到公网（自动 HTTPS）

把域名解析到服务器，空出 80 和 443 端口，然后在 `.env` 里写：

```bash
OSA_PASSWORD=一个足够长的随机密码
OSA_DOMAIN=osa.example.com
```

```bash
docker compose --profile https up -d
```

自带的 Caddy 会自动申请证书，访问 https://osa.example.com 。服务本身仍只监听本机的 8765。

服务器上已经有 Caddy 或 nginx 的话，不用这个 profile，把域名反代到 `127.0.0.1:8765` 就行，写法见 [配置 → 部署到公网服务器](configuration.md#部署到公网服务器)。

## 电脑用 Instabox

服务器内存小、不想在上面跑桌面和浏览器时，电脑改用 Instabox 云主机：

1. `.env` 里设 `INSTABOX_API_KEY`（`computer.backend` 保持 `auto` 就会用它，也可以在 `config.yaml` 里写成 `instabox`）。
2. 用 `docker compose up -d osa` 启动，只构建服务的镜像，不构建本地电脑的。
3. 服务用不到 Docker 了，可以删掉 `compose.yaml` 里挂 `/var/run/docker.sock` 的那一行。

## 选项

`.env` 里的这几项只给 compose 用（其他环境变量见 [配置 → 环境变量](configuration.md#环境变量)）：

| 变量 | 默认 | 用途 |
|---|---|---|
| `OSA_PORT` | `8765` | 宿主机上的端口 |
| `OSA_BIND` | `127.0.0.1` | 监听地址。局域网里直接访问改成 `0.0.0.0`，先设 `OSA_PASSWORD` |
| `TZ` | `Asia/Shanghai` | 时区，定时任务按它算 |
| `OSA_DOMAIN` | | `--profile https` 用的域名 |
| `FORWARDED_ALLOW_IPS` | `*` | 信任哪些来源带的真实 IP（`X-Forwarded-For`）。默认只有本机连得进来，所以都信任；`OSA_BIND=0.0.0.0` 直接对外时改成 `127.0.0.1`，否则别人可以伪造 IP 绕过登录限速 |
| `APT_MIRROR`、`NPM_REGISTRY`、`PYPI_INDEX` | | 构建镜像用的软件源，见下面 |

`.env` 里的注释要单独占一行，写在值后面的注释可能被当成值的一部分。

## 国内网络

- Docker Hub 拉不下镜像时，先给 Docker 配镜像加速（`registry-mirrors`）。
- 构建时用国内软件源，在 `.env` 里写上，再 `docker compose build`：

  ```bash
  APT_MIRROR=mirrors.ustc.edu.cn
  NPM_REGISTRY=https://registry.npmmirror.com
  PYPI_INDEX=https://mirrors.ustc.edu.cn/pypi/simple
  ```

  `NPM_REGISTRY` 和 `PYPI_INDEX` 也会写进镜像，运行时 npx / uvx 下载应用同样走它们。
- 要用 Telegram 时设代理。宿主机上的代理写成 `HTTPS_PROXY=http://host.docker.internal:7890`，代理要允许局域网连接（比如 Clash 的“允许局域网”）。

## 常见问题

| 现象 | 原因和处理 |
|---|---|
| 用电脑时报 `Cannot connect to the Docker daemon` | 没挂 `/var/run/docker.sock`，或者 Docker 的 socket 不在这里（rootless Docker 在 `$XDG_RUNTIME_DIR/docker.sock`），改 `compose.yaml` 里那一行 |
| 改了电脑镜像没生效 | 旧容器还在：`docker compose build computer && docker rm -f osa-computer`，下次用电脑时按新镜像创建 |
| 镜像构建成了 amd64，或者电脑启动报平台不匹配 | 环境里设了 `DOCKER_DEFAULT_PLATFORM`，`unset` 之后重新构建 |
| 文件夹应用要访问宿主机上的目录 | 应用跑在服务的容器里，只看得到容器里的路径。在 `compose.yaml` 的 `volumes` 里把目录挂进去（比如 `- ~/Documents:/mnt/docs`），添加应用时填 `/mnt/docs` |
| 连宿主机上的 Ollama | 设置 → 模型 → 添加 → Ollama，地址改成 `http://host.docker.internal:11434/v1` |
| 设置里的单价一直是“没有单价” | 容器连不上 models.dev（国内网络常见）：在 `.env` 里设 `HTTPS_PROXY`，或者把单价写进 `config.yaml` 的 `pricing.models` |

## 安全

- 挂进去的 `docker.sock` 让服务能创建和操作宿主机上的容器，等于有宿主机的 root 权限。服务里跑着第三方的应用（MCP），只添加信得过的；用 Instabox 时把这一行删掉。
- 电脑的 VNC 没有密码，所以只监听电脑容器里的 127.0.0.1，不映射到宿主机端口，由服务经 `docker exec` 转发给网页。
- 其余见 [SECURITY.md](../SECURITY.md)。
