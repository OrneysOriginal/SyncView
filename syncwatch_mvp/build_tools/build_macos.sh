#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Использование: ./build_tools/build_macos.sh /путь/к/подготовленному/vlc-runtime [--clean]" >&2
  exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VLC_DIR="$(cd "$1" && pwd)"

if [[ ! -f "$VLC_DIR/lib/libvlc.dylib" && ! -f "$VLC_DIR/libvlc.dylib" ]]; then
  echo "В $VLC_DIR не найден libvlc.dylib" >&2
  exit 1
fi
if [[ ! -d "$VLC_DIR/plugins" && ! -d "$VLC_DIR/lib/vlc/plugins" ]]; then
  echo "В $VLC_DIR не найден каталог плагинов VLC" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
if [[ "${2:-}" == "--clean" ]]; then
  rm -rf build dist
fi

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt

export SYNCWATCH_VLC_DIR="$VLC_DIR"
pyinstaller --noconfirm --clean packaging/syncwatch.spec

ARCH="$(uname -m)"
DITTO_OUT="$PROJECT_ROOT/dist/SyncWatch-macOS-${ARCH}.zip"
rm -f "$DITTO_OUT"
ditto -c -k --sequesterRsrc --keepParent "$PROJECT_ROOT/dist/SyncWatch.app" "$DITTO_OUT"
echo "Готово: $DITTO_OUT"
