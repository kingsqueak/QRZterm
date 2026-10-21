#!/bin/sh
# Install QRZterm into its own virtual environment and link the command onto PATH.
# Default command: /usr/local/bin/qrzterm
# Override the prefix with PREFIX=/opt/qrzterm sudo -E ./install.sh

set -eu

PREFIX="${PREFIX:-/usr/local}"
PYTHON="${PYTHON:-python3}"

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
VENV="$PREFIX/lib/qrzterm"
TARGET="$PREFIX/bin/qrzterm"

if [ ! -w "$PREFIX" ] && [ "$(id -u)" -ne 0 ]; then
    echo "Cannot write to $PREFIX. Run: sudo $0" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "QRZterm needs Python 3.12 or newer. $PYTHON is:" >&2
    "$PYTHON" --version >&2 || true
    exit 1
fi

mkdir -p "$PREFIX/bin" "$PREFIX/lib"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install "$ROOT"
ln -sfn "$VENV/bin/qrzterm" "$TARGET"

echo "Installed $TARGET"
"$TARGET" --version
