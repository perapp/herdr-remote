"""Codex rollout regression tests: canonical response items, not duplicated event messages.

Synthetic fixtures follow Codex's rollout recorder and ResponseItem schemas; no private
conversation text, reasoning or credentials are copied from local sessions.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_transcript import SESSION, load_transcript, user_row

transcript = load_transcript()


def codex_item(kind, **payload):
    return {"timestamp": "2026-10-23T12:00:00.000Z", "type": "response_item",
            "payload": {"type": kind, **payload}}


def codex_message(role, text, **payload):
    return codex_item("message", role=role, content=[
        {"type": "input_text" if role == "user" else "output_text", "text": text},
    ], **payload)


def write_rollout(root, rows, dated=True):
    directory = Path(root) / "2026" / "10" / "23" if dated else Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-10-23T12-00-00-{SESSION}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class CodexParseTests(unittest.TestCase):
    def parse(self, rows):
        return transcript.parse_codex(json.dumps(row) for row in rows)[0]

    def test_a_plain_exchange_becomes_timestamped_turns(self):
        turns = self.parse([codex_message("user", "does this work?", id="u1"),
                            codex_message("assistant", "it does", id="a1")])
        self.assertEqual([(t["role"], t["text"]) for t in turns],
                         [("user", "does this work?"), ("assistant", "it does")])
        self.assertEqual(turns[0]["ts"], "2026-10-23T12:00:00.000Z")
        self.assertFalse(turns[0]["truncated"])

    def test_only_canonical_conversation_text_is_read(self):
        turns = self.parse([
            {"type": "session_meta", "payload": {"base_instructions": "private instructions"}},
            {"type": "turn_context", "payload": {"cwd": "/private"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
            codex_message("user", "hello"),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}},
            codex_message("assistant", "hi"),
            codex_message("developer", "private developer text"),
            codex_message("system", "private system text"),
            codex_item("reasoning", summary=[{"text": "private reasoning"}],
                       encrypted_content="encrypted reasoning"),
            codex_message("assistant", "private analysis", channel="analysis"),
            codex_item("unknown", text="future event"),
        ])
        self.assertEqual([(t["role"], t["text"]) for t in turns],
                         [("user", "hello"), ("assistant", "hi")])

    def test_images_are_ignored_and_text_blocks_are_joined(self):
        turns = self.parse([codex_item("message", role="user", content=[
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {"type": "input_text", "text": "at this"},
        ])])
        self.assertEqual(turns[0]["text"], "look\nat this")
        self.assertNotIn("base64", json.dumps(turns))

    def test_function_calls_and_results_share_one_tool_turn(self):
        turns = self.parse([
            codex_item("function_call", name="exec_command", call_id="call_1",
                       arguments=json.dumps({"cmd": "echo hello"})),
            codex_item("function_call_output", call_id="call_1", output="hello\nsecond line"),
        ])
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0]["role"], turns[0]["tool"]), ("tool", "exec_command"))
        self.assertIn("echo hello", turns[0]["target"])
        self.assertTrue(turns[0]["text"].endswith(" → hello"))

    def test_custom_tool_input_and_text_only_output_are_read(self):
        turns = self.parse([
            codex_item("custom_tool_call", name="exec", call_id="call_1", input="echo hello"),
            codex_item("custom_tool_call_output", call_id="call_1", output=[
                {"type": "input_text", "text": "hello\nsecond line"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]),
        ])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["tool"], "exec")
        self.assertIn("echo hello", turns[0]["target"])
        self.assertTrue(turns[0]["text"].endswith(" → hello"))
        self.assertNotIn("base64", json.dumps(turns))

    def test_legacy_json_tool_output_marks_a_failed_call(self):
        turns = self.parse([
            codex_item("function_call", name="shell", call_id="call_1",
                       arguments=json.dumps({"command": "false"})),
            codex_item("function_call_output", call_id="call_1", output=json.dumps({
                "output": "permission denied\nmore detail", "metadata": {"exit_code": 1},
            })),
        ])
        self.assertTrue(turns[0]["error"])
        self.assertEqual(turns[0]["result"], "permission denied")
        self.assertTrue(turns[0]["text"].endswith(" ! permission denied"))

    def test_replayed_item_ids_are_deduplicated(self):
        message = codex_message("user", "once", id="item_1")
        turns = self.parse([message, message, codex_message("assistant", "twice", id="item_2")])
        self.assertEqual([t["text"] for t in turns], ["once", "twice"])

    def test_idless_cursors_are_unique_and_stable_when_reading_a_tail(self):
        rows = [codex_message("user", "first"), codex_message("assistant", "second"),
                codex_item("function_call", name="shell", call_id="call_1", arguments="{}")]
        full, tail = self.parse(rows), self.parse(rows[1:])
        self.assertEqual(len({t["uuid"] for t in full}), 3)
        self.assertEqual([t["uuid"] for t in full[1:]], [t["uuid"] for t in tail])

    def test_bad_rows_and_orphan_results_do_not_break_a_page(self):
        lines = ["not json", "[]", "null", '{"type":"response_item",',
                 json.dumps(codex_item("message", role="user", content=None)),
                 json.dumps({"type": "response_item", "payload": []}),
                 json.dumps(codex_item("function_call_output", call_id="missing", output="no call")),
                 json.dumps(codex_message("user", "intact"))]
        turns, _ = transcript.parse_codex(lines)
        self.assertEqual([t["text"] for t in turns], ["intact"])

    def test_text_and_tool_targets_obey_existing_limits(self):
        turns = self.parse([
            codex_message("user", "\x1b[31m" + "x" * (transcript.TEXT_LIMIT + 20)),
            codex_item("custom_tool_call", name="exec", call_id="call_1", input="x" * 1000),
        ])
        self.assertEqual(len(turns[0]["text"]), transcript.TEXT_LIMIT)
        self.assertTrue(turns[0]["truncated"])
        self.assertNotIn("\x1b", turns[0]["text"])
        self.assertLessEqual(len(turns[1]["text"]), transcript.TOOL_TEXT_LIMIT)
        self.assertLessEqual(len(turns[1]["target"]), transcript.TOOL_TEXT_LIMIT)


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        transcript.cache_clear()
        self.addCleanup(transcript.cache_clear)
        self.session = {"agent": "codex", "kind": "id", "value": SESSION}

    def test_history_reads_dated_and_legacy_rollouts_under_configured_roots(self):
        for dated in (True, False):
            with self.subTest(dated=dated), tempfile.TemporaryDirectory() as root:
                write_rollout(root, [codex_message("user", "hello")], dated=dated)
                module = load_transcript({"HERDR_CODEX_ROOTS": root})
                body = module.history(self.session, agent="codex")
                self.assertIsNone(body["unavailable"])
                self.assertEqual(body["agent"], "codex")
                self.assertEqual([t["text"] for t in body["messages"]], ["hello"])

    def test_default_local_root_honours_codex_home(self):
        with tempfile.TemporaryDirectory() as home:
            write_rollout(Path(home) / "sessions", [codex_message("user", "custom home")])
            module = load_transcript({"CODEX_HOME": home, "HERDR_CODEX_ROOTS": ""})
            self.assertIsNone(module.history(self.session)["unavailable"])

    def test_roots_are_searched_in_order(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            path = write_rollout(second, [codex_message("user", "second root")])
            self.assertIsNone(transcript.locate_codex(SESSION, roots=[first]))
            # Compared through realpath: locate_codex resolves the file it found, and on macOS the
            # temp dir sits under /var, which is a symlink to /private/var.
            self.assertEqual(transcript.locate_codex(SESSION, roots=[first, second]),
                             os.path.realpath(path))

    def test_symlinks_cannot_escape_a_codex_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            path = write_rollout(outside, [codex_message("user", "private")], dated=False)
            try:
                (Path(root) / path.name).symlink_to(path)
            except OSError:
                self.skipTest("symlinks unavailable")
            self.assertIsNone(transcript.locate_codex(SESSION, roots=[root]))

    def test_a_missing_rollout_is_no_log_not_unsupported(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(transcript, "CODEX_ROOTS", [root]):
            self.assertEqual(transcript.history(self.session)["unavailable"], "no-log")

    def test_invalid_session_refs_never_touch_disk_or_ssh(self):
        for kind, value in (("path", "/tmp/private.jsonl"), ("id", "../private"),
                            ("id", SESSION + "; echo unsafe"), ("id", "")):
            with self.subTest(kind=kind, value=value), \
                 mock.patch.object(transcript, "_load_local") as local, \
                 mock.patch.object(transcript, "_load_remote") as remote:
                body = transcript.history({"agent": "codex", "kind": kind, "value": value})
                self.assertEqual(body["unavailable"], "no-session")
                local.assert_not_called()
                remote.assert_not_called()

    def test_existing_pagination_tools_filter_and_cache_work_for_codex(self):
        rows = [codex_message("user", "first"),
                codex_item("function_call", name="shell", call_id="call_1", arguments="{}"),
                codex_message("assistant", "last")]
        with tempfile.TemporaryDirectory() as root:
            write_rollout(root, rows)
            with mock.patch.object(transcript, "CODEX_ROOTS", [root]), \
                 mock.patch.object(transcript, "read_local", wraps=transcript.read_local) as reader:
                newest = transcript.history(self.session, limit=1)
                older = transcript.history(self.session, before=newest["messages"][0]["uuid"])
                tools = transcript.history(self.session, include_tools=True)
        self.assertEqual([m["text"] for m in newest["messages"]], ["last"])
        self.assertTrue(newest["has_more"])
        self.assertEqual(newest["total"], 2)
        self.assertEqual([m["text"] for m in older["messages"]], ["first"])
        self.assertFalse(older["has_more"])
        self.assertEqual([m["role"] for m in tools["messages"]], ["user", "tool", "assistant"])
        self.assertEqual(reader.call_count, 1)

    def test_a_grown_rollout_is_reparsed_without_moving_existing_cursors(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_rollout(root, [codex_message("user", "first")])
            with mock.patch.object(transcript, "CODEX_ROOTS", [root]):
                first = transcript.history(self.session)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(codex_message("assistant", "last")) + "\n")
                second = transcript.history(self.session)
        self.assertEqual(second["messages"][0]["uuid"], first["messages"][0]["uuid"])
        self.assertEqual(second["total"], 2)

    def test_remote_history_uses_codex_roots_and_reuses_the_size_cache(self):
        row = json.dumps(codex_message("user", "remote hello")) + "\n"
        frame = f"SIZE {len(row.encode())}\n".encode() + row.encode()
        runner = mock.Mock(side_effect=[(0, frame), (0, b"CACHED\n")])
        with mock.patch.object(transcript, "REMOTE_CODEX_ROOTS", ["$HOME/custom-codex/sessions"]):
            first = transcript.history(self.session, remote="build-box", remote_runner=runner)
            second = transcript.history(self.session, remote="build-box", remote_runner=runner)
        self.assertIsNone(first["unavailable"])
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(first["messages"][0]["text"], "remote hello")
        script = runner.call_args_list[0].args[1]
        self.assertIn(f"$HOME/custom-codex/sessions/*/*/*/rollout-*-{SESSION}.jsonl", script)
        self.assertNotIn(".claude", script)
        self.assertIn("CACHED", runner.call_args_list[1].args[1])

    def test_remote_codex_and_claude_with_the_same_uuid_do_not_share_a_parse(self):
        def runner(remote, script, ssh_args=()):
            if "CACHED" in script:
                return 0, b"CACHED\n"
            item = codex_message("user", "codex") if "rollout-" in script else user_row("claude")
            blob = (json.dumps(item) + "\n").encode()
            return 0, f"SIZE {len(blob)}\n".encode() + blob

        codex = transcript.history(self.session, remote="box", remote_runner=runner)
        claude = transcript.history({"kind": "id", "value": SESSION}, agent="claude",
                                    remote="box", remote_runner=runner)
        self.assertEqual([m["text"] for m in codex["messages"]], ["codex"])
        self.assertEqual([m["text"] for m in claude["messages"]], ["claude"])

    def test_remote_tail_keeps_cursors_stable_and_reports_truncation(self):
        rows = [codex_message("user", "first"), codex_message("assistant", "last")]
        full = transcript.parse_codex(json.dumps(row) for row in rows)[0]
        tail = ("torn row\n" + json.dumps(rows[-1]) + "\n").encode()
        frame = b"SIZE 1000000\n" + tail
        body = transcript.history(self.session, remote="box",
                                  remote_runner=lambda *args: (0, frame))
        self.assertTrue(body["file_truncated"])
        self.assertEqual(body["messages"], full[-1:])

    def test_remote_probe_rejects_unsafe_refs_and_roots(self):
        for value, roots in ((SESSION + "; echo unsafe", ["$HOME/.codex/sessions"]),
                             (SESSION, ["$HOME/x; echo unsafe"])):
            with self.subTest(value=value, roots=roots), self.assertRaises(ValueError):
                transcript.remote_probe_script(value, roots, 0, 4096, harness="codex")

    def test_harnesses_without_remote_locators_do_not_probe_another_harness_tree(self):
        for agent, kind, value in (("kimi", "id", f"session_{SESSION}"),):
            with self.subTest(agent=agent):
                runner = mock.Mock()
                body = transcript.history({"agent": agent, "kind": kind, "value": value},
                                          remote="box", remote_runner=runner)
                self.assertEqual(body["unavailable"], "unsupported")
                runner.assert_not_called()

    @unittest.skipIf(os.name == "nt", "remote probes require a POSIX shell")
    def test_the_generated_remote_probe_can_read_a_real_dated_rollout(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_rollout(root, [codex_message("user", "from the shell")])
            script = transcript.remote_probe_script(SESSION, [root], 0, 4096, harness="codex")
            result = subprocess.run(["sh", "-c", script], capture_output=True, check=True)
            header, _, body = result.stdout.partition(b"\n")
            self.assertEqual(header, f"SIZE {path.stat().st_size}".encode())
            self.assertEqual(body, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
