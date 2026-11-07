#!/bin/sh
# 从 /opt/osa/app/config.yaml 生成正式服务（osa.cclsp.top）的配置 config.server.yaml，每次部署后跑一遍：
# 电脑用 Instabox；打开服务器锁（security.local_exec: false），网页上不能让服务器执行任意命令，见 SECURITY.md
set -eu
cd /opt/osa/app
.venv/bin/python - <<'PY'
import yaml
c = yaml.safe_load(open("config.yaml"))
c["computer"]["backend"] = "instabox"
c.setdefault("security", {})["local_exec"] = False
yaml.safe_dump(c, open("config.server.yaml", "w"), allow_unicode=True, sort_keys=False)
PY
