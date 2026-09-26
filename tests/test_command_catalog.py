import asyncio
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from .test_herdr_relay import _FakeWebSocket, loaded_relay
except ImportError:
    from test_herdr_relay import _FakeWebSocket, loaded_relay

SPEC = importlib.util.spec_from_file_location(
    "command_catalog_test", Path(__file__).resolve().parents[1] / "relay" / "command_catalog.py")
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.session = self.root / "session.jsonl"
        self.session.write_text("{}\n")
        self.sidecar = Path(str(self.session) + catalog.SUFFIX)
        self.ref = {"agent": "pi", "kind": "path", "value": str(self.session)}
        self.data = {"schemaVersion": 1, "sessionFile": str(self.session), "pid": 123,
                     "updatedAt": 1000000, "commands": [
                         {"name": "dynamic", "description": "Real runtime command", "source": "extension"},
                         {"name": "skill:pdf", "description": "PDF", "source": "skill"},
                         {"name": "review", "description": "Review", "source": "template"}]}
        roots = mock.patch.object(catalog.transcript, "PI_ROOTS", [str(self.root)])
        roots.start()
        self.addCleanup(roots.stop)
        enabled = mock.patch.object(catalog, "ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def read(self):
        return catalog.commands(self.ref, agent="pi", now=1000)

    def save(self):
        self.sidecar.write_text(json.dumps(self.data))

    def test_valid_catalog_and_dedup(self):
        self.data["commands"].append(self.data["commands"][0])
        self.save()
        result = self.read()
        self.assertNotIn("unavailable", result)
        self.assertEqual([c["cmd"] for c in result["commands"]], ["/dynamic", "/skill:pdf", "/review"])
        self.assertTrue(all(c["common"] for c in result["commands"]))
        self.assertNotIn("sessionFile", json.dumps(result))

    def test_allocated_session_has_metadata_without_creating_transcript(self):
        self.session.unlink()
        self.assertEqual(self.read()["unavailable"], "no-catalog")
        self.save()
        result = self.read()
        self.assertNotIn("unavailable", result)
        self.assertEqual(result["commands"][0]["desc"], "Real runtime command")
        self.assertFalse(self.session.exists())
        self.assertEqual(list(self.root.iterdir()), [self.sidecar])

    def test_allocated_path_validation(self):
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside)
            parent_link = self.root / "escape"
            parent_link.symlink_to(foreign, target_is_directory=True)
            dangling = self.root / "dangling.jsonl"
            dangling.symlink_to(foreign / "absent.jsonl")
            directory = self.root / "directory.jsonl"
            directory.mkdir()
            invalid = [str(foreign / "absent.jsonl"), str(parent_link / "absent.jsonl"),
                       str(dangling), str(directory), str(self.root / "missing" / "new.jsonl"),
                       str(self.root / "wrong.txt"), str(self.root) + "/./session.jsonl",
                       str(self.root) + "/../" + self.root.name + "/session.jsonl",
                       str(self.root) + "/session.jsonl/", str(self.root / "bad\x00.jsonl")]
            if hasattr(os, "mkfifo"):
                fifo = self.root / "fifo.jsonl"
                os.mkfifo(fifo)
                invalid.append(str(fifo))
            with mock.patch.object(catalog, "_read") as reader:
                for value in invalid:
                    with self.subTest(value=value):
                        self.ref["value"] = value
                        self.assertEqual(self.read()["unavailable"], "no-session")
                reader.assert_not_called()
            self.assertFalse((self.root / "missing").exists())
            self.assertEqual(list(foreign.iterdir()), [])

    def test_canonical_parent_and_existing_file_links(self):
        nested = self.root / "nested"
        nested.mkdir()
        parent_link = self.root / "alias"
        parent_link.symlink_to(nested, target_is_directory=True)
        allocated = nested / "fresh.jsonl"
        self.data["sessionFile"] = str(allocated)
        Path(str(allocated) + catalog.SUFFIX).write_text(json.dumps(self.data))
        self.ref["value"] = str(parent_link / allocated.name)
        self.assertNotIn("unavailable", self.read())
        self.assertFalse(allocated.exists())
        link = self.root / "file-link.jsonl"
        link.symlink_to(self.session)
        self.ref["value"] = str(link)
        self.data["sessionFile"] = str(self.session)
        self.save()
        self.assertNotIn("unavailable", self.read())
        self.assertEqual(self.session.read_text(), "{}\n")

    def test_unavailable_states_without_opening(self):
        with mock.patch.object(catalog, "_read") as reader:
            self.assertEqual(catalog.commands(None)["unavailable"], "no-session")
            self.assertEqual(catalog.commands(self.ref, agent="claude")["unavailable"], "unsupported")
            self.assertEqual(catalog.commands(self.ref, remote="host")["unavailable"], "remote")
            with mock.patch.object(catalog, "ENABLED", False):
                self.assertEqual(self.read()["unavailable"], "disabled")
            reader.assert_not_called()
        self.assertEqual(self.read()["unavailable"], "no-catalog")

    def test_containment_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside) / "session.jsonl"
            foreign.write_text("{}")
            link = self.root / "escape.jsonl"
            link.symlink_to(foreign)
            for value in [str(foreign), str(link), "relative.jsonl", "\0", ""]:
                self.ref["value"] = value
                self.assertEqual(self.read()["unavailable"], "no-session")

    def test_stale_future_and_wrong_session(self):
        for timestamp in [0, 924999, 1006000]:
            self.data["updatedAt"] = timestamp
            self.save()
            self.assertEqual(self.read()["unavailable"], "stale")
        self.data["updatedAt"] = 1000000
        self.data["sessionFile"] = str(self.session) + "other"
        self.save()
        self.assertEqual(self.read()["unavailable"], "error")

    def test_header_validation(self):
        for key, value, reason in [("schemaVersion", 2, "unsupported"),
                                   ("schemaVersion", True, "unsupported"),
                                   ("pid", True, "error"), ("pid", -1, "error"),
                                   ("updatedAt", True, "error"), ("updatedAt", float("nan"), "error"),
                                   ("commands", {}, "error")]:
            with self.subTest(key=key, value=value):
                original = self.data[key]
                self.data[key] = value
                self.save()
                self.assertEqual(self.read()["unavailable"], reason)
                self.data[key] = original

    def test_entry_validation(self):
        entry = self.data["commands"][0]
        for key, value in [("name", "bad\n"), ("name", "/slash"), ("name", "x" * 129),
                           ("name", ""), ("source", []), ("source", "prompt"),
                           ("source", "builtin"), ("description", "x" * 201)]:
            with self.subTest(key=key, value=value):
                original = entry[key]
                entry[key] = value
                self.save()
                self.assertEqual(self.read()["unavailable"], "error")
                entry[key] = original

    def test_limits_and_nonregular_files(self):
        self.sidecar.write_bytes(b" " * (catalog.MAX_BYTES + 1))
        self.assertEqual(self.read()["unavailable"], "error")
        self.data["commands"] = [self.data["commands"][0]] * 501
        self.save()
        self.assertEqual(self.read()["unavailable"], "error")
        self.sidecar.unlink()
        self.sidecar.symlink_to(self.session)
        self.assertEqual(self.read()["unavailable"], "error")
        self.sidecar.unlink()
        self.sidecar.mkdir()
        self.assertEqual(self.read()["unavailable"], "error")
        self.sidecar.rmdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.sidecar)
            self.assertEqual(self.read()["unavailable"], "error")

    def test_malformed_and_unreadable_are_safe(self):
        for blob in [b"{", b"[]", b"\xff", b"[" * 2000]:
            self.sidecar.write_bytes(blob)
            self.assertEqual(self.read()["unavailable"], "error")
        with mock.patch.object(catalog, "_read", side_effect=PermissionError):
            self.assertEqual(self.read()["unavailable"], "error")


class CommandHandlerTests(unittest.TestCase):
    def request(self, relay, **extra):
        message = {"type": "get_commands", "pane_id": "pane", "request_id": "commands-1", **extra}
        ws = _FakeWebSocket([json.dumps(message)])
        with mock.patch.object(relay, "send_current_snapshot", new=mock.AsyncMock()):
            asyncio.run(relay.handle_client(ws))
        return json.loads(ws.sent[0])

    def test_server_ref_and_off_loop_read(self):
        with loaded_relay() as relay:
            relay.known_panes.add("pane")
            relay.agent_cache["pane"] = {"agent": "pi"}
            relay.pane_session_map["pane"] = {"kind": "path", "value": "/server/session"}
            thread = threading.get_ident()

            def reader(session, **kwargs):
                self.assertNotEqual(thread, threading.get_ident())
                self.assertEqual(session["value"], "/server/session")
                self.assertEqual(kwargs, {"remote": None, "agent": "pi"})
                return {"commands": []}

            with mock.patch.object(relay.command_catalog, "commands", side_effect=reader):
                reply = self.request(relay, sessionFile="/client/path", session={"value": "/client/path"})
            self.assertEqual(reply, {"type": "commands", "pane_id": "pane",
                                     "request_id": "commands-1", "commands": []})

    def test_fresh_known_pane_returns_real_metadata(self):
        with loaded_relay() as relay, tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "fresh.jsonl"
            sidecar = Path(str(session) + catalog.SUFFIX)
            sidecar.write_text(json.dumps({
                "schemaVersion": 1, "sessionFile": str(session), "pid": 123,
                "updatedAt": catalog.time.time() * 1000,
                "commands": [{"name": "write-mode", "description": "Toggle write mode",
                              "source": "extension"}]}))
            relay.known_panes.add("pane")
            relay.agent_cache["pane"] = {"agent": "pi"}
            relay.pane_session_map["pane"] = {"kind": "path", "value": str(session)}
            with mock.patch.object(relay.command_catalog.transcript, "PI_ROOTS", [directory]), \
                    mock.patch.object(relay.command_catalog, "ENABLED", True):
                reply = self.request(relay)
            self.assertEqual(reply["commands"], [{"cmd": "/write-mode", "desc": "Toggle write mode",
                                                  "source": "extension", "common": True}])
            self.assertNotIn("unavailable", reply)
            self.assertFalse(session.exists())
            self.assertEqual(list(Path(directory).iterdir()), [sidecar])

    def test_correlation_and_scope_on_refusals(self):
        with loaded_relay() as relay:
            relay.known_panes.add("pane")
            for fields in [{"pane_id": "missing"}, {"pane_id": []}, {"pane_id": None},
                           {"request_id": ""}, {"request_id": "x" * 129},
                           {"request_id": {}}, {"request_id": 42}]:
                with self.subTest(fields=fields), mock.patch.object(relay.command_catalog, "commands") as reader:
                    result = self.request(relay, **fields)
                    self.assertEqual(result["type"], "error")
                    self.assertEqual(result["scope"], "get_commands")
                    reader.assert_not_called()
                    if "request_id" in fields:
                        self.assertNotIn("request_id", result)
                    else:
                        self.assertEqual(result["request_id"], "commands-1")
            with mock.patch.object(relay.command_catalog, "commands", side_effect=RuntimeError("secret")):
                result = self.request(relay)
                self.assertEqual(result["scope"], "get_commands")
                self.assertEqual(result["request_id"], "commands-1")
                self.assertNotIn("secret", json.dumps(result))

    def test_normal_unavailable_reply(self):
        with loaded_relay() as relay:
            relay.known_panes.add("pane")
            result = self.request(relay)
            self.assertEqual(result["unavailable"], "no-session")
            self.assertEqual(result["commands"], [])
