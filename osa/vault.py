"""保险库：加密保存密钥（API key、token 等）。

明文只在两个地方出现：用户在界面上录入的那一刻，以及启动应用时注入进程环境变量。
接口和模型只能看到名字。主密钥优先取环境变量 OSA_VAULT_KEY，没有就在数据目录生成一个（权限 600）。
"""
import base64
import hashlib
import os
import re
import time
from cryptography.fernet import Fernet, InvalidToken
from . import i18n
from .memory import conn, db_path

conn.execute("create table if not exists vault(name text primary key, ciphertext blob, created real)")
conn.commit()

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_fernet: Fernet | None = None


def _key() -> Fernet:
    global _fernet
    if _fernet:
        return _fernet
    raw = os.environ.get("OSA_VAULT_KEY")
    if not raw:
        f = db_path.parent / "vault.key"
        if not f.exists():
            f.write_bytes(Fernet.generate_key())
            f.chmod(0o600)
        raw = f.read_text().strip()
    try:
        _fernet = Fernet(raw.encode())
    except ValueError:   # 不是 Fernet 格式的口令：派生一个
        _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))
    return _fernet


def put(name: str, value: str):
    if not NAME.match(name):
        raise ValueError(i18n.t("名字只能用字母、数字和下划线，且不能以数字开头"))
    if not value:
        raise ValueError(i18n.t("值不能为空"))
    conn.execute("insert or replace into vault values(?,?,?)", (name, _key().encrypt(value.encode()), time.time()))
    conn.commit()


def get(name: str) -> str | None:
    r = conn.execute("select ciphertext from vault where name=?", (name,)).fetchone()
    if not r:
        return None
    try:
        return _key().decrypt(r[0]).decode()
    except InvalidToken:
        return None   # 主密钥换过了，旧值解不开


def delete(name: str):
    conn.execute("delete from vault where name=?", (name,))
    conn.commit()


def names() -> list[dict]:
    return [{"name": n, "created": c, "ok": get(n) is not None}
            for n, c in conn.execute("select name, created from vault order by name")]
