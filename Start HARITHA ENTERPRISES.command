#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

if [ ! -x .venv/bin/python ]; then
    printf 'Run Install HARITHA ENTERPRISES.command first.\n' >&2
    printf 'Press Return to close this window... '
    read -r unused || true
    exit 1
fi

exec .venv/bin/python desktop_launcher.py
