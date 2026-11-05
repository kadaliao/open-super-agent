"""python -m osa.catalog：检查所有应用清单。有错时逐个列出来，退出码为 1（CI 里也跑）。"""
import sys
from . import LOCAL, _files, _show, load

items, errors = load()
for e in errors:
    print("✗", e)
local = [_show(p) for p in _files().values() if p.parent == LOCAL]
if local:
    print(f"本机清单（{_show(LOCAL)}，同名的覆盖内置的）：{'、'.join(local)}")
print(f"{len(items)} 个应用可用" + (f"，{len(errors)} 份清单有错" if errors else "，清单都没问题"))
sys.exit(1 if errors else 0)
