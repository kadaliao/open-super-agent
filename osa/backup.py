"""数据备份与迁移：把全部数据打成一个加密文件，换机器、从本机搬到服务器时恢复。

备份里有：数据库（Bot、对话、记忆、技能、定时任务、资料库页面、应用配置、确认规则、聊天软件连接、费用记录……）、
保险库里的密钥、data/apps/ 里只在本机生效的应用清单。
不在备份里：电脑（共用电脑里的文件、浏览器的登录状态）、登录用的 session.key、日志。

- 密钥：导出时用这台的主密钥解开，放进加密包；恢复时用新机器自己的主密钥重新加密。所以两台的 OSA_VAULT_KEY 不一样也没关系。
- 文件格式（.osabak）：第一行 "OSABAK1"，第二行 JSON 头（scrypt 参数、salt、nonce 前缀、块大小），后面是加密的 zip。
  zip 按 1 MiB 分块，每块用 AES-256-GCM 加密；nonce = 前缀 7 字节 + 块号 4 字节 + 是否最后一块 1 字节，
  头的两行作为附加数据。密钥由口令经 scrypt 派生。口令不对、文件被截短或改过都解不开。
- 恢复分两步：stage 解密、检查，放到 data/restore/，返回摘要给用户确认；confirm 标记好以后重启服务，
  启动时（apply_pending，在打开数据库之前）换上。换下来的旧数据库留在 data/osa.db.before-restore-<时间>。
- 恢复到另一台机器时：清掉 Bot 和电脑的对应关系（新机器按自己的设置开电脑），聊天软件连接先停用（两台连同一个机器人会互相顶掉）。

命令行：python -m osa.backup export [-o 文件]；python -m osa.backup restore 文件（下次启动时生效）。
口令可以放在环境变量 OSA_BACKUP_PASSPHRASE 里，方便定时备份。
"""
import base64
import json
import os
import re
import shutil
import signal
import sqlite3
import struct
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from . import i18n   # 只读 locales/en.json，不碰数据库（这个模块在打开数据库之前就要导入）
from .config import DATA, DB_PATH, ROOT

MAGIC = b"OSABAK1\n"
CHUNK = 1 << 20
FORMAT = 1
DB = DB_PATH
STAGE = DB.parent / "restore"
APPS = DATA / "apps"
MIN_PASS = 8
MAX_UNZIPPED = 8 << 30
NEED_TABLES = {"bots", "chats", "messages"}
COUNTS = {"bots": "个 Bot", "chats": "个对话", "messages": "条消息", "memories": "条记忆", "skills": "个技能",
          "routines": "个定时任务", "pages": "个页面", "apps": "个应用", "channels": "个聊天软件连接"}
RESTART = False   # 网页上确认恢复后置位：服务关闭时重新启动自己


class BackupError(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _key(passphrase: str, head: dict) -> AESGCM:
    kdf = Scrypt(salt=base64.b64decode(head["salt"]), length=32, n=head["n"], r=head["r"], p=head["p"])
    return AESGCM(kdf.derive(passphrase.encode()))


def _nonce(prefix: bytes, i: int, last: bool) -> bytes:
    return prefix + struct.pack(">IB", i, 1 if last else 0)


def check_passphrase(p: str):
    if len(p or "") < MIN_PASS:
        raise BackupError(i18n.t("口令至少 {n} 个字符：备份里有你所有的密钥，口令就是保护它们的锁", n=MIN_PASS))


# ---------- 加密、解密 ----------
def encrypt(src: Path, dst: Path, passphrase: str):
    salt, prefix = os.urandom(16), os.urandom(7)
    head = {"kdf": "scrypt", "n": 1 << 15, "r": 8, "p": 1, "salt": _b64(salt), "nonce": _b64(prefix), "chunk": CHUNK}
    hline = json.dumps(head).encode() + b"\n"
    aes, ad = _key(passphrase, head), MAGIC + hline
    with open(src, "rb") as f, open(dst, "wb") as out:
        out.write(MAGIC + hline)
        i, buf = 0, f.read(CHUNK)
        while True:
            nxt = f.read(CHUNK)
            out.write(aes.encrypt(_nonce(prefix, i, not nxt), buf, ad))
            if not nxt:
                return
            buf, i = nxt, i + 1


def decrypt(src: Path, dst: Path, passphrase: str):
    with open(src, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupError(i18n.t("这不是 open-super-agent 的备份文件"))
        hline = f.readline(2048)
        try:
            head = json.loads(hline)
            ok = head.get("kdf") == "scrypt" and 1 << 10 <= head["n"] <= 1 << 20 and head["r"] <= 16 and head["p"] <= 4 \
                and 1 << 12 <= head["chunk"] <= 1 << 24
            prefix = base64.b64decode(head["nonce"])
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok or len(prefix) != 7:
            raise BackupError(i18n.t("备份文件的开头坏了"))
        aes, ad, size = _key(passphrase, head), MAGIC + hline, head["chunk"] + 16
        with open(dst, "wb") as out:
            i, buf = 0, f.read(size)
            while True:
                nxt = f.read(size)
                try:
                    out.write(aes.decrypt(_nonce(prefix, i, not nxt), buf, ad))
                except InvalidTag:
                    raise BackupError(i18n.t("口令不对（或者文件坏了）") if i == 0 else i18n.t("备份文件不完整，或者被改过"))
                if not nxt:
                    return
                buf, i = nxt, i + 1


# ---------- 导出 ----------
def version() -> str:
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
        return m[1] if m else ""
    except OSError:
        return ""


def counts(db: sqlite3.Connection) -> dict:
    have = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    return {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in COUNTS if t in have}


def key_matches() -> bool:
    """这个进程的主密钥解得开保险库里的值吗（命令行没带上服务的 OSA_VAULT_KEY 时会对不上）。"""
    from . import memory, vault
    rows = memory.conn.execute("select ciphertext from vault limit 20").fetchall()
    for (ct,) in rows:
        try:
            vault._key().decrypt(ct)
            return True
        except Exception:
            pass
    return not rows


def snapshot() -> Path:
    """数据库的一致快照（服务照常运行）。放在临时目录里。"""
    from . import memory
    work = Path(tempfile.mkdtemp(prefix="osa-backup-"))
    db = sqlite3.connect(work / "osa.db")
    memory.conn.commit()
    memory.conn.backup(db)
    db.close()
    return work / "osa.db"


def export(passphrase: str, snap: Path | None = None) -> tuple[Path, dict]:
    """打一个备份，返回 (文件路径, 摘要)。文件在临时目录里，用完由调用方删掉整个目录。
    snap：先在事件循环里做好的快照，其余（解密密钥、压缩、加密）可以放到线程里做。"""
    check_passphrase(passphrase)
    from . import vault
    snap = snap or snapshot()
    work = snap.parent
    try:
        db = sqlite3.connect(snap)
        secrets, unreadable = {}, []
        for name, ct in db.execute("select name, ciphertext from vault").fetchall():
            try:
                secrets[name] = vault._key().decrypt(ct).decode()
            except Exception:
                unreadable.append(name)   # 主密钥换过、解不开的旧值：带不走
        db.execute("delete from vault")
        db.commit()
        meta = {"format": FORMAT, "created": time.time(), "version": version(), "counts": counts(db),
                "secrets": len(secrets), "unreadable": unreadable}
        db.execute("vacuum")
        db.close()
        apps = sorted(APPS.glob("*.yaml")) if APPS.is_dir() else []
        meta["local_apps"] = len(apps)
        zpath = work / "backup.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("backup.json", json.dumps(meta, ensure_ascii=False, indent=1))
            z.writestr("secrets.json", json.dumps(secrets, ensure_ascii=False))
            z.write(snap, "osa.db")
            for p in apps:
                z.write(p, f"apps/{p.name}")
        snap.unlink()
        out = work / f"osa-backup-{time.strftime('%Y%m%d-%H%M')}.osabak"
        encrypt(zpath, out, passphrase)
        zpath.unlink()
        return out, meta
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ---------- 恢复 ----------
def staged() -> dict | None:
    """已经解密检查好、等确认的备份的摘要。"""
    f = STAGE / "meta.json"
    if not f.exists() or (STAGE / "ready").exists():
        return None
    try:
        return json.loads(f.read_text())
    except ValueError:
        return None


def stage(upload: Path, passphrase: str) -> dict:
    """解密、检查备份，放到 data/restore/ 等确认。返回摘要。"""
    from . import vault
    work = Path(tempfile.mkdtemp(prefix="osa-restore-"))
    try:
        zpath = work / "backup.zip"
        decrypt(upload, zpath, passphrase)
        try:
            z = zipfile.ZipFile(zpath)
            names = set(z.namelist())
            meta = json.loads(z.read("backup.json"))
        except (zipfile.BadZipFile, KeyError, ValueError):
            raise BackupError(i18n.t("备份文件里的内容坏了"))
        if meta.get("format") != FORMAT:
            raise BackupError(i18n.t("这个备份是更新版本的 open-super-agent 做的，先升级再恢复"))
        if "osa.db" not in names or sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
            raise BackupError(i18n.t("备份文件里的内容不对"))
        shutil.rmtree(STAGE, ignore_errors=True)
        STAGE.mkdir(parents=True)
        with z.open("osa.db") as src, open(STAGE / "osa.db", "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        db = sqlite3.connect(STAGE / "osa.db")
        try:
            if db.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise BackupError(i18n.t("备份里的数据库坏了"))
        except sqlite3.DatabaseError:
            raise BackupError(i18n.t("备份里的数据库坏了"))
        have = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        if NEED_TABLES - have:
            raise BackupError(i18n.t("备份里不是 open-super-agent 的数据"))
        secrets = json.loads(z.read("secrets.json")) if "secrets.json" in names else {}
        key = vault._key()   # 这台机器的主密钥
        db.execute("create table if not exists vault(name text primary key, ciphertext blob, created real)")
        db.execute("delete from vault")
        for name, value in secrets.items():
            db.execute("insert into vault values(?,?,?)", (name, key.encrypt(str(value).encode()), time.time()))
        if "computers" in have:
            db.execute("delete from computers")   # 电脑是那台机器上的：这边按自己的设置开
        off = 0
        if "channels" in have:
            off = db.execute("update channels set enabled=0 where enabled=1").rowcount
        db.commit()
        info = meta | {"counts": counts(db), "channels_off": off}
        db.close()
        apps = [n for n in names if re.fullmatch(r"apps/[a-z][a-z0-9_-]{0,23}\.yaml", n)]
        if apps:
            (STAGE / "apps").mkdir()
            for n in apps:
                (STAGE / "apps" / n[5:]).write_bytes(z.read(n))
        z.close()
        (STAGE / "meta.json").write_text(json.dumps(info, ensure_ascii=False))
        return info
    except BaseException:
        shutil.rmtree(STAGE, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cancel():
    shutil.rmtree(STAGE, ignore_errors=True)


def confirm():
    """标记好，下次启动时换上。"""
    if not (STAGE / "meta.json").exists():
        raise BackupError(i18n.t("没有等待恢复的备份，请重新上传"))
    (STAGE / "ready").write_text(str(time.time()))


def apply_pending() -> bool:
    """服务启动、打开数据库之前调用：有确认过的备份就换上。"""
    if not (STAGE / "ready").exists() or not (STAGE / "osa.db").exists():
        return False
    ts = time.strftime("%Y%m%d-%H%M%S")
    old = DB.with_name(f"{DB.name}.before-restore-{ts}")
    if DB.exists():
        os.replace(DB, old)
    for suf in ("-wal", "-shm", "-journal"):   # 旧库没写完的日志跟着旧库走，不能留给新库
        p = Path(f"{DB}{suf}")
        if p.exists():
            os.replace(p, Path(f"{old}{suf}"))
    os.replace(STAGE / "osa.db", DB)
    if (STAGE / "apps").is_dir():
        if APPS.exists():
            os.replace(APPS, APPS.with_name(f"apps.before-restore-{ts}"))
        os.replace(STAGE / "apps", APPS)
    shutil.rmtree(STAGE, ignore_errors=True)
    print(f"已从备份恢复数据，原来的数据库留在 {old}", flush=True)
    return True


def request_restart(delay: float = 0.5):
    """让 uvicorn 正常关闭（回完这个请求），关闭的最后一步重新启动自己（restart）。"""
    global RESTART
    RESTART = True
    import asyncio
    asyncio.get_running_loop().call_later(delay, os.kill, os.getpid(), signal.SIGTERM)


def restart():
    """在服务关闭的最后调用：用同样的命令行重新执行自己（systemd、Docker、直接运行都适用）。"""
    from . import memory
    try:
        memory.conn.close()
    except Exception:
        pass
    argv = getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv]
    if "--multiprocessing-fork" in argv:   # uvicorn --reload 的子进程：重新执行不了，退出后要手动重启
        print("备份会在下次启动时恢复：开发模式下请手动重启服务", flush=True)
        os._exit(3)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)


# ---------- 命令行 ----------
def _passphrase(twice: bool) -> str:
    p = os.environ.get("OSA_BACKUP_PASSPHRASE")
    if p:
        return p
    import getpass
    p = getpass.getpass("备份口令：")
    if twice and getpass.getpass("再输一遍：") != p:
        sys.exit("两次输入的不一样")
    return p


def _summary(meta: dict) -> str:
    c = meta.get("counts") or {}
    parts = [f"{c[k]} {v}" for k, v in COUNTS.items() if c.get(k)]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.get("created") or 0))
    return f"{when} 的备份（v{meta.get('version') or '?'}）：" + "、".join(parts) + f"、{meta.get('secrets', 0)} 个密钥"


def main(argv: list[str]):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m osa.backup", description="备份和恢复 open-super-agent 的全部数据")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="导出一个加密的备份文件")
    e.add_argument("-o", "--output", help="存到哪里（文件或目录），默认当前目录")
    r = sub.add_parser("restore", help="从备份恢复：服务下次启动时生效")
    r.add_argument("file")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            if not key_matches():
                sys.exit("失败：这里的保险库主密钥和服务用的对不上。命令行要和服务用同样的环境变量（OSA_VAULT_KEY、OSA_CONFIG）")
            path, meta = export(_passphrase(True))
            dst = Path(a.output or ".")
            dst = dst / path.name if dst.is_dir() else dst
            shutil.move(path, dst)
            shutil.rmtree(path.parent, ignore_errors=True)
            print(f"已导出到 {dst}\n{_summary(meta)}")
            if meta["unreadable"]:
                print(f"有 {len(meta['unreadable'])} 个密钥解不开（主密钥换过），没有带上：{', '.join(meta['unreadable'])}")
        else:
            if not key_matches():
                sys.exit("失败：这里的保险库主密钥和服务用的对不上。命令行要和服务用同样的环境变量（OSA_VAULT_KEY、OSA_CONFIG）")
            meta = stage(Path(a.file), _passphrase(False))
            confirm()
            print(_summary(meta) + "\n已准备好，重启服务后换上（原来的数据库会留一份）。")
            if meta.get("channels_off"):
                print(f"{meta['channels_off']} 个聊天软件连接先停用了，确认原来那台已经停掉后，在 设置 → 聊天软件 里启用。")
    except BackupError as err:
        sys.exit(f"失败：{err}")


if __name__ == "__main__":
    main(sys.argv[1:])
