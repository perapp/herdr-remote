"""Machine routing contract tests: all native/SSH subprocesses are mocked."""
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_herdr_relay import loaded_relay
from test_machine_sources import profile


class MachineRelayTests(unittest.TestCase):
    def configured(self, relay):
        relay.MACHINE_REGISTRY.setting = "auto"
        relay.MACHINE_REGISTRY.refresh(lambda: [profile()], force=True)
        relay.MACHINE_REGISTRY._checked = float("inf")
        return "machine:one"

    def test_fixed_session_native_routing_and_literal_reply(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            relay.ACTIVE_SESSIONS[source] = "wrong"
            self.assertEqual(relay.active_session_for(source), "saved")
            wire = relay.bind_source_id(source, "w1:p1")
            proc = subprocess.CompletedProcess([], 0, "ok", "")
            with mock.patch.dict(os.environ, {"HERDR_SESSION": "inherited", "HERDR_SOCKET_PATH": "/bad"}), mock.patch.object(relay, "validate_machine_profile", return_value=relay.MACHINE_REGISTRY.machines[source]), mock.patch.object(relay.subprocess, "run", return_value=proc) as run:
                relay._invoke_herdr("agent", "prompt", wire, wire, remote=source)
            args, kwargs = run.call_args
            self.assertEqual(args[0], [relay.HERDR, "--machine", "one", "agent", "prompt", "w1:p1", wire])
            self.assertNotIn("HERDR_SESSION", kwargs["env"])
            self.assertNotIn("HERDR_SOCKET_PATH", kwargs["env"])
            self.assertNotIn("--session", args[0])
            with mock.patch.object(relay, "run_herdr") as native:
                self.assertEqual(relay.get_sessions(source), [{"name": "saved", "running": True}])
                native.assert_not_called()
            self.assertEqual(relay.apply_session_switch(source, None, names=set()),
                             (False, "registered machines use their configured session", False))

    def test_colliding_raw_ids_keep_records_refs_and_layout_distinct(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            relay.MACHINE_REGISTRY.machines["machine:two"] = {**profile("two"), "kind": "machine"}
            pane = {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "pi", "agent_status": "working",
                    "agent_session": {"kind": "path", "value": "/home/me/session.jsonl", "agent": "pi"}}
            def native(*args, remote=None):
                if args[:2] == ("pane", "list"):
                    return json.dumps({"result": {"panes": [pane]}})
                if args[:2] == ("pane", "layout"):
                    return json.dumps({"result": {"layout": {"focused_pane_id": "w1:p1", "panes": [{"pane_id": "w1:p1", "rect": {}}]}}})
                return '{"result": {}}'
            with mock.patch.object(relay, "run_herdr", side_effect=native):
                first, _ = relay.list_panes_from_host(source)
                second, _ = relay.list_panes_from_host("machine:two")
                local, _ = relay.list_panes_from_host()
                relay.update_pane_maps(first + second + local)
                wire = first[0]["pane_id"]
                layout = relay.pane_layout(wire, source)
            self.assertEqual(len(relay.agent_cache), 3)
            self.assertEqual(len(relay.pane_session_map), 3)
            self.assertEqual(local[0]["pane_id"], "w1:p1")
            self.assertEqual(first[0]["source_label"], "One")
            self.assertEqual(layout["focused_pane_id"], wire)
            self.assertEqual(layout["panes"][0]["pane_id"], wire)
            self.assertEqual(relay.translate_source_args(("pane", "read", wire), source)[2], "w1:p1")
            with self.assertRaises(ValueError):
                relay.translate_source_args(("pane", "read", wire), "machine:two")

    def test_failed_pane_read_preserves_last_good(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            pane = {"pane_id": "w1:p1", "agent": "pi", "agent_status": "working"}
            with mock.patch.object(relay, "run_herdr", side_effect=lambda *a, **k: json.dumps({"result": {"panes": [pane]}}) if a[:2] == ("pane", "list") else '{}'):
                good = relay.list_panes_from_host(source)
            with mock.patch.object(relay, "run_herdr", return_value="offline"):
                self.assertEqual(relay.list_panes_from_host(source), good)
            self.assertFalse(relay.pane_list_ok[source])

    def test_registry_changes_invalidate_old_routing(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            wire = relay.bind_source_id(source, "w1:p1")
            relay.known_panes.add(wire)
            relay.pane_remote_map[wire] = source
            relay.MACHINE_REGISTRY._checked = None
            proc = subprocess.CompletedProcess([], 0, json.dumps([profile(session="new")]), "")
            with mock.patch.object(relay.subprocess, "run", return_value=proc):
                relay.refresh_sources()
            self.assertNotIn(wire, relay.known_panes)
            self.assertNotIn(wire, relay.source_bindings)
            with self.assertRaises(ValueError):
                relay._invoke_herdr("pane", "read", wire, remote=source)
            self.assertNotEqual(wire, relay.bind_source_id(source, "w1:p1"))

    def test_transport_resolves_physical_target_only(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            proc = subprocess.CompletedProcess([], 0, b"ok", b"")
            with mock.patch.object(relay.subprocess, "run", return_value=proc) as run:
                relay.transcript_ssh(source, "true", ["-x"])
                self.assertEqual(run.call_args.args[0][2], "user@host")
                relay.upload_ssh(source, "true", os.devnull)
                self.assertIn("user@host", run.call_args.args[0])
                self.assertNotIn(source, run.call_args.args[0])

    def test_file_transport_captures_target_and_scopes_cache_by_revision(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            cache_key, runner = relay.source_file_transport(source)
            relay.MACHINE_REGISTRY.machines[source]["target"] = "new-host"
            new_key, _ = relay.source_file_transport(source)
            self.assertNotEqual(cache_key, new_key)
            with mock.patch.object(relay, "transcript_ssh", return_value=(0, b"ok")) as transport:
                self.assertEqual(runner(cache_key, "true", ("-T",)), (0, b"ok"))
            transport.assert_called_once_with("user@host", "true", ("-T",))

    def test_sessions_metadata_without_native_machine_session_list(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            with mock.patch.object(relay, "run_herdr", return_value="name state\ndefault running") as run:
                message = relay.sessions_message()
            machine = next(s for s in message["sources"] if s["host"] == source)
            self.assertFalse(machine["switchable"])
            self.assertEqual(machine["kind"], "machine")
            self.assertEqual(machine["label"], "One")
            self.assertEqual(machine["active"], "saved")
            self.assertTrue(all(call.kwargs.get("remote") != source for call in run.call_args_list))

    def test_upload_binding_and_cleanup_cannot_follow_new_target(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            wire = relay.bind_source_id(source, "w1:p1")
            relay.known_panes.add(wire)
            relay.pane_remote_map[wire] = source
            binding = relay.upload_binding(wire)
            relay.MACHINE_REGISTRY.machines[source]["target"] = "different-host"
            with mock.patch.object(relay, "upload_ssh") as transport:
                with self.assertRaises(ValueError):
                    relay.store_upload("/nonexistent", wire, "x.txt", binding)
                relay.upload_ledger.append({"host": source, "physical_target": "old-host"})
                relay._sweep_host(source, "saved", {})
                transport.assert_not_called()

    def test_shared_physical_host_uses_source_scoped_upload_folders(self):
        with loaded_relay() as relay, tempfile.TemporaryDirectory() as directory:
            source = self.configured(relay)
            relay.MACHINE_REGISTRY.machines["machine:two"] = {**profile("two"), "kind": "machine"}
            with mock.patch.object(relay, "upload_ssh", return_value=(0, b"/home/me/upload.txt\n")):
                for index, key in enumerate((source, "machine:two")):
                    wire = relay.bind_source_id(key, "w1:p1")
                    relay.pane_remote_map[wire] = key
                    relay.pane_refs_by_host[key] = ("saved", {wire: "path:/same/session.jsonl"})
                    spool = Path(directory) / str(index)
                    spool.write_bytes(b"file")
                    relay.store_upload(str(spool), wire, "file.txt")
            self.assertEqual(len(relay.upload_ledger), 2)
            self.assertNotEqual(relay.upload_ledger[0]["folder"], relay.upload_ledger[1]["folder"])
            self.assertEqual({entry["physical_target"] for entry in relay.upload_ledger}, {"user@host"})

    def test_disabled_machine_immediately_loses_binding_after_refresh(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            wire = relay.bind_source_id(source, "w1:p1")
            relay.known_panes.add(wire)
            relay.MACHINE_REGISTRY._checked = None
            proc = subprocess.CompletedProcess([], 0, json.dumps([profile(enabled=False)]), "")
            with mock.patch.object(relay.subprocess, "run", return_value=proc):
                relay.refresh_sources()
            self.assertNotIn(source, relay.MACHINE_REGISTRY.machines)
            self.assertNotIn(wire, relay.known_panes)
            with self.assertRaises(ValueError):
                relay.active_session_for(source)

    def test_generation_guard_precedes_snapshot_and_map_update(self):
        with loaded_relay() as relay:
            relay._upload_spool_cleared = True
            def changed():
                relay.POLL_GENERATION += 1
                return [], []
            with mock.patch.object(relay, "refresh_sources"), mock.patch.object(relay, "get_all_panes", side_effect=changed), mock.patch.object(relay, "update_pane_maps") as update, mock.patch.object(relay, "broadcast", new_callable=mock.AsyncMock) as broadcast:
                asyncio.run(relay._poll_once())
            update.assert_not_called()
            broadcast.assert_not_called()

    def test_dispatch_validates_authoritative_profile_inside_discovery_ttl(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            wire = relay.bind_source_id(source, "w1:p1")
            for changes in ({"session": "different"}, {"target": "different"}, {"enabled": False}):
                with self.subTest(changes=changes):
                    changed = subprocess.CompletedProcess([], 0, json.dumps([profile(**changes)]), "")
                    with mock.patch.object(relay.subprocess, "run", return_value=changed) as run, self.assertRaises(ValueError):
                        relay._invoke_herdr("pane", "send-text", wire, "do not send", remote=source)
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.args[0][1:], ["machine", "list", "--json"])

    def test_refresh_during_dispatch_cannot_authorize_an_old_native_id(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            wire = relay.bind_source_id(source, "w1:p1")
            def changed(_source):
                relay.MACHINE_REGISTRY.machines[source]["session"] = "new"
                return dict(relay.MACHINE_REGISTRY.machines[source])
            with mock.patch.object(relay, "validate_machine_profile", side_effect=changed), mock.patch.object(relay.subprocess, "run") as run, self.assertRaises(ValueError):
                relay._invoke_herdr("pane", "send-keys", wire, "Enter", remote=source)
            run.assert_not_called()

    def test_validation_failure_never_uses_last_good_profile(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            wire = relay.bind_source_id(source, "w1:p1")
            failed = subprocess.CompletedProcess([], 1, "", "unavailable")
            with mock.patch.object(relay.subprocess, "run", return_value=failed) as run, self.assertRaises(ValueError):
                relay._invoke_herdr("pane", "send-keys", wire, "Enter", remote=source)
            self.assertEqual(run.call_count, 1)

    def test_hierarchy_cannot_rebind_old_ids_to_new_session(self):
        with loaded_relay() as relay:
            source = self.configured(relay)
            def changed(*args, **kwargs):
                if args[:2] == ("tab", "list"):
                    relay.MACHINE_REGISTRY.machines[source]["session"] = "different"
                    return '{"result":{"tabs":[{"tab_id":"w1:t1","workspace_id":"w1"}]}}'
                return '{"result":{"workspaces":[{"workspace_id":"w1"}]}}'
            with mock.patch.object(relay, "run_herdr", side_effect=changed), self.assertRaises(ValueError):
                relay.get_spaces_from_host(source)
            self.assertEqual(relay.source_bindings, {})

    def test_named_local_sessions_have_distinct_bindings(self):
        with loaded_relay() as relay:
            relay.ACTIVE_SESSIONS[None] = "first"
            first = relay.bind_source_id(None, "w1:p1")
            self.assertNotEqual(first, "w1:p1")
            self.assertEqual(relay.translate_source_args(("pane", "read", first), None)[2], "w1:p1")
            relay.ACTIVE_SESSIONS[None] = "second"
            with self.assertRaises(ValueError):
                relay.translate_source_args(("pane", "read", first), None)
            self.assertNotEqual(first, relay.bind_source_id(None, "w1:p1"))

    def test_remote_push_is_only_a_hint(self):
        with loaded_relay() as relay:
            async def exercise():
                await relay.event_queue.put({"type": "agent_event", "host": "user@host", "pane_id": "w1:p1", "status": "blocked"})
                task = asyncio.create_task(relay.event_push())
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            with mock.patch.object(relay, "get_all_panes") as poll, mock.patch.object(relay, "broadcast", new_callable=mock.AsyncMock) as broadcast:
                asyncio.run(exercise())
            self.assertEqual(relay.agent_cache, {})
            poll.assert_not_called()
            broadcast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
