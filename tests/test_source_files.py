"""文件夹有新文件：第一次只记现状、新文件要连着两次一样大才报、没下完的和隐藏文件不看、服务器锁下不能用。"""
import os

import pytest

from osa import events
from osa.sources import files


@pytest.fixture(autouse=True)
def local(monkeypatch):
    monkeypatch.setattr(files.security, "local_exec", lambda: True)


def put(d, name: str, data: bytes = b"x", mtime: float = 1_790_000_000):
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


def P(d, **kw) -> dict:
    return events.clean_params(files.FOLDER, {"path": str(d)} | kw)


async def test_baseline_then_new_file_after_it_settles(tmp_path):
    put(tmp_path, "old.txt", mtime=1_790_000_000)
    put(tmp_path, "newest.txt", mtime=1_790_000_500)
    p = P(tmp_path)
    evs, st = await files.check(p, None)
    # 第一次：只记下现状，最新的一个给“测试”当示例（fire_first 是 False，引擎不会报）
    assert not files.FOLDER.fire_first
    assert [e["title"] for e in evs] == ["新文件：newest.txt"] and set(st["files"]) == {"old.txt", "newest.txt"}
    f = put(tmp_path, "report.pdf", b"a" * 10, mtime=1_790_001_000)
    evs, st = await files.check(p, st)
    assert evs == [] and "report.pdf" in st["pending"]            # 第一次看到：等一等
    f.write_bytes(b"a" * 2000)                                    # 还在写
    os.utime(f, (1_790_001_060, 1_790_001_060))
    evs, st = await files.check(p, st)
    assert evs == [] and st["pending"]["report.pdf"][0] == 2000
    evs, st = await files.check(p, st)                            # 两次一样大：写完了
    assert len(evs) == 1
    e = evs[0]
    assert e["title"] == "新文件：report.pdf" and f"位置：{tmp_path / 'report.pdf'}" in e["text"]
    assert "大小：2.0 KB" in e["text"] and "类型：application/pdf" in e["text"]
    assert st["pending"] == {} and "report.pdf" in st["files"]
    assert (await files.check(p, st))[0] == []


async def test_skips_hidden_partial_and_unmatched(tmp_path):
    p = P(tmp_path, pattern="*.pdf, *.PNG")
    _, st = await files.check(p, None)
    for name in (".DS_Store", "movie.mp4.crdownload", "a.pdf.part", "~$report.docx", "notes.txt", ".hidden/x.pdf"):
        put(tmp_path, name)
    put(tmp_path, "Shot.png")
    put(tmp_path, "BILL.PDF")
    _, st = await files.check(p, st)
    evs, st = await files.check(p, st)
    assert sorted(e["title"] for e in evs) == ["新文件：BILL.PDF", "新文件：Shot.png"]


async def test_recursive(tmp_path):
    put(tmp_path, "sub/deep/a.txt")
    flat, deep = P(tmp_path), P(tmp_path, recursive=True)
    assert (await files.check(flat, None))[1]["files"] == {}
    _, st = await files.check(deep, None)
    assert list(st["files"]) == ["sub/deep/a.txt"]
    put(tmp_path, "sub/b.txt")
    _, st = await files.check(deep, st)
    evs, st = await files.check(deep, st)
    assert [e["title"] for e in evs] == ["新文件：sub/b.txt"]


async def test_modified_files(tmp_path):
    f = put(tmp_path, "todo.md", b"1")
    quiet, loud = P(tmp_path), P(tmp_path, modified=True)
    _, st_q = await files.check(quiet, None)
    _, st_l = await files.check(loud, None)
    f.write_bytes(b"12")
    os.utime(f, (1_790_009_000, 1_790_009_000))
    for _ in range(2):
        evs_q, st_q = await files.check(quiet, st_q)
        evs_l, st_l = await files.check(loud, st_l)
        assert evs_q == []
    assert [e["title"] for e in evs_l] == ["文件改了：todo.md"]
    assert st_q["files"]["todo.md"][0] == 2       # 不报也记下新的大小
    f.unlink()
    _, st_q = await files.check(quiet, st_q)
    assert st_q["files"] == {}


async def test_many_new_files_are_summarized(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_REPORT", 3)
    p = P(tmp_path)
    _, st = await files.check(p, None)
    for i in range(5):
        put(tmp_path, f"f{i}.txt", mtime=1_790_000_000 + i)
    _, st = await files.check(p, st)
    evs, st = await files.check(p, st)
    assert [e["title"] for e in evs] == ["新文件：f0.txt", "新文件：f1.txt", "新文件：f2.txt", "另外还有 2 个文件"]
    assert evs[-1]["text"] == "f3.txt\nf4.txt"


async def test_state_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILES", 3)
    for i in range(6):
        put(tmp_path, f"f{i}.txt", mtime=1_790_000_000 + i)
    _, st = await files.check(P(tmp_path), None)
    assert sorted(st["files"]) == ["f3.txt", "f4.txt", "f5.txt"]     # 只记最新的
    evs, st = await files.check(P(tmp_path), st)
    assert evs == [] and st["pending"] == {}                         # 旧的不会被当成新文件


def test_validate(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="文件夹不存在"):
        P(tmp_path / "nope")
    with pytest.raises(ValueError, match="要填文件夹"):
        events.clean_params(files.FOLDER, {})
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    p = events.clean_params(files.FOLDER, {"path": "~/Downloads", "pattern": "*.pdf"})
    assert p["path"] == "~/Downloads" and files.FOLDER.summary(p) == "~/Downloads（*.pdf）"
    assert files.FOLDER.summary(P(tmp_path)) == str(tmp_path)


async def test_folder_gone(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    p = P(d)
    d.rmdir()
    with pytest.raises(ValueError, match="文件夹不存在了"):
        await files.check(p, None)


async def test_server_lock(tmp_path, monkeypatch):
    p = P(tmp_path)
    monkeypatch.setattr(files.security, "local_exec", lambda: False)
    assert "服务器锁" in events.unavailable(files.FOLDER)
    with pytest.raises(ValueError, match="服务器锁"):
        events.clean({"kind": "event", "source": "folder", "params": {"path": str(tmp_path)}})
    with pytest.raises(ValueError, match="服务器锁"):
        await files.check(p, None)


def test_scenarios_have_valid_params():
    for sc in files.FOLDER.scenarios:
        assert set(sc["params"]) <= {f["key"] for f in files.FOLDER.fields}
