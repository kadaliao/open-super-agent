"""桌面版要用的服务端功能：一次性登录票、cookie 改名、App 退出时服务跟着停。"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from osa import auth, server
from osa.config import ROOT
from test_auth import _call, _scope


async def _post_ticket(secret: str, origin=None):
    sent = await _call(server.app, _scope(method="POST", path="/login/ticket", origin=origin,
                                          extra=[("authorization", f"Bearer {secret}")]))
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return sent[0]["status"], json.loads(body or b"{}")


async def _use_ticket(ticket: str, nxt="/apps") -> tuple[int, dict]:
    sent = await _call(server.app, _scope(path="/login/ticket", query=f"t={ticket}&next={nxt}"))
    return sent[0]["status"], {k.decode(): v.decode() for k, v in sent[0]["headers"]}


async def test_ticket_logs_the_window_in(password, monkeypatch):
    monkeypatch.setattr(auth, "_fails", {})
    assert (await _post_ticket("wrong"))[0] == 401
    status, body = await _post_ticket("test-password")
    assert status == 200 and body["expires_in"] == auth.TICKET_TTL

    status, head = await _use_ticket(body["ticket"])
    assert status == 303 and head["location"] == "/apps"
    name, _, token = head["set-cookie"].split(";")[0].partition("=")
    assert name == auth.COOKIE and auth.check_token(token)
    assert "HttpOnly" in head["set-cookie"]

    status, head = await _use_ticket(body["ticket"])                     # 只能用一次
    assert head["location"] == "/login?next=%2Fapps" and "set-cookie" not in head


async def test_expired_or_unknown_ticket(password, monkeypatch):
    monkeypatch.setattr(auth, "_fails", {})
    _, body = await _post_ticket("test-password")
    monkeypatch.setitem(auth._tickets, body["ticket"], time.time() - 1)
    assert (await _use_ticket(body["ticket"]))[1]["location"].startswith("/login")
    assert (await _use_ticket("made-up"))[1]["location"].startswith("/login")


async def test_other_sites_cannot_get_a_ticket(password):
    assert (await _post_ticket("test-password", origin="https://evil.example"))[0] == 403


async def test_ticket_needs_login_enabled(no_password):
    assert (await _post_ticket("x"))[0] == 400
    assert (await _use_ticket("x", nxt="/"))[1]["location"] == "/"


@pytest.mark.parametrize("value,want", [("osa_desktop", "osa_desktop"), ("a;b=c", "osa_session"), ("", "osa_session")])
def test_cookie_name(value, want):
    out = subprocess.run([sys.executable, "-c", "from osa import auth; print(auth.COOKIE)"], cwd=ROOT,
                         env={**os.environ, "OSA_COOKIE": value}, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == want, out.stderr


async def test_server_stops_when_the_app_is_gone(monkeypatch):
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if pid == 4242 and len(calls) > 2:   # 前两次 App 还在，第三次不在了
            raise ProcessLookupError
    monkeypatch.setattr(os, "kill", fake_kill)
    await asyncio.wait_for(server.parent_watch(4242, every=0.01), 2)
    assert calls == [(4242, 0)] * 3 + [(os.getpid(), signal.SIGTERM)]
