#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

finish() {
    result=$?
    printf '\n'
    if [ "$result" -eq 0 ]; then
        printf 'Installation complete. Open HARITHA ENTERPRISES from your Applications folder.\n'
    else
        printf 'Installation failed. Check the error above and try again.\n'
    fi
    printf 'Press Return to close this window... '
    read -r unused || true
}
trap finish EXIT

PYTHON=
for candidate in python3 python3.14 python3.13 python3.12; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        PYTHON=$candidate
        break
    fi
done
if [ -z "$PYTHON" ]; then
    printf 'Python 3.12 or newer with Tkinter is required.\n' >&2
    exit 1
fi

"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements-desktop.txt
if ! .venv/bin/python create_mac_app.py; then
    printf 'The Applications shortcut could not be created. Use Start HARITHA ENTERPRISES.command instead.\n' >&2
fi
