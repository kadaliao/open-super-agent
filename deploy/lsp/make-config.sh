#!/bin/sh
# 生成 lsp 上正式服务和演示服务器的配置，每次部署（换了 src/ 里的代码）后跑一遍。用镜像里的 Python，从 src/config.yaml 改出来：
# - 正式服务 /opt/osa/docker/config.server.yaml：电脑用 Docker 沙盒（compose 里的 computer 容器），面板里也能选云电脑；
#   服务器只有 2 GB 内存，电脑一次只给一个任务用；服务器锁打开（security.local_exec: false），命令只在沙箱（电脑容器）里跑，见 SECURITY.md
# - 演示服务器 /opt/osa/demo/config.yaml：演示模式、不开电脑、不导入触发器、每天最多 50 万 token
set -eu
docker run --rm -i -v /opt/osa/docker:/w -v /opt/osa/demo:/demo open-super-agent:lsp python - <<'PY'
import copy, yaml
base = yaml.safe_load(open("/w/src/config.yaml"))

c = copy.deepcopy(base)
c["computer"]["backend"] = "local"
c["computer"]["choices"] = ["local", "instabox"]
c["computer"]["parallel"] = 1
c.setdefault("security", {})["local_exec"] = False
yaml.safe_dump(c, open("/w/config.server.yaml", "w"), allow_unicode=True, sort_keys=False)

d = copy.deepcopy(base)
d["computer"]["enabled"] = False
d["triggers"] = []
d["limits"]["daily_token_budget"] = 500000
d["tools"]["fetch_private"] = False
d["security"] = {"local_exec": False, "demo": True}
yaml.safe_dump(d, open("/demo/config.yaml", "w"), allow_unicode=True, sort_keys=False)
PY
chmod 644 /opt/osa/docker/config.server.yaml /opt/osa/demo/config.yaml
