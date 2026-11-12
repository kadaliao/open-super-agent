"""网页界面的版本编号：前端构建时写进 index.html 的 <meta name="osa-build">，服务端读出来告诉开着的页面，
页面的和它不一样就提示更新。只改网页的部署是直接换掉 dist 目录、不重启服务，所以换了之后要马上读到新的。"""
import os
import httpx
from osa import server


def _write(dist, build):
    dist.mkdir(exist_ok=True)
    meta = f'<meta name="osa-build" content="{build}">' if build else ""
    (dist / "index.html").write_text(f"<!doctype html><html><head>{meta}</head><body></body></html>")


async def test_build_follows_index_html(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    monkeypatch.setattr(server, "DIST", dist)
    monkeypatch.setattr(server, "_build", (None, ""))
    assert server.web_build() == ""                     # 还没构建前端
    _write(dist, "aaa111")
    assert server.web_build() == "aaa111"
    assert await server.current_build() == {"build": "aaa111"}

    # 部署只改网页的：整个 dist 换成新的（mv dist dist.prev && mv dist.new/dist dist），服务不重启
    new = tmp_path / "new"
    _write(new, "bbb222")
    dist.rename(tmp_path / "dist.prev")
    new.rename(dist)
    assert server.web_build() == "bbb222"

    _write(dist, "")                                    # 旧的构建没有这个编号：不提示更新
    os.utime(dist / "index.html", ns=(1, 1))
    assert server.web_build() == ""


async def test_bootstrap_and_endpoint_carry_build(monkeypatch):
    # 后端重启过的部署：页面重连后从 bootstrap 拿；只改网页的部署：页面隔一阵问 /api/build
    monkeypatch.setattr(server, "web_build", lambda: "ccc333")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1") as c:
        assert (await c.get("/api/bootstrap")).json()["build"] == "ccc333"
        assert (await c.get("/api/build")).json() == {"build": "ccc333"}
