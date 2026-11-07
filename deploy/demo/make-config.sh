#!/bin/sh
# 从 /opt/osa/app/config.yaml 生成演示服务器的配置：演示模式（见 SECURITY.md）、不开电脑、不导入触发器、每天最多 50 万 token
set -eu
cd /opt/osa/app
.venv/bin/python - <<'PY'
import yaml
c = yaml.safe_load(open("config.yaml"))
c["computer"]["enabled"] = False
c["triggers"] = []
c["limits"]["daily_token_budget"] = 500000
c["tools"]["fetch_private"] = False
c["security"] = {"local_exec": False, "demo": True}
yaml.safe_dump(c, open("/opt/osa/demo/config.yaml", "w"), allow_unicode=True, sort_keys=False)
PY
chmod 644 /opt/osa/demo/config.yaml
