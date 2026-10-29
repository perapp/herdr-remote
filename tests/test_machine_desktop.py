"""Portable desktop routing contracts; native UI/runtime checks need their host OS."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class DesktopMachineContracts(unittest.TestCase):
    def source(self, path):
        return (ROOT / path).read_text()

    def test_mac_discovers_enabled_profiles_not_selection(self):
        source = self.source('herdi-mac/Sources/RelayConnection.swift')
        self.assertIn('runHerdr("machine", "list", "--json")', source)
        self.assertIn('HERDR_MACHINES', source)
        self.assertIn('if enabled {', source)
        self.assertNotIn('item["selected"]', source)
        self.assertIn('["--machine", route.source.profileId] + args', source)
        self.assertNotIn('"--session"', source)

    def test_mac_explicit_raw_route_and_stale_guard(self):
        source = self.source('herdi-mac/Sources/RelayConnection.swift')
        self.assertIn('let rawPaneId: String', source)
        self.assertIn('[kind, profileId, session, target]', source)
        self.assertIn('[source.key, rawPaneId]', source)
        self.assertIn('machines.contains(where: { $0.key == route.source.key })', source)
        self.assertNotIn('drop(while:', source)
        self.assertIn('guard let route = currentRoute(paneId)', source)
        self.assertIn('route.rawPaneId', source)

    def test_windows_registry_and_native_route(self):
        cli = self.source('herdi-win/Services/HerdrCli.cs')
        self.assertIn('"machine", "list", "--json"', cli)
        self.assertIn('HERDR_MACHINES', cli)
        self.assertNotIn('"selected"', cli)
        self.assertIn('"--machine", route.ProfileId', cli)
        self.assertNotIn('"--session"', cli)
        self.assertIn('current.Any(r => r.Key == route.Key)', cli)
        self.assertIn('LocalBinary is null', cli)
        route = self.source('herdi-win/Models/DirectRoute.cs')
        self.assertIn('Kind, ProfileId, Session, Target', route)
        poller = self.source('herdi-win/Services/HerdrPoller.cs')
        self.assertIn('RawPaneId', poller)
        self.assertNotIn('StartsWith(prefix', poller)

    def test_relay_labels_are_metadata_not_routing(self):
        mac = self.source('herdi-mac/Sources/RelayConnection.swift')
        self.assertIn('id: data.pane_id', mac)
        self.assertIn('data.source_label', mac)
        self.assertIn('source_label', self.source('herdi-mac/Sources/Agent.swift'))
        protocol = self.source('herdi-win/Models/Protocol.cs')
        self.assertIn('"source_label"', protocol)
        connection = self.source('herdi-win/Services/RelayConnection.cs')
        self.assertIn('Protocol.ReadPane(agent.PaneId', connection)
        self.assertIn('Protocol.AgentPrompt(agent.PaneId', connection)


if __name__ == '__main__':
    unittest.main()
