"""服务器锁（security.local_exec: false）和演示模式（security.demo: true）：网页上不能让服务器执行任意命令、不能把服务的密钥带走。
策略表（security.exec / plugins / apps）：没写时按 local_exec / demo 推，写了以写的为准，演示服务器上有上限。
还有不分模式的两条：换了模型服务的地址要重新填密钥；应用市场里填的值不能带 env: / vault: 引用。"""
import json
import time

import httpx
import pytest

from osa import catalog, computer, mcp_hub, providers, security, server, settings
from osa.computer import host
from osa.config import CFG
from osa.memory import conn


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setitem(CFG, "security", {"local_exec": False})


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setitem(CFG, "security", {"demo": True})


@pytest.fixture
def cleanup():
    names = []
    yield names
    for n in names:
        mcp_hub.remove(n)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1")


def _fs(path: str) -> dict:
    return catalog.build("filesystem", {"path": path}) | {"enabled": True}


# ---------- 默认：自己电脑上什么都能配 ----------
def test_unlocked_by_default(cleanup):
    assert security.local_exec() and not security.demo()
    cleanup.append("mytool")
    mcp_hub.save("mytool", {"command": "/bin/sh", "args": ["-c", "true"], "env": {"X": "env:HOME"}})
    assert settings.public()["security"] == {"local_exec": True, "demo": False, "exec": "host", "plugins": "any", "apps": "any"}


# ---------- 策略表 ----------
@pytest.mark.parametrize("cfg, want", [
    ({}, ("host", "any", "any")),
    ({"local_exec": True}, ("host", "any", "any")),
    ({"local_exec": False}, ("sandbox", "market", "market")),
    ({"demo": True}, ("sandbox", "off", "market")),
    ({"demo": True, "local_exec": True}, ("sandbox", "off", "market")),      # 演示服务器不看 local_exec
    ({"local_exec": False, "apps": "any"}, ("sandbox", "market", "any")),    # 写了新键就以新键为准
    ({"local_exec": True, "exec": "sandbox", "plugins": "market"}, ("sandbox", "market", "any")),
    ({"exec": False, "plugins": False}, ("off", "off", "any")),              # YAML 里不带引号的 off 读出来是 false
    ({"exec": "HOST", "plugins": " Market "}, ("host", "market", "any")),
    ({"exec": "hots", "plugins": "all", "apps": "yes"}, ("off", "off", "market")),   # 写错的按最严的算
    ({"demo": True, "exec": "host", "plugins": "any", "apps": "any"}, ("sandbox", "market", "market")),   # 演示服务器有上限
    ({"demo": True, "exec": "off"}, ("off", "off", "market")),
])
def test_policy_mapping(monkeypatch, cfg, want):
    monkeypatch.setitem(CFG, "security", cfg)
    assert (security.exec_mode(), security.plugins_mode(), security.apps_mode()) == want
    assert security.local_exec() == (want[0] == "host")
    assert security.public() == {"local_exec": want[0] == "host", "demo": bool(cfg.get("demo")),
                                 "exec": want[0], "plugins": want[1], "apps": want[2]}


def test_policy_reads_yaml_off(monkeypatch):
    import yaml
    monkeypatch.setitem(CFG, "security", yaml.safe_load("exec: off\nplugins: market\napps: any"))
    assert security.policy() == {"exec": "off", "plugins": "market", "apps": "any"}


def test_apps_policy_alone_decides_apps(monkeypatch, cleanup):
    """apps 和 exec 分开管：能在本机跑命令也可以只认应用市场里的应用，反过来也行。"""
    monkeypatch.setitem(CFG, "security", {"local_exec": True, "apps": "market"})
    with pytest.raises(ValueError, match="security.apps"):
        security.check_app("mytool", {"command": "/bin/sh", "args": ["-c", "id"]})
    security.check_app("train", catalog.build("train", {}))
    monkeypatch.setitem(CFG, "security", {"local_exec": False, "apps": "any"})
    assert not security.local_exec()
    security.check_app("mytool", {"command": "/bin/sh", "args": ["-c", "id"]})
    security.check_market("train")


# ---------- 服务器锁 ----------
def test_locked_rejects_custom_commands(locked, cleanup):
    cleanup += ["mytool", "train"]
    with pytest.raises(ValueError, match="应用市场里原样"):
        mcp_hub.save("mytool", {"command": "/bin/sh", "args": ["-c", "id"]})
    mcp_hub.save("train", catalog.build("train", {}) | {"enabled": True})      # 应用市场里原样的照常能加
    for bad in ({"command": "/bin/sh"}, {"args": ["-y", "12306-mcp", "--evil"]}, {"env": {"NODE_OPTIONS": "--require /tmp/x.js"}}):
        with pytest.raises(ValueError, match="应用市场里原样"):
            mcp_hub.save("train", bad)
    mcp_hub.save("train", {"enabled": False, "command": "/bin/sh"})            # 停用不拦，已经不能用的也要能关掉
    with pytest.raises(ValueError, match="应用市场里原样"):
        mcp_hub.save("train", {"enabled": True})


def test_locked_market_env_values_can_change_but_not_keys(locked, cleanup):
    cleanup.append("gmail")
    cfg = catalog.build("gmail", {"email": "a@gmail.com", "password": "pw"}) | {"enabled": True}
    mcp_hub.save("gmail", cfg)
    mcp_hub.save("gmail", {"env": cfg["env"] | {"MCP_EMAIL_SERVER_EMAIL_ADDRESS": "b@gmail.com"}})   # 填的值能改
    with pytest.raises(ValueError):
        mcp_hub.save("gmail", {"env": cfg["env"] | {"MCP_EMAIL_SERVER_IMAP_HOST": "evil.example"}})  # 模板里写死的不能改
    with pytest.raises(ValueError):
        mcp_hub.save("gmail", {"env": cfg["env"] | {"MCP_EMAIL_SERVER_EMAIL_ADDRESS": "{env:OSA_PASSWORD}"}})


def test_locked_folder_app_stays_in_workspace(locked, cleanup, tmp_path):
    cleanup.append("filesystem")
    ws = security.WORKSPACE
    mcp_hub.save("filesystem", _fs(str(ws)))
    mcp_hub.save("filesystem", _fs(str(ws / "reports")))
    for bad in ("/", "/etc", str(ws / ".."), "~/.ssh", "--help"):
        with pytest.raises(ValueError):
            mcp_hub.save("filesystem", _fs(bad))
    ws.mkdir(parents=True, exist_ok=True)
    link = ws / "out"
    link.symlink_to(tmp_path)                                                 # 经链接跑出去也不行
    try:
        with pytest.raises(ValueError, match="工作文件夹"):
            mcp_hub.save("filesystem", _fs(str(link)))
    finally:
        link.unlink()


def test_locked_checks_again_before_connecting(locked):
    """恢复备份、直接改数据库进来的配置，连接之前也挡住。"""
    cfg = {"command": "/bin/sh", "args": ["-c", "id"], "env": {}, "deny": [], "confirm": "auto"}
    conn.execute("insert or replace into apps values(?,?,?,?)", ("injected", json.dumps(cfg), 1, time.time()))
    conn.commit()
    try:
        with pytest.raises(ValueError, match="应用市场里原样"):
            mcp_hub._transport("injected", mcp_hub.get("injected"))
    finally:
        mcp_hub.remove("injected")


def test_locked_only_trusts_builtin_manifests(locked, monkeypatch, tmp_path):
    """data/apps 里本机加的清单（恢复备份也会带进来）不算：启动命令只认随项目发布的。"""
    monkeypatch.setattr(catalog, "LOCAL", tmp_path)
    (tmp_path / "train.yaml").write_text((catalog.BUILTIN / "train.yaml").read_text().replace("12306-mcp", "evil-pkg"))
    assert catalog.items()["train"]["connect"]["args"] == ["-y", "evil-pkg"]
    with pytest.raises(ValueError, match="应用市场里原样"):
        security.check_app("train", catalog.build("train", {}))
    (tmp_path / "mine.yaml").write_text((catalog.BUILTIN / "train.yaml").read_text())
    with pytest.raises(ValueError, match="应用市场里原样"):
        security.check_market("mine")


def test_locked_allows_apps_from_config_yaml(locked, monkeypatch):
    monkeypatch.setitem(CFG, "mcp_servers", {"mine": {"command": "npx", "args": ["-y", "x"], "env_from": ["TOKEN"]}})
    security.check_app("mine", mcp_hub.from_yaml(CFG["mcp_servers"]["mine"]))
    with pytest.raises(ValueError):
        security.check_app("mine", mcp_hub.from_yaml(CFG["mcp_servers"]["mine"]) | {"args": ["-y", "y"]})


def test_locked_remote_apps_cannot_read_server_env(locked, cleanup):
    cleanup.append("remote")
    mcp_hub.save("remote", {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer {vault:REMOTE_TOKEN}"}})
    with pytest.raises(ValueError, match="环境变量"):
        mcp_hub.save("remote", {"headers": {"X-Key": "{env:DEEPSEEK_API_KEY}"}})
    with pytest.raises(ValueError, match="环境变量"):
        mcp_hub.save("remote", {"url": "https://example.com/mcp?k={env:OSA_PASSWORD}"})


def test_locked_computer_cannot_use_this_machine(locked, monkeypatch):
    monkeypatch.setitem(CFG, "computer", {**CFG["computer"], "backend": "host", "host_shell": True})
    assert computer.backend_name() != "host"
    assert not host.shell_on()
    assert next(b for b in computer.available() if b["id"] == "host") == {"id": "host", "ok": False, "locked": True}
    with pytest.raises(ValueError, match="security.exec"):
        settings.save({"computer_backend": "host"})
    with pytest.raises(ValueError, match="security.exec"):
        settings.save({"computer_host_shell": True})
    monkeypatch.setenv("OSA_DESKTOP", "1")
    monkeypatch.setitem(CFG, "computer", {**CFG["computer"], "backend": "auto"})
    assert computer.backend_name() != "host"


async def test_disabled_computer_never_starts(monkeypatch):
    """config.yaml 里 computer.enabled: false（演示服务器）：哪个后端都不开机，比如不会在服务器上构建 Docker 镜像。"""
    from osa.computer.base import ComputerError
    from osa.computer.local import LocalComputer
    monkeypatch.setitem(CFG, "computer", {**CFG["computer"], "enabled": False})
    for c in (LocalComputer(0), host.HostComputer(0)):
        with pytest.raises(ComputerError, match="computer.enabled"):
            await c.start()


# ---------- 演示模式 ----------
def test_demo_blocks_all_local_apps_and_custom_apps(demo, cleanup):
    assert not security.local_exec()
    with pytest.raises(ValueError, match="远程连接"):
        security.check_market("train")
    with pytest.raises(ValueError, match="远程连接"):
        mcp_hub.save("train", catalog.build("train", {}) | {"enabled": True})
    cleanup.append("deepwiki")
    mcp_hub.save("deepwiki", catalog.build("deepwiki", {}) | {"enabled": True})   # 应用市场里的远程应用可以
    with pytest.raises(ValueError, match="应用市场里的应用"):
        mcp_hub.save("deepwiki", {"url": "http://127.0.0.1:8765/api"})
    with pytest.raises(ValueError, match="应用市场里的应用"):
        mcp_hub.save("remote", {"type": "http", "url": "https://example.com/mcp"})


async def test_demo_endpoints(demo):
    async with _client() as c:
        r = await c.put("/api/apps/remote", json={"type": "http", "url": "https://example.com/mcp"})
        assert r.status_code == 403
        assert (await c.post("/api/backup/export", json={"passphrase": "x" * 12})).status_code == 403
        assert (await c.put("/api/backup/restore", content=b"x", headers={"x-passphrase": "eA=="})).status_code == 403
        assert (await c.post("/api/backup/restore/confirm")).status_code == 403
        r = await c.put("/api/settings", json={"limits": {"daily_token_budget": 10 ** 9}})
        assert r.status_code == 400 and "用量上限" in r.json()["detail"]
        r = await c.post("/api/providers/check", json={"base_url": "http://127.0.0.1:8317/v1"})
        assert r.status_code == 400 and "内网" in r.json()["detail"]
        r = await c.post("/api/providers", json={"base_url": "http://169.254.169.254/v1", "api_key": "k"})
        assert r.status_code == 400


# ---------- 不分模式 ----------
def test_changing_provider_address_needs_the_key_again(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-server")
    old = CFG["providers"]["deepseek"]["base_url"]
    with pytest.raises(ValueError, match="重新填 API Key"):
        providers.save("deepseek", {"base_url": "https://evil.example.com/v1"})
    with pytest.raises(ValueError, match="重新填 API Key"):
        providers.save("deepseek", {"base_url": old.replace("https://", "http://")})
    providers.save("deepseek", {"base_url": old + "/v1"})              # 同一个服务换路径不用
    providers.save("deepseek", {"base_url": old})
    assert providers.same_origin("https://a.com", "https://A.com:443/v1")
    assert not providers.same_origin("https://a.com", "https://a.com:8443")


async def test_check_does_not_send_stored_key_elsewhere(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-server")
    sent = []

    async def check(url, key):
        sent.append(key)
        return {"ok": True, "models": [], "error": "", "note": ""}
    monkeypatch.setattr(providers, "check", check)
    async with _client() as c:
        await c.post("/api/providers/check", json={"name": "deepseek", "base_url": "https://evil.example.com/v1"})
        await c.post("/api/providers/check", json={"name": "deepseek", "base_url": CFG["providers"]["deepseek"]["base_url"]})
    assert sent == ["", "sk-server"]


def test_market_values_cannot_carry_references():
    for v in ("{env:OSA_PASSWORD}", "env:OSA_PASSWORD", "x{vault:LLM_KEY_DEEPSEEK}"):
        with pytest.raises(ValueError, match="引用"):
            catalog.build("filesystem", {"path": v})
