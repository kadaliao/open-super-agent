"""用服务器真正的加密代码（osa/push.py）生成一条推送，给 WebPushCrypto 的 Swift 测试用：证明两边对得上。

uv run python mobile/scripts/make-push-fixture.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["OSA_DATA_DIR"] = tempfile.mkdtemp(prefix="osa-fixture-")   # 别碰真的数据库
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from osa import push  # noqa: E402

device = ec.generate_private_key(ec.SECP256R1())
auth = os.urandom(16)
pub = device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
payload = {"title": "小助手 需要你确认", "body": "send_mail：给 Alice 发周报 ✉️", "url": "/?chat=12&thread=34", "tag": "approval-7", "badge": 2}
body = push.encrypt(json.dumps(payload, ensure_ascii=False).encode(), push.b64u(pub), push.b64u(auth))
out = ROOT / "mobile/swift/WebPushCrypto/Tests/WebPushCryptoTests/Fixtures/from-server.json"
out.write_text(json.dumps({
    "private_key": push.b64u(device.private_numbers().private_value.to_bytes(32, "big")),
    "auth": push.b64u(auth), "body": push.b64u(body), "payload": payload,
}, ensure_ascii=False, indent=2) + "\n")
print("wrote", out.relative_to(ROOT))
