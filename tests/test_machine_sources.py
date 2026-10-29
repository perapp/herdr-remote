"""Pure registry/identity tests; never contact registered machines."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("machine_sources_test", Path(__file__).resolve().parents[1] / "relay/sources.py")
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


def profile(ident="one", **changes):
    return {"id": ident, "label": ident.title(), "target": "user@host", "session": "saved", "enabled": True, "selected": False, **changes}


class MachineSourcesTests(unittest.TestCase):
    def test_real_array_shape_and_wrapper(self):
        registry = sources.MachineRegistry()
        expected = registry.parse([profile()])
        self.assertEqual(expected, registry.parse(json.dumps({"machines": [profile()]})))
        self.assertEqual(expected["machine:one"]["session"], "saved")

    def test_failure_is_not_removal_and_refresh_is_cached(self):
        registry = sources.MachineRegistry()
        discover = mock.Mock(return_value=json.dumps([profile()]))
        self.assertTrue(registry.refresh(discover))
        self.assertFalse(registry.refresh(discover))
        self.assertEqual(discover.call_count, 1)
        discover.return_value = '{"error":"no registry"}'
        self.assertFalse(registry.refresh(discover, force=True))
        self.assertIn("machine:one", registry.machines)
        self.assertIsNotNone(registry.error)
        discover.side_effect = OSError("offline")
        self.assertFalse(registry.refresh(discover, force=True))
        self.assertIn("machine:one", registry.machines)
        discover.side_effect = None
        discover.return_value = "[]"
        self.assertTrue(registry.refresh(discover, force=True))
        self.assertEqual(registry.machines, {})

    def test_opt_out_and_allowlist(self):
        discover = mock.Mock()
        registry = sources.MachineRegistry("off")
        registry.refresh(discover)
        discover.assert_not_called()
        selected = sources.MachineRegistry("one").parse([profile(), profile("two")])
        self.assertEqual(list(selected), ["machine:one"])
        self.assertEqual(sources.MachineRegistry().parse([profile(enabled=False)]), {})

    def test_malformed_profiles_fail_whole_read(self):
        for value in ({}, [None], [profile(), profile()], [profile(session=None)],
                      [profile(target="-oops")], [profile(enabled="yes")]):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                sources.MachineRegistry().parse(value)

    def test_wire_ids_bind_source_session_and_target(self):
        raw = "w1:p1"
        make = sources.wire_id
        self.assertEqual(make(None, "default", None, raw), raw)
        wire = make("machine:one", "saved", "host", raw)
        self.assertEqual(wire, make("machine:one", "saved", "host", raw))
        self.assertRegex(wire, r"^[A-Za-z0-9:_.-]{1,128}$")
        self.assertEqual(len({wire, make("machine:two", "saved", "host", raw),
                              make("machine:one", "other", "host", raw),
                              make("machine:one", "saved", "other", raw)}), 4)
        self.assertLessEqual(len(make("machine:one", "saved", "host", "a" * 128)), 128)


if __name__ == "__main__":
    unittest.main()
