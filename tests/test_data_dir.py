"""桌面版的代码目录是只读的：OSA_DATA_DIR 指到哪，数据库、密钥、应用清单、工作目录就都放在哪。"""
import json
import os
import subprocess
import sys

from conftest import TMP
from osa.config import ROOT

PROBE = """
import json
from osa import auth, backup, catalog, mcp_hub, memory, prices
from osa.computer.local import LocalComputer
auth._signing_key()
print(json.dumps({"db": str(memory.db_path), "backup_db": str(backup.DB), "apps": str(backup.APPS),
                  "catalog_apps": str(catalog.LOCAL), "workspace": str(mcp_hub.WORKSPACE),
                  "catalog_workspace": str(catalog.WORKSPACE), "prices": str(prices.FILE),
                  "computer": LocalComputer(1).data}))
"""


def _paths(**env) -> dict:
    full = {k: v for k, v in os.environ.items() if k not in ("OSA_DATA_DIR", "OSA_CONFIG")}
    full.update(env)
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=full, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_everything_goes_into_the_data_dir():
    data = TMP / "desktop-data"
    p = _paths(OSA_DATA_DIR=str(data), OSA_CONFIG=str(ROOT / "config.yaml"))   # 仓库里的配置：memory.db 是相对路径 data/osa.db
    assert p["db"] == p["backup_db"] == str(data / "osa.db")
    assert p["apps"] == p["catalog_apps"] == str(data / "apps")
    assert p["workspace"] == p["catalog_workspace"] == str(data / "workspace")
    assert p["prices"] == str(data / "model-prices.json")
    assert p["computer"] == str(data / "computers" / "shared")
    assert (data / "osa.db").exists() and (data / "session.key").exists()


def test_absolute_db_path_still_wins():
    data = TMP / "desktop-data-2"
    p = _paths(OSA_DATA_DIR=str(data), OSA_CONFIG=str(TMP / "config.yaml"))     # 测试用的配置：memory.db 是绝对路径
    assert p["db"] == str(TMP / "osa.db")
    assert p["apps"] == str(data / "apps")


def test_default_is_unchanged():
    p = _paths(OSA_CONFIG=str(TMP / "config.yaml"))
    assert p["db"] == str(TMP / "osa.db")
    assert p["apps"] == str(ROOT / "data" / "apps")
    assert p["workspace"] == str(ROOT / "data" / "workspace")
    assert p["computer"] == str(ROOT / "data" / "computers" / "shared")
