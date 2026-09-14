#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-remote"
TOKEN_FILE="${HERDR_RELAY_TOKEN_FILE:-$CONFIG_DIR/run-token}"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is required but was not found in PATH." >&2
    exit 1
fi

TOKEN_SOURCE="HERDR_RELAY_TOKEN environment variable"
if [[ -z "${HERDR_RELAY_TOKEN:-}" ]]; then
    TOKEN_SOURCE="$TOKEN_FILE"
    if [[ -f "$TOKEN_FILE" ]]; then
        if [[ -L "$TOKEN_FILE" || ! -O "$TOKEN_FILE" ]]; then
            echo "Error: refusing token file not owned directly by the current user: $TOKEN_FILE" >&2
            exit 1
        fi
        chmod 600 "$TOKEN_FILE"
        HERDR_RELAY_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
    else
        mkdir -p "$CONFIG_DIR"
        umask 077
        if command -v openssl >/dev/null 2>&1; then
            HERDR_RELAY_TOKEN="$(openssl rand -hex 32)"
        else
            HERDR_RELAY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        fi
        printf '%s\n' "$HERDR_RELAY_TOKEN" > "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE"
    fi
fi

if [[ ! "$HERDR_RELAY_TOKEN" =~ ^[A-Za-z0-9_-]{16,128}$ ]]; then
    echo "Error: relay token must contain 16-128 URL-safe characters." >&2
    exit 1
fi

LAN_IP="$(python3 - <<'PY'
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.connect(("1.1.1.1", 80))
    print(sock.getsockname()[0])
except OSError:
    print("127.0.0.1")
finally:
    sock.close()
PY
)"

export HERDR_RELAY_HOST=0.0.0.0
export HERDR_RELAY_PORT=8001
export HERDR_RELAY_TOKEN

ACCESS_URL="http://$LAN_IP:$HERDR_RELAY_PORT/?token=$HERDR_RELAY_TOKEN"
printf 'Herdr Remote: %s\n' "$ACCESS_URL"
printf 'Token source: %s\n\n' "$TOKEN_SOURCE"

HERDR_ACCESS_URL="$ACCESS_URL" uv run --with 'qrcode>=8.0' python - <<'PY'
import os
import sys

import qrcode

qr = qrcode.QRCode(border=2)
qr.add_data(os.environ["HERDR_ACCESS_URL"])
qr.make(fit=True)
qr.print_ascii(tty=sys.stdout.isatty(), invert=not sys.stdout.isatty())
PY
printf '\nScan the QR code to open Herdr Remote. Press Ctrl+C to stop.\n\n'

exec uv run "$SCRIPT_DIR/relay/herdr_relay.py"
