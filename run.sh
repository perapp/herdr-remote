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
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys


def parse_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        return None
    return address


def is_rfc1918(address):
    return any(address in network for network in (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ))


def interface_score(interface, address):
    virtual_prefixes = (
        "awdl", "br-", "docker", "gif", "llw", "lo", "podman", "stf",
        "tailscale", "tap", "tun", "utun", "veth", "virbr", "wg",
    )
    physical_prefixes = ("en", "eth", "wlan", "wlp")
    score = 100 if is_rfc1918(address) else 0
    if interface.startswith(physical_prefixes):
        score += 50
    if interface.startswith(virtual_prefixes):
        score -= 200
    return score


def add_candidate(candidates, interface, value):
    address = parse_ipv4(value)
    if address is None or address.is_link_local:
        return
    candidate = (interface_score(interface, address), interface, str(address))
    if candidate not in candidates:
        candidates.append(candidate)


override = os.environ.get("HERDR_LAN_IP")
if override:
    address = parse_ipv4(override)
    if address is None:
        print("Error: HERDR_LAN_IP must be a usable IPv4 address.", file=sys.stderr)
        raise SystemExit(1)
    print(address)
    raise SystemExit

candidates = []
if shutil.which("ip"):
    try:
        output = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "up"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        for interface in json.loads(output):
            name = interface.get("ifname", "")
            for info in interface.get("addr_info", []):
                add_candidate(candidates, name, info.get("local", ""))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

if shutil.which("ifconfig"):
    try:
        output = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=2
        ).stdout
        interface = ""
        for line in output.splitlines():
            match = re.match(r"^([A-Za-z0-9_.:-]+):", line)
            if match:
                interface = match.group(1)
                continue
            match = re.match(r"^\s*inet\s+(?:addr:)?([0-9.]+)", line)
            if interface and match:
                add_candidate(candidates, interface, match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass

if candidates:
    # Prefer RFC 1918 addresses on physical interfaces over VPN/tunnel addresses.
    print(max(candidates, key=lambda candidate: candidate[0])[2])
    raise SystemExit

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
export HERDR_SHELL_PANES=1

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
