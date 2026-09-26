"""Bounded, read-only Pi runtime catalogs. Never load or evaluate extension source."""
import json
import math
import os
import re
import stat
import time

try:
    import transcript
except ModuleNotFoundError:
    from importlib.util import module_from_spec, spec_from_file_location

    _spec = spec_from_file_location(
        "herdr_commands_transcript", os.path.join(os.path.dirname(__file__), "transcript.py"))
    transcript = module_from_spec(_spec)
    _spec.loader.exec_module(transcript)

SUFFIX = ".herdr-commands.json"
MAX_BYTES = 128 * 1024
MAX_COMMANDS = 500
TTL_SECONDS = 75
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SOURCES = {"extension", "template", "skill"}
ENABLED = os.environ.get("HERDR_COMMAND_DISCOVERY", "1").strip().lower() not in {
    "0", "false", "no", "off"}


def unavailable(reason):
    return {"commands": [], "unavailable": reason}


def _allocated_session(path):
    """Validate a Pi-allocated filename without requiring or creating its transcript."""
    if (not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path
            or not path.endswith(".jsonl")):
        return None
    parts = path.replace(os.altsep, os.sep) if os.altsep else path
    if any(part in {".", ".."} for part in parts.split(os.sep)):
        return None
    try:
        roots = [os.path.realpath(os.path.expanduser(root)) for root in transcript.PI_ROOTS]

        def contained(value):
            return any(os.path.commonpath([value, root]) == root for root in roots)

        parent = os.path.realpath(os.path.dirname(path), strict=True)
        if not os.path.isdir(parent) or not contained(parent):
            return None
        allocated = os.path.join(parent, os.path.basename(path))
        try:
            os.lstat(allocated)
        except FileNotFoundError:
            return allocated
        # Resolve existing links strictly: dangling links are not allocated filenames.
        canonical = os.path.realpath(allocated, strict=True)
        if (canonical.endswith(".jsonl") and contained(canonical)
                and stat.S_ISREG(os.stat(canonical).st_mode)):
            return canonical
    except (OSError, ValueError):
        return None
    return None


def _read(path):
    """Reject links/devices/FIFOs, including replacements between lstat and open."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
        raise ValueError("invalid catalog file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_BYTES
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("catalog changed")
        blob = handle.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise ValueError("catalog too large")
    return json.loads(blob)


def _validate(data, session_file, now):
    if not isinstance(data, dict):
        return unavailable("error")
    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        return unavailable("unsupported")
    if data.get("sessionFile") != session_file:
        return unavailable("error")
    pid, updated = data.get("pid"), data.get("updatedAt")
    if type(pid) is not int or pid <= 0:
        return unavailable("error")
    if type(updated) not in (int, float) or not math.isfinite(updated):
        return unavailable("error")
    # Epoch milliseconds, like Date.now(). Future dates must not extend the TTL indefinitely.
    age = now - updated / 1000
    if age > TTL_SECONDS or age < -5:
        return unavailable("stale")
    commands = data.get("commands")
    if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
        return unavailable("error")
    result, seen = [], set()
    for command in commands:
        if not isinstance(command, dict):
            return unavailable("error")
        name, description, source = (command.get(key) for key in ("name", "description", "source"))
        if (not isinstance(name, str) or not NAME_RE.fullmatch(name)
                or not isinstance(description, str) or len(description) > 200
                or not isinstance(source, str) or source not in SOURCES):
            return unavailable("error")
        if name in seen:
            continue
        seen.add(name)
        result.append({"cmd": "/" + name, "desc": description, "source": source, "common": True})
    return {"commands": result}


def commands(session, remote=None, agent="", now=None):
    """Read only the session ref supplied by relay state, never a client path."""
    if not ENABLED:
        return unavailable("disabled")
    if agent and agent != "pi":
        return unavailable("unsupported")
    if not isinstance(session, dict):
        return unavailable("no-session")
    harness = session.get("agent") or agent
    if harness != "pi":
        return unavailable("unsupported")
    value = session.get("value")
    if (session.get("kind") != "path" or not isinstance(value, str)
            or not os.path.isabs(value) or "\x00" in value):
        return unavailable("no-session")
    if remote:
        return unavailable("remote")
    try:
        session_file = _allocated_session(value)
        if not session_file:
            return unavailable("no-session")
        try:
            data = _read(session_file + SUFFIX)
        except FileNotFoundError:
            return unavailable("no-catalog")
        return _validate(data, session_file, time.time() if now is None else now)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return unavailable("error")
