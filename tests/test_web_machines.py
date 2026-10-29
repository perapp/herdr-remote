"""Machine-qualified panes and fixed registered-machine sessions in the browser."""
import unittest

from test_web_history import PAGE, _chrome, sync_playwright

_shared = {}


def setUpModule():
    if sync_playwright is None or _chrome() is None:
        return
    _shared["playwright"] = sync_playwright().start()
    _shared["browser"] = _shared["playwright"].chromium.launch(executable_path=_chrome())


def tearDownModule():
    if "browser" in _shared:
        _shared["browser"].close()
        _shared["playwright"].stop()
    _shared.clear()


@unittest.skipIf(sync_playwright is None or _chrome() is None, "playwright/chromium unavailable")
class MachineBrowserTests(unittest.TestCase):
    def setUp(self):
        self.page = _shared["browser"].new_page(viewport={"width": 390, "height": 844})
        self.page.goto(PAGE)
        self.page.evaluate("""() => {
          window.__sent = [];
          ws = {readyState: WebSocket.OPEN, send: raw => __sent.push(JSON.parse(raw))};
          sessionState = {sources: [
            {host:'local', active:'default', sessions:[{name:'default',running:true},{name:'work',running:true}]},
            {host:'machine:abc', label:'perd25', active:'default', switchable:false,
             sessions:[{name:'default',running:true}]}
          ]};
          renderSessionSelector();
        }""")

    def tearDown(self):
        self.page.close()

    def test_fixed_profile_session_is_information_not_a_switch(self):
        self.page.evaluate("toggleSessionMenu()")
        row = self.page.locator('#sessionMenu [aria-disabled="true"]')
        self.assertEqual(row.count(), 1)
        self.assertIn("configured in Herdr", row.inner_text())
        self.assertIn("perd25", self.page.locator("#sessionMenu").inner_text().lower())
        self.assertNotIn("machine:abc", self.page.locator("#sessionMenu").inner_text())
        row.dispatch_event("click")
        self.page.evaluate("switchHerdrSession('machine:abc', 'work')")
        self.assertEqual(self.page.evaluate("__sent"), [])
        self.assertEqual(row.get_attribute("tabindex"), "-1")

    def test_local_session_still_switches(self):
        self.page.evaluate("switchHerdrSession('local', 'work')")
        self.assertEqual(self.page.evaluate("__sent"), [
            {"type": "session_switch", "host": "local", "session": "work"}])

    def test_single_fixed_profile_is_still_visible(self):
        self.page.evaluate("sessionState.sources.shift(); renderSessionSelector()")
        self.assertTrue(self.page.locator("#sessionPicker").is_visible())

    def test_colliding_raw_panes_keep_distinct_wire_ids(self):
        self.page.evaluate("""() => {
          agents = [
            {pane_id:'w1:p1',host:'local',agent:'pi',project:'local',status:'working'},
            {pane_id:'r0123456789abcdef:w1:p1',host:'machine:abc',source_label:'perd25',
             agent:'pi',project:'remote',status:'working'}
          ];
          render();
        }""")
        remote = self.page.locator('[data-pane-id="r0123456789abcdef:w1:p1"]').first
        self.assertIn("perd25", remote.inner_text())
        self.assertNotIn("machine:abc", remote.inner_text())
        remote.click()
        self.assertEqual(self.page.evaluate("activePane"), "r0123456789abcdef:w1:p1")
        self.page.fill("#termInput", "hello remote")
        self.page.evaluate("sendText()")
        sends = self.page.evaluate("__sent.filter(m => m.type === 'send_text')")
        self.assertEqual(sends[-1]["pane_id"], "r0123456789abcdef:w1:p1")

    def test_spaces_with_the_same_name_show_the_machine(self):
        self.page.evaluate("""() => {
          spaces = {workspaces:[
            {workspace_id:'w1',label:'work',host:'local',number:1},
            {workspace_id:'w1',label:'work',host:'machine:abc',source_label:'perd25',number:1}
          ], tabs:[]};
        }""")
        rows = self.page.evaluate("workspaceRows()")
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])
        self.assertEqual(rows[0]["name"], "work")
        self.assertEqual(rows[1]["name"], "work @perd25")

    def test_machine_label_is_escaped(self):
        self.page.evaluate("""() => {
          sessionState.sources[1].label = '<img src=x onerror="window.__bad=true">';
          renderSessionSelector();
        }""")
        self.assertEqual(self.page.locator("#sessionMenu img").count(), 0)
        self.assertFalse(self.page.evaluate("!!window.__bad"))
