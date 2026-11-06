#!/usr/bin/env bash
# 准备桌面 App 要带的运行环境，放进 desktop/build/：
#   runtime/  独立的 Python（python-build-standalone）+ 依赖 + uv（uvx 应用要用）
#   app/      服务端代码（osa/，含构建好的前端）、config.yaml、pyproject.toml
# 用法：desktop/scripts/build-runtime.sh [aarch64|x86_64]   不写就是这台机器的架构
# 先构建好前端（cd web && npm run build）。需要 uv。
set -euo pipefail

ARCH="${1:-$(uname -m)}"
[ "$ARCH" = "arm64" ] && ARCH=aarch64
case "$ARCH" in aarch64|x86_64) ;; *) echo "不支持的架构：$ARCH" >&2; exit 1 ;; esac
PY_VERSION=3.13
TRIPLE="$ARCH-apple-darwin"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/desktop/build"
cd "$ROOT"
[ -f osa/static/dist/index.html ] || { echo "先构建前端：cd web && npm run build" >&2; exit 1; }

rm -rf "$OUT/runtime" "$OUT/app" "$OUT/python"
mkdir -p "$OUT"

echo "==> Python $PY_VERSION ($ARCH)"
UV_PYTHON_INSTALL_DIR="$OUT/python" uv python install --no-bin "cpython-$PY_VERSION-macos-$ARCH-none" >/dev/null
PY_DIR="$(find "$OUT/python" -maxdepth 1 -type d -name "cpython-$PY_VERSION*-macos-$ARCH-none" | head -1)"
[ -n "$PY_DIR" ] || { echo "没找到下载的 Python" >&2; exit 1; }
cp -R "$PY_DIR" "$OUT/runtime"
rm -rf "$OUT/python"
PY="$OUT/runtime/bin/python3"

echo "==> 依赖（按 uv.lock，不含开发依赖）"
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements.txt -o "$OUT/requirements.txt" >/dev/null
# cryptography 从 49.0（2026-06）起不再提供 Intel Mac 的安装包。Intel 版用最后一个有 universal2 包的 48.0
# （用到的 Fernet、AESGCM、Scrypt、HKDF、ec 这些接口没变，测试在 48.0 下全过）
: > "$OUT/overrides.txt"
[ "$ARCH" = x86_64 ] && echo "cryptography==48.0.0" > "$OUT/overrides.txt"
# 只用现成的安装包：缺了就报错，不在这台机器上现场编译（交叉编译出来的会缺 OpenSSL 这类依赖，装上了也用不了）
uv pip install --quiet --python "$PY" --python-platform "$TRIPLE" --break-system-packages --only-binary :all: \
  --override "$OUT/overrides.txt" -r "$OUT/requirements.txt" "uv==$(uv --version | awk '{print $2}')"
rm "$OUT/requirements.txt" "$OUT/overrides.txt"

echo "==> 精简"
LIB="$OUT/runtime/lib/python$PY_VERSION"
rm -rf "$LIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$LIB"/lib-dynload/_tkinter* \
  "$LIB"/site-packages/pip "$LIB"/site-packages/pip-* "$OUT/runtime/bin"/pip* \
  "$OUT/runtime"/lib/{tcl,tk,itcl,thread}* "$OUT/runtime/share" "$OUT/runtime/include"
find "$OUT/runtime" -type d \( -name __pycache__ -o -name tests -o -name test \) -prune -exec rm -rf {} +
# bin 里只留 python3、uv、uvx：Tauri 打包时会把符号链接展开成实体文件，python/python3/python3.13 会变成三份；
# 那些命令行脚本的第一行写死了这台机器上的路径，在 App 里也用不了
(cd "$OUT/runtime/bin" && mv "python$PY_VERSION" python3.real && find . -maxdepth 1 ! -name . ! -name python3.real ! -name uv ! -name uvx -exec rm -rf {} + && mv python3.real python3)
# Python 本体是静态链接的，扩展模块也不依赖 libpython；编译扩展用的东西也用不着
rm -rf "$OUT/runtime"/lib/libpython* "$OUT/runtime/lib/pkgconfig" "$LIB"/config-*
rm -f "$OUT/runtime/lib/python$PY_VERSION/EXTERNALLY-MANAGED"
# 去掉调试符号（Python 本体和扩展模块能小一半）。这会让原来的签名失效，先补一个临时签名；发布时 CI 再用 Developer ID 签
find "$OUT/runtime" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q Mach-O; then strip -x "$f" 2>/dev/null || true; codesign -f -s - "$f" 2>/dev/null || true; fi
done

echo "==> 服务端代码"
mkdir -p "$OUT/app"
rsync -a --exclude __pycache__ osa "$OUT/app/"
cp config.yaml pyproject.toml LICENSE "$OUT/app/"

echo "==> 试一下能不能导入"
RUN=()
[ "$ARCH" != "$(uname -m | sed 's/arm64/aarch64/')" ] && RUN=(arch -"$ARCH")   # Apple 芯片上打 Intel 版：用 Rosetta 试
(cd "$OUT/app" && OSA_DATA_DIR="$(mktemp -d)" ${RUN[@]+"${RUN[@]}"} "$PY" -B -c "import osa.server; print('ok')")
du -sh "$OUT/runtime" "$OUT/app"
