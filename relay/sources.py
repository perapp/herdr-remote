"""Registered Herdr sources and opaque, session-bound wire identifiers (stdlib only)."""
import hashlib
import json
import re
import subprocess
import threading
import time


class MachineRegistry:
    def __init__(self, setting="", ttl=30):
        self.setting = setting.strip()
        self.ttl = ttl
        self.machines = {}
        self.generation = 0
        self.error = None
        self._checked = None
        self._lock = threading.Lock()

    def refresh(self, discover, force=False):
        """A bad/failed read is not an empty registry. Serialize and bound discovery."""
        with self._lock:
            now = time.monotonic()
            if not force and self._checked is not None and now - self._checked < self.ttl:
                return False
            self._checked = now
            try:
                machines = {} if self.setting.lower() in {"off", "0", "false", "no"} else self.parse(discover())
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                self.error = str(exc)[:200]
                return False
            self.error = None
            changed = machines != self.machines
            if changed:
                self.machines = machines
                self.generation += 1
            return changed

    def parse(self, raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            data = data.get("machines")
        if not isinstance(data, list):
            raise TypeError("machine list must be an array")
        allow = None if self.setting.lower() in {"", "on", "1", "true", "yes", "auto"} else set(self.setting.split(","))
        result = {}
        seen = set()
        for item in data:
            if not isinstance(item, dict):
                raise TypeError("invalid machine profile")
            ident, target, session = (item.get(k) for k in ("id", "target", "session"))
            if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", ident) or ident in seen:
                raise ValueError("invalid or duplicate machine id")
            seen.add(ident)
            if not isinstance(target, str) or not target or target.startswith("-") or any(c.isspace() for c in target):
                raise ValueError("invalid machine target")
            if not isinstance(session, str) or not session or not isinstance(item.get("enabled"), bool):
                raise ValueError("invalid machine session/enabled")
            label = item.get("label") or ident
            if not isinstance(label, str):
                raise TypeError("invalid machine label")
            if item["enabled"] and (allow is None or ident in allow or label in allow):
                key = "machine:" + ident
                result[key] = {"id": ident, "label": label, "target": target, "session": session, "kind": "machine"}
        return result


def fingerprint(source, session, target):
    return hashlib.sha256(json.dumps([source, session, target], separators=(",", ":")).encode()).hexdigest()[:20]


def wire_id(source, session, target, raw):
    if source is None or not raw:
        return raw
    # Raw native identifiers are validated at ingestion; the digest keeps labels/targets off wire.
    prefix = "r" + fingerprint(source, session, target) + "."
    return prefix + raw if len(prefix + raw) <= 128 else prefix + hashlib.sha256(raw.encode()).hexdigest()
