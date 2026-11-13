# lsp（osa.cclsp.top）上的 Docker 部署

正式服务、演示服务器（demo.cclsp.top）、推送中转（push.cclsp.top）和 Bot 的电脑（Docker 沙盒）都是容器，
由 `/opt/osa/docker/compose.yaml`（就是这里的 `compose.yaml`）管。服务器上只有独立的 `docker-compose`（v2），没有 `docker compose` 插件。

服务器只有 2 GB 内存，Bot 平时用云电脑（Instabox，`/opt/osa/env` 里的 `INSTABOX_API_KEY`），Docker 电脑（`computer` 容器）默认不启动。
要用 Docker 沙盒：`docker-compose up -d computer`，再到网页的 设置 → 电脑 里换过去；不用了 `docker-compose stop computer`。

| 目录 | 内容 |
|---|---|
| `/opt/osa/docker/` | `compose.yaml`、`make-config.sh`、生成的 `config.server.yaml`、`src/`（要部署的代码，构建用） |
| `/opt/osa/app/data`、`/opt/osa/env` | 正式服务的数据和环境变量（原地挂进容器，路径不变） |
| `/root/.config/mcp-email-server` | Gmail 应用自己的数据 |
| `/opt/osa/demo/`、`/opt/osa/relay/` | 演示服务器、推送中转的数据和环境变量 |

## 部署新版本

```sh
# 在 Mac 上：把要部署的代码打包传过去
git archive HEAD | ssh lsp 'rm -rf /opt/osa/docker/src.new && mkdir /opt/osa/docker/src.new && tar xf - -C /opt/osa/docker/src.new'
# 在 lsp 上：
cd /opt/osa/docker && rm -rf src.prev && mv src src.prev && mv src.new src
docker-compose build                      # 旧容器照常跑着（computer 默认不启动，也不构建；要用它时 docker-compose build computer）
sh make-config.sh
# 先看有没有正在跑的任务（/api/bootstrap 的 tasks），再换：
docker-compose up -d
```

`src.prev` 是上一版的代码，回退时换回来再 `docker-compose build && docker-compose up -d`。数据库在换版前照旧用 sqlite 的 backup 存一份快照。

## 从 systemd 换成容器（2026-11 一次性）

以前三个服务是 systemd 单元（`osa`、`osa-demo`、`osa-relay`），代码在 `/opt/osa/app`。换的时候：构建好镜像、生成配置 → 存数据库快照 →
`systemctl stop` 三个单元 → `docker-compose up -d` → 检查 → `systemctl disable` 三个单元（单元文件和 `/opt/osa/app` 先留着，回退用：
`docker-compose down && systemctl enable --now osa osa-demo osa-relay`）。
