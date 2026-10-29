"""Execute the generated SSH reader locally; never contact a host or real Pi tree."""
import importlib.util
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "remote_pi_catalog", Path(__file__).resolve().parents[1] / "relay" / "command_catalog.py")
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)
transcript = catalog.transcript


class RemotePiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "sessions"
        self.root.mkdir()
        self.outside = Path(temp.name) / "outside"
        self.outside.mkdir()
        self.path = self.root / "session.jsonl"
        self.ref = {"agent": "pi", "kind": "path", "value": str(self.path)}
        self.calls = []
        for obj, name, value in ((transcript, "REMOTE_PI_ROOTS", [str(self.root)]),
                                 (transcript, "ENABLED", True), (catalog, "ENABLED", True)):
            patch = mock.patch.object(obj, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        transcript.cache_clear()
        self.addCleanup(transcript.cache_clear)

    def runner(self, remote, script, ssh_args):
        self.calls.append((remote, script, ssh_args))
        # No shell: test exactly the quoted argv that the remote shell would execute.
        proc = subprocess.run(shlex.split(script), capture_output=True, timeout=5, check=False)
        self.assertEqual(proc.stderr, b"")
        return proc.returncode, proc.stdout

    def history(self, **kwargs):
        return transcript.history(self.ref, remote="machine:one", remote_runner=self.runner,
                                  ssh_args=("-T",), **kwargs)

    def commands(self, **kwargs):
        return catalog.commands(self.ref, remote="machine:one", ssh_runner=self.runner,
                                ssh_args=("-T",), now=1000, **kwargs)

    def save_turn(self, ident="one", text="Hello"):
        with self.path.open("a") as handle:
            handle.write(json.dumps({"type": "message", "id": ident,
                                     "message": {"role": "user", "content": text}}) + "\n")

    def save_catalog(self, **changes):
        data = {"schemaVersion": 1, "sessionFile": str(self.path), "pid": 123,
                "updatedAt": 1000000, "commands": [
                    {"name": "git-mode", "description": "Toggle", "source": "extension"}]}
        data.update(changes)
        Path(str(self.path) + catalog.SUFFIX).write_text(json.dumps(data))

    def test_history_cache_append_pagination_and_logical_source(self):
        self.save_turn()
        self.save_turn("two", "Second")
        self.assertEqual(self.history(limit=1)["messages"][0]["text"], "Second")
        self.assertEqual(self.history(before="two")["messages"][0]["text"], "Hello")
        self.assertIn('"expected":', self.calls[-1][1])
        self.assertIn(("machine:one", "pi", str(self.path)), transcript._cache)
        self.assertEqual(self.calls[-1][0], "machine:one")
        self.assertEqual(self.calls[-1][2], ("-T",))
        self.save_turn("three", "Appended")
        self.assertEqual(self.history()["total"], 3)

    def test_remote_paths_never_use_local_locator(self):
        self.save_turn()
        self.save_catalog()
        with mock.patch.object(transcript, "_load_local", side_effect=AssertionError), \
                mock.patch.object(catalog, "_allocated_session", side_effect=AssertionError), \
                mock.patch.object(catalog, "_read", side_effect=AssertionError):
            self.assertIsNone(self.history()["unavailable"])
            self.assertEqual(self.commands()["commands"][0]["cmd"], "/git-mode")

    def test_allocated_catalog_without_transcript(self):
        self.save_catalog()
        self.assertEqual(self.commands()["commands"][0]["cmd"], "/git-mode")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.history()["unavailable"], "no-log")
        self.assertEqual(self.calls[-1][0], "machine:one")

    def test_invalid_paths_and_special_files(self):
        self.save_turn()
        outside = self.outside / "foreign.jsonl"
        outside.write_text("{}\n")
        escape = self.root / "escape.jsonl"
        escape.symlink_to(outside)
        parent = self.root / "parent"
        parent.symlink_to(self.outside, target_is_directory=True)
        dangling = self.root / "dangling.jsonl"
        dangling.symlink_to(self.outside / "missing.jsonl")
        directory = self.root / "directory.jsonl"
        directory.mkdir()
        fifo = self.root / "fifo.jsonl"
        os.mkfifo(fifo)
        device = self.root / "device.jsonl"
        device.symlink_to("/dev/null")
        invalid = [str(outside), str(escape), str(parent / "fresh.jsonl"), str(dangling),
                   str(directory), str(fifo), str(device), str(self.root / "bad.txt"),
                   str(self.root / "missing" / "fresh.jsonl"),
                   str(self.root) + "/../sessions/session.jsonl",
                   str(self.root) + "/./session.jsonl", str(self.root / "bad\x00.jsonl")]
        for value in invalid:
            with self.subTest(value=value):
                self.ref["value"] = value
                self.assertEqual(self.history()["unavailable"], "no-session")
                self.assertEqual(self.commands()["unavailable"], "no-session")

    def test_sidecar_must_be_regular_bounded_and_not_link(self):
        sidecar = Path(str(self.path) + catalog.SUFFIX)
        self.assertEqual(self.commands()["unavailable"], "no-catalog")
        outside = self.outside / "catalog.json"
        outside.write_text("{}")
        sidecar.symlink_to(outside)
        self.assertEqual(self.commands()["unavailable"], "error")
        sidecar.unlink()
        os.mkfifo(sidecar)
        self.assertEqual(self.commands()["unavailable"], "error")
        sidecar.unlink()
        sidecar.mkdir()
        self.assertEqual(self.commands()["unavailable"], "error")
        sidecar.rmdir()
        sidecar.write_bytes(b" " * (catalog.MAX_BYTES + 1))
        self.assertEqual(self.commands()["unavailable"], "error")

    def test_catalog_validation_is_shared_with_local(self):
        cases = [({"schemaVersion": 2}, "unsupported"), ({"schemaVersion": True}, "unsupported"),
                 ({"sessionFile": "/wrong.jsonl"}, "error"), ({"pid": 0}, "error"),
                 ({"updatedAt": 0}, "stale"), ({"updatedAt": 1006000}, "stale"),
                 ({"updatedAt": float("nan")}, "error"),
                 ({"commands": [{"name": "bad/name", "description": "", "source": "skill"}]}, "error")]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                self.save_catalog(**changes)
                self.assertEqual(self.commands()["unavailable"], reason)

    def test_canonical_session_link_and_parent_alias(self):
        self.save_turn()
        self.save_catalog()
        alias = self.root / "alias.jsonl"
        alias.symlink_to(self.path)
        self.ref["value"] = str(alias)
        self.assertIsNone(self.history()["unavailable"])
        self.assertEqual(self.commands()["commands"][0]["cmd"], "/git-mode")
        parent = self.root / "parent"
        parent.symlink_to(self.root, target_is_directory=True)
        self.ref["value"] = str(parent / self.path.name)
        self.assertEqual(self.commands()["commands"][0]["cmd"], "/git-mode")

    def test_default_remote_root_expands_on_remote_host(self):
        home = self.outside
        default = home / ".pi" / "agent" / "sessions"
        default.mkdir(parents=True)
        allocated = default / "fresh.jsonl"
        self.ref["value"] = str(allocated)
        with mock.patch.object(transcript, "REMOTE_PI_ROOTS", ["~/.pi/agent/sessions"]), \
                mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(self.commands()["unavailable"], "no-catalog")
        self.assertFalse(allocated.exists())

    def test_shell_metacharacters_are_data(self):
        self.path = self.root / "quote' ; $(touch nope).jsonl"
        self.ref["value"] = str(self.path)
        self.save_turn(text="Safe")
        self.assertEqual(self.history()["messages"][0]["text"], "Safe")
        script = self.calls[-1][1]
        self.assertEqual(shlex.split(script)[0:2], ["python3", "-c"])
        self.assertEqual(len(shlex.split(script)), 4)

    def test_truncated_tail_drops_partial_line(self):
        self.save_turn("large", "x" * 1000)
        self.save_turn("last", "Visible")
        with mock.patch.object(transcript, "REMOTE_TAIL_BYTES", 200):
            # _load_remote passes the current configured bound explicitly.
            result = self.history()
        self.assertTrue(result["file_truncated"])
        self.assertEqual([turn["uuid"] for turn in result["messages"]], ["last"])
        header, _, body = self.runner("machine:one", transcript.remote_pi_script(
            str(self.path), tail_bytes=200), ())[1].partition(b"\n")
        self.assertEqual(int(header.split()[1]), self.path.stat().st_size)
        self.assertEqual(len(body), 200)

    def test_cache_eviction_retries_without_shortcut(self):
        self.save_turn()
        self.history()
        calls = []
        def runner(remote, script, args):
            if not calls:
                calls.append(script)
                transcript.cache_clear()
                return 0, b"CACHED\n"
            calls.append(script)
            return self.runner(remote, script, args)
        result = transcript.history(self.ref, remote="machine:one", remote_runner=runner)
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(calls), 2)

    def test_transport_timeout_is_unavailable(self):
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired("ssh", 25))
        self.assertEqual(catalog.commands(self.ref, remote="machine:one",
                         ssh_runner=runner)["unavailable"], "error")
        self.assertEqual(transcript.history(self.ref, remote="machine:one",
                         remote_runner=runner)["unavailable"], "error")

    def test_missing_python_and_bad_remote_frames(self):
        for code, blob, reason in [(127, b"", "python3-unavailable"),
                                   (255, b"", "remote-error"),
                                   (0, b"ERROR\n", "error"), (0, b"garbage\n", "error")]:
            with self.subTest(code=code, blob=blob):
                runner = lambda *args, code=code, blob=blob: (code, blob)
                self.assertEqual(transcript.history(self.ref, remote="machine:one",
                                 remote_runner=runner)["unavailable"], reason)
                self.assertEqual(catalog.commands(self.ref, remote="machine:one",
                                 ssh_runner=runner)["unavailable"], reason)
        self.assertEqual(catalog.commands(self.ref, remote="machine:one")["unavailable"], "remote")


if __name__ == "__main__":
    unittest.main()
