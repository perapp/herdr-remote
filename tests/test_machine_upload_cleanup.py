"""Machine upload retirement never follows a reconfigured logical profile."""
import subprocess
import unittest
from unittest import mock

from test_herdr_relay import loaded_relay
from test_machine_sources import profile


class MachineUploadCleanupTests(unittest.TestCase):
    def configure(self, relay):
        relay.MACHINE_REGISTRY.setting = "auto"
        relay.MACHINE_REGISTRY.refresh(lambda: [profile()], force=True)
        relay.REMOTES = []
        return "machine:one"

    def entry(self, relay, host="machine:one", target="user@host", version=None, index=1, old=True):
        return {
            "host": host, "remote": host, "physical_target": target,
            "source_fingerprint": version or relay.source_fingerprint(host),
            "session": "saved", "pane_id": f"pane{index}", "ref": None,
            "folder": f"p-{index:016x}", "path": f"/upload/{index}.txt",
            "at": 0 if old else relay.time.time(),
        }

    def test_ended_sweep_filters_old_generations_instead_of_poisoning_current(self):
        with loaded_relay() as relay:
            source = self.configure(relay)
            current = self.entry(relay)
            historical = self.entry(relay, version="old", index=2)
            ambiguous = self.entry(relay, index=3)
            ambiguous.pop("physical_target")
            relay.upload_ledger[:] = [current, historical, ambiguous]
            relay.pane_list_ok[source] = True
            relay.pane_refs_by_host[source] = ("saved", {})
            with mock.patch.object(relay, "_remote_upload_script", return_value=True) as cleanup, \
                 mock.patch.object(relay.uploads, "save_ledger"):
                relay.sweep_ended_uploads(force=True)
            self.assertEqual(relay.upload_ledger, [historical, ambiguous])
            cleanup.assert_called_once_with("user@host", relay.uploads.remote_delete_script([current["folder"]]))

    def test_ended_sweep_still_requires_successful_list(self):
        with loaded_relay() as relay:
            source = self.configure(relay)
            entry = self.entry(relay)
            relay.upload_ledger[:] = [entry]
            relay.pane_list_ok[source] = False
            relay.pane_refs_by_host[source] = ("saved", {})
            with mock.patch.object(relay, "_remote_upload_script") as cleanup:
                relay.sweep_ended_uploads(force=True)
            cleanup.assert_not_called()
            self.assertEqual(relay.upload_ledger, [entry])

    def test_ttl_retries_recorded_target_after_removal_or_reconfiguration(self):
        for removed in (False, True):
            with self.subTest(removed=removed), loaded_relay() as relay:
                source = self.configure(relay)
                first = self.entry(relay)
                second = self.entry(relay, index=2)
                fresh = self.entry(relay, index=3, old=False)
                relay.upload_ledger[:] = [first, second, fresh]
                if removed:
                    relay.MACHINE_REGISTRY.machines.clear()
                else:
                    relay.MACHINE_REGISTRY.machines[source]["target"] = "new-host"
                    relay.MACHINE_REGISTRY.machines[source]["session"] = "new-session"
                relay.pane_list_ok[source] = False
                with mock.patch.object(relay.uploads, "expire_local"), \
                     mock.patch.object(relay.uploads, "save_ledger"), \
                     mock.patch.object(relay, "upload_ssh", side_effect=[(1, b""), (0, b"")]) as transport:
                    relay.expire_uploads()
                    self.assertEqual(relay.upload_ledger, [first, second, fresh])
                    relay.expire_uploads()
                self.assertEqual(relay.upload_ledger, [fresh])
                self.assertEqual(transport.call_count, 2)
                for call in transport.call_args_list:
                    self.assertEqual(call.args[0], "user@host")
                    self.assertEqual(call.kwargs["timeout"], relay.UPLOAD_SWEEP_SSH_TIMEOUT)

    def test_ttl_groups_profiles_and_legacy_alias_by_endpoint(self):
        with loaded_relay() as relay:
            self.configure(relay)
            relay.REMOTES = ["user@host"]
            relay.pane_list_ok["user@host"] = True
            entries = [self.entry(relay), self.entry(relay, host="machine:removed", version="old", index=2)]
            relay.upload_ledger[:] = entries
            with mock.patch.object(relay.uploads, "expire_local"), \
                 mock.patch.object(relay.uploads, "save_ledger"), \
                 mock.patch.object(relay, "upload_ssh", return_value=(0, b"")) as transport:
                relay.expire_uploads()
            transport.assert_called_once()
            self.assertEqual(transport.call_args.args[0], "user@host")
            self.assertEqual(relay.upload_ledger, [])

    def test_ambiguous_or_unsafe_machine_entries_are_reported_and_retained(self):
        for target in (None, "", "-oops", "machine:one", "host name", "host\x00name"):
            with self.subTest(target=target), loaded_relay() as relay:
                self.configure(relay)
                entry = self.entry(relay, target=target)
                if target is None:
                    entry.pop("physical_target")
                relay.upload_ledger[:] = [entry]
                with mock.patch.object(relay.uploads, "expire_local"), \
                     mock.patch.object(relay, "upload_ssh") as transport, \
                     mock.patch.object(relay.log, "warning") as warning:
                    relay.expire_uploads()
                transport.assert_not_called()
                self.assertEqual(relay.upload_ledger, [entry])
                self.assertIn("orphaned machine uploads", warning.call_args.args[0])

    def test_ttl_timeout_keeps_ledger_for_retry(self):
        with loaded_relay() as relay:
            self.configure(relay)
            entry = self.entry(relay)
            relay.upload_ledger[:] = [entry]
            with mock.patch.object(relay.uploads, "expire_local"), \
                 mock.patch.object(relay, "upload_ssh", side_effect=subprocess.TimeoutExpired("ssh", 15)):
                relay.expire_uploads()
            self.assertEqual(relay.upload_ledger, [entry])


if __name__ == "__main__":
    unittest.main()
