#!/usr/bin/env bash
# Set up the venv, install deps, and register the systemd user service.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".env"

if [ ! -d "$VENV" ]; then
    echo "Creating venv at $VENV"
    "$PYTHON" -m venv "$VENV"
fi

echo "Installing dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

if command -v systemctl >/dev/null 2>&1; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cp auto-sorter.service "$UNIT_DIR/auto-sorter.service"
    systemctl --user daemon-reload
    systemctl --user enable --now auto-sorter.service
    echo
    echo "Service installed and running."
    echo "To start it at boot without logging in, run once:"
    echo "    sudo loginctl enable-linger $USER"
    echo
    systemctl --user --no-pager status auto-sorter.service || true
else
    echo
    echo "systemd not found. Run manually:"
    echo "    $VENV/bin/python auto-sorter.py"
fi
