#!/bin/sh
# 从 /opt/osa/app/config.yaml 生成演示服务器的配置：不开电脑、不导入触发器、每天最多 50 万 token
set -eu
cd /opt/osa/app
.venv/bin/python - <<'PY'
import yaml
c = yaml.safe_load(open("config.yaml"))
c["computer"]["enabled"] = False
c["triggers"] = []
c["limits"]["daily_token_budget"] = 500000
yaml.safe_dump(c, open("/opt/osa/demo/config.yaml", "w"), allow_unicode=True, sort_keys=False)
PY
