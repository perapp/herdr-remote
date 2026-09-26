"""Phone reading/composer geometry and safe compact transcript links, in a real browser."""
import os
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

PAGE = (Path(__file__).resolve().parents[1] / "web" / "index.html").as_uri()
CHROME = next((p for p in (
    os.environ.get("HERDR_TEST_CHROME", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
    "/usr/bin/chromium", "/usr/bin/google-chrome",
) if p and os.path.exists(p)), None)


@unittest.skipIf(sync_playwright is None or CHROME is None, "playwright/chromium unavailable")
class MobileReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=CHROME)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 390, "height": 844})
        self.page.route("https://esm.sh/**", lambda route: route.abort())
        self.page.goto(PAGE)
        self.page.evaluate("""() => {
          document.getElementById('settingsView').style.display = 'none';
          document.getElementById('agentListView').style.display = 'none';
          document.getElementById('terminalView').classList.add('active');
          activePane = 'phone:p1';
          agents = [{pane_id: activePane, agent: 'pi', status: 'working', project: 'billing'}];
          window.__sent = [];
          ws = {readyState: 1, send: p => window.__sent.push(JSON.parse(p))};
          document.getElementById('termTitle').textContent = 'billing · pi';
          setSessionView('conversation');
        }""")

    def tearDown(self):
        self.page.close()

    def test_phone_has_one_header_and_composer_is_not_covered(self):
        self.assertFalse(self.page.locator("body > .header").is_visible())
        for width, height in ((320, 640), (390, 844), (600, 800), (390, 380)):
            with self.subTest(width=width, height=height):
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.wait_for_function("Math.abs(visualViewport.height - parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--session-viewport-height'))) < 2")
                result = self.page.evaluate("""() => {
                  const input = document.getElementById('termInput').getBoundingClientRect();
                  const history = document.getElementById('termHistory').getBoundingClientRect();
                  const composer = document.querySelector('.term-input').getBoundingClientRect();
                  const hit = document.elementFromPoint(input.x + input.width / 2, input.y + 10);
                  return {inputWidth: input.width, bottom: composer.bottom, end: history.bottom,
                    start: composer.top, reachable: hit.id === 'termInput', overflow:
                    document.documentElement.scrollWidth > innerWidth};
                }""")
                self.assertGreater(result["inputWidth"], width - 145)
                self.assertLessEqual(result["bottom"], height + 1)
                self.assertLessEqual(result["end"], result["start"] + 1)
                self.assertTrue(result["reachable"])
                self.assertFalse(result["overflow"])

    def test_tools_do_not_compete_with_the_input_until_requested(self):
        self.assertFalse(self.page.locator("#composerTools").is_visible())
        self.page.click("#composerToolsBtn")
        self.assertTrue(self.page.locator("#composerTools").is_visible())
        self.assertEqual(self.page.get_attribute("#composerToolsBtn", "aria-expanded"), "true")
        self.page.click("#keysDockBtn")
        self.assertTrue(self.page.locator("#termKeys").is_visible())
        self.page.click("#composerToolsBtn")
        self.assertFalse(self.page.locator("#termKeys").is_visible())

    def test_composer_grows_for_newlines_and_shift_enter_does_not_send(self):
        self.page.fill("#termInput", "one")
        before = self.page.locator("#termInput").bounding_box()["height"]
        self.page.press("#termInput", "Shift+Enter")
        self.page.fill("#termInput", "one\ntwo\nthree")
        self.assertGreater(self.page.locator("#termInput").bounding_box()["height"], before)
        self.assertFalse(self.page.evaluate("__sent.some(m => m.type === 'respond')"))
        self.page.evaluate("() => { termInput.value = ''; resizeComposer(); }")
        self.assertEqual(self.page.locator("#termInput").bounding_box()["height"], before)

    def test_primary_controls_have_phone_sized_targets(self):
        for selector in (".term-header .back", "#sessionActions summary", "#conversationModeBtn",
                         "#terminalModeBtn", "#composerToolsBtn", ".term-send"):
            with self.subTest(selector=selector):
                box = self.page.locator(selector).bounding_box()
                self.assertGreaterEqual(box["height"], 44)
                self.assertGreaterEqual(box["width"], 44)

    def test_prose_wraps_but_code_scrolls_inside_the_conversation(self):
        self.page.evaluate(r"""() => {
          const body = document.getElementById('historyContent');
          const row = document.createElement('div'); row.className = 'msg-text';
          row.append(mdFragment('Readable prose '.repeat(30) + '\n\n```sh\n' + 'x'.repeat(160) + '\n```'));
          body.replaceChildren(row);
        }""")
        self.assertEqual(self.page.locator("#historyContent pre").evaluate("e => getComputedStyle(e).overflowX"), "auto")
        self.assertTrue(self.page.locator("#historyContent pre").evaluate("e => e.scrollWidth > e.clientWidth"))
        self.assertFalse(self.page.locator("#historyContent").evaluate("e => e.scrollWidth > e.clientWidth"))

    def test_bare_url_is_compact_but_href_and_copy_keep_the_exact_destination(self):
        url = "https://example.com/surface?token=" + "a" * 100
        result = self.page.evaluate("""url => {
          const host = document.createElement('div'); host.append(mdFragment(url));
          document.getElementById('historyContent').replaceChildren(host);
          const link = host.querySelector('a');
          return {href: link.href, label: link.textContent, hasCopy: !!host.querySelector('button')};
        }""", url)
        self.assertEqual(result["href"], url)
        self.assertEqual(result["label"], "example.com/surface")
        self.assertTrue(result["hasCopy"])
        self.page.evaluate("""() => {
          Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copied = text;}}});
        }""")
        self.page.click(".conversation-link button")
        self.assertEqual(self.page.evaluate("window.copied"), url)
        self.assertEqual(self.page.locator(".conversation-link button").inner_text(), "Copied")

    def test_code_explicit_labels_and_invalid_urls_remain_safe(self):
        result = self.page.evaluate("""() => {
          const node = document.createElement('div');
          node.append(mdFragment('`https://example.com/?token=secret` [Docs](https://example.com) http://[bad <img src=x>'));
          return {code: node.querySelector('code').textContent,
            links: [...node.querySelectorAll('a')].map(a => a.textContent), text: node.textContent,
            images: node.querySelectorAll('img').length};
        }""")
        self.assertEqual(result["code"], "https://example.com/?token=secret")
        self.assertEqual(result["links"], ["Docs"])
        self.assertIn("http://[bad", result["text"])
        self.assertEqual(result["images"], 0)


if __name__ == "__main__":
    unittest.main()
