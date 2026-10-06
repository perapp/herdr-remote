"""Runtime-discovered command palette, safe rendering, and explicit user shortcuts."""
import unittest

from test_web_history import PAGE, _chrome, sync_playwright

_shared = {}


def setUpModule():
    if sync_playwright is None or _chrome() is None:
        return
    _shared['playwright'] = sync_playwright().start()
    _shared['browser'] = _shared['playwright'].chromium.launch(executable_path=_chrome())


def tearDownModule():
    if 'browser' in _shared:
        _shared['browser'].close()
        _shared['playwright'].stop()
    _shared.clear()


@unittest.skipIf(sync_playwright is None or _chrome() is None, 'playwright/chromium unavailable')
class CommandPaletteTests(unittest.TestCase):
    def setUp(self):
        self.page = _shared['browser'].new_page(viewport={'width': 390, 'height': 844})
        self.page.route('https://esm.sh/**', lambda route: route.abort())
        self.page.add_init_script("""const originalTimer = window.setTimeout;
          window.setTimeout = (fn, ...args) => fn.name === 'connect' ? 0 : originalTimer(fn, ...args);""")
        self.page.goto(PAGE)
        self.page.evaluate("""() => {
          localStorage.removeItem(COMMAND_SHORTCUTS_KEY);
          localStorage.removeItem(COMMAND_FAVORITES_KEY);
          agents = [{pane_id:'pi',agent:'pi'}, {pane_id:'other',agent:'claude'}];
          activePane = 'pi'; window.sent = [];
          ws = {readyState:1, send: raw => sent.push(JSON.parse(raw))};
          openCommandPalette();
        }""")

    def tearDown(self):
        self.page.close()

    def reply(self, **fields):
        req = self.page.evaluate("sent.findLast(m => m.type === 'get_commands')")
        self.page.evaluate('msg => receiveSessionMessage(msg)', {
            'type': 'commands', 'pane_id': req['pane_id'], 'request_id': req['request_id'],
            'commands': [
                {'cmd':'/git-mode', 'desc':'Toggle Git permissions', 'source':'extension'},
                {'cmd':'/write-mode', 'desc':'Toggle writing', 'source':'extension'},
                {'cmd':'/skill:example', 'desc':'A loaded skill', 'source':'skill'},
            ], **fields,
        })

    def test_loaded_extensions_and_skills_appear_without_search(self):
        self.reply()
        text = self.page.locator('#cmdList').inner_text()
        for command in ('/git-mode', '/write-mode', '/skill:example', '/model'):
            self.assertIn(command, text)
        self.assertIn('3 loaded commands', self.page.locator('#cmdStatus').inner_text())
        self.page.get_by_role('button', name='/write-mode Toggle writing extension', exact=True).click()
        writes = self.page.evaluate("sent.filter(m => m.type === 'send_text' || m.type === 'send_keys')")
        self.assertEqual(writes, [{'type':'send_text','pane_id':'pi','text':'/write-mode'},
                                 {'type':'send_keys','pane_id':'pi','keys':['Enter']}])

    def test_shortcuts_persist_without_discovery_and_are_agent_scoped(self):
        self.reply(commands=[], unavailable='no-catalog')
        self.page.locator('#cmdShortcutEditor summary').click()
        self.page.fill('#cmdShortcuts', '/mine | My shortcut\n/git-mode | Manual fallback')
        self.page.get_by_role('button', name='Save shortcuts', exact=True).click()
        self.assertIn('shortcut', self.page.locator('#cmdList').inner_text())
        self.page.evaluate('hidePalette(); openCommandPalette()')
        self.assertIn('/mine', self.page.locator('#cmdList').inner_text())
        self.page.evaluate("hidePalette(); activePane='other'; openCommandPalette()")
        self.assertNotIn('/mine', self.page.locator('#cmdList').inner_text())

    def test_runtime_wins_over_duplicate_shortcuts_and_static_defaults(self):
        self.page.evaluate("""() => localStorage.setItem(COMMAND_SHORTCUTS_KEY,
          JSON.stringify({pi:[{cmd:'/git-mode',desc:'Old description'},{cmd:'/model',desc:'Manual model'}]}))""")
        self.reply(commands=[{'cmd':'/git-mode','desc':'Actual loaded extension','source':'extension'}])
        self.assertEqual(self.page.locator('.cmd-name', has_text='/git-mode').count(), 1)
        self.assertIn('Actual loaded extension', self.page.locator('#cmdList').inner_text())
        self.assertNotIn('Old description', self.page.locator('#cmdList').inner_text())

    def test_metadata_cannot_inject_markup_or_handler_code(self):
        self.reply(commands=[
            {'cmd':'/safe','desc':'<img src=x onerror=alert(1)>','source':'extension'},
            {'cmd':"/bad');alert(1);//",'desc':'bad','source':'extension'},
            {'cmd':'/bad\n/other','desc':'bad','source':'extension'},
            {'cmd':['/array'],'desc':'bad','source':'extension'},
        ])
        self.assertEqual(self.page.locator('#cmdList img').count(), 0)
        self.assertIn('<img src=x onerror=alert(1)>', self.page.locator('#cmdList').inner_text())
        self.assertNotIn('/bad', self.page.locator('#cmdList').inner_text())
        self.assertNotIn('/array', self.page.locator('#cmdList').inner_text())

    def test_late_pane_connection_and_request_replies_are_ignored(self):
        first = self.page.evaluate('sent[0]')
        self.page.evaluate("hidePalette(); activePane='other'; openCommandPalette()")
        self.page.evaluate("r => receiveCommands({...r,type:'commands',commands:[{cmd:'/wrong',desc:'stale',source:'extension'}]})", first)
        self.assertNotIn('/wrong', self.page.locator('#cmdList').inner_text())
        self.reply(commands=[{'cmd':'/current','desc':'Current','source':'extension'}])
        self.assertIn('/current', self.page.locator('#cmdList').inner_text())
        self.page.evaluate("ws = {readyState:1,send: raw => sent.push(JSON.parse(raw))}")
        self.page.evaluate("runCommand('/current')")
        self.assertFalse(self.page.evaluate("sent.some(m => m.type === 'send_text')"))

    def test_discovery_errors_keep_fallback_and_shortcut_editor(self):
        self.reply(type='error', scope='get_commands', commands=[], message='failure')
        self.assertIn('failed', self.page.locator('#cmdStatus').inner_text())
        self.assertIn('/model', self.page.locator('#cmdList').inner_text())
        self.assertTrue(self.page.locator('#cmdShortcutEditor summary').is_visible())

    def test_offline_never_sends_a_command(self):
        self.page.evaluate("ws.readyState=3; runCommand('/model')")
        self.assertFalse(self.page.evaluate("sent.some(m => m.type === 'send_text')"))
        self.assertIn('Not connected', self.page.locator('#cmdStatus').inner_text())

    def test_invalid_shortcut_does_not_overwrite_saved_entries(self):
        self.page.locator('#cmdShortcutEditor summary').click()
        self.page.fill('#cmdShortcuts', '/valid | Good')
        self.page.get_by_role('button', name='Save shortcuts', exact=True).click()
        self.page.fill('#cmdShortcuts', 'not a slash command')
        self.page.get_by_role('button', name='Save shortcuts', exact=True).click()
        self.assertIn('Use /command', self.page.locator('#cmdShortcutStatus').inner_text())
        self.assertIn('/valid', self.page.evaluate('JSON.stringify(savedCommandShortcuts())'))

    def heart(self, command):
        return self.page.get_by_role('button', name=f'Favorite {command}', exact=True)

    def names(self):
        return self.page.locator('#cmdList .cmd-name').all_text_contents()

    def test_hearts_toggle_and_sort_without_executing_commands(self):
        self.reply()
        original = self.names()
        self.assertEqual(self.heart('/write-mode').inner_text(), '🤍')
        self.heart('/write-mode').click()
        self.assertEqual(self.heart('/write-mode').get_attribute('aria-pressed'), 'true')
        self.assertEqual(self.heart('/write-mode').inner_text(), '❤️')
        self.assertEqual(self.names(), ['/write-mode'] + [c for c in original if c != '/write-mode'])
        self.heart('/git-mode').click()
        self.assertEqual(self.names()[:2], ['/git-mode', '/write-mode'])
        self.heart('/write-mode').click()
        self.assertEqual(self.heart('/write-mode').get_attribute('aria-pressed'), 'false')
        self.assertEqual(self.names(), ['/git-mode'] + [c for c in original if c != '/git-mode'])
        self.assertFalse(self.page.evaluate("sent.some(m => m.type === 'send_text' || m.type === 'send_keys')"))
        self.assertTrue(self.page.locator('#cmdPalette').is_visible())
        self.assertEqual(self.page.locator('#cmdList button button').count(), 0)

    def test_favorites_survive_page_reload(self):
        self.heart('/model').click()
        self.page.reload()
        self.page.evaluate("""() => {
          agents = [{pane_id:'pi',agent:'pi'}]; activePane = 'pi';
          window.sent = []; ws = {readyState:1,send: raw => sent.push(JSON.parse(raw))};
          openCommandPalette();
        }""")
        self.assertEqual(self.names()[0], '/model')
        self.assertEqual(self.heart('/model').get_attribute('aria-pressed'), 'true')

    def test_favorites_are_shared_between_panes_but_scoped_to_agent_type(self):
        self.heart('/model').click()
        self.page.evaluate("hidePalette(); activePane='other'; openCommandPalette()")
        self.assertEqual(self.heart('/model').get_attribute('aria-pressed'), 'false')
        self.heart('/clear').click()
        self.page.evaluate("""hidePalette(); agents.push({pane_id:'second-pi',agent:'pi'});
          activePane='second-pi'; openCommandPalette()""")
        self.assertEqual(self.names()[0], '/model')
        stored = self.page.evaluate('JSON.parse(localStorage.getItem(COMMAND_FAVORITES_KEY))')
        self.assertEqual(stored, {'pi':['/model'], 'claude':['/clear']})

    def test_search_only_command_becomes_visible_in_quick_list_when_favorited(self):
        self.page.fill('#cmdSearch', '/reload')
        self.heart('/reload').click()
        self.page.fill('#cmdSearch', '')
        self.assertEqual(self.names()[0], '/reload')
        self.heart('/reload').click()
        self.assertNotIn('/reload', self.names())
        self.assertTrue(self.page.locator('#cmdSearch').evaluate('e => e === document.activeElement'))

    def test_search_respects_favorite_order_but_does_not_show_nonmatches(self):
        self.reply()
        self.heart('/write-mode').click()
        self.page.fill('#cmdSearch', 'mode')
        self.assertEqual(self.names(), ['/write-mode', '/model', '/git-mode'])
        self.page.fill('#cmdSearch', 'git')
        self.assertEqual(self.names(), ['/git-mode'])

    def test_unavailable_runtime_favorites_are_remembered_but_not_offered(self):
        self.reply()
        self.heart('/write-mode').click()
        self.page.evaluate('hidePalette(); openCommandPalette()')
        self.reply(commands=[], unavailable='no-catalog')
        self.assertNotIn('/write-mode', self.names())
        self.assertTrue(self.page.evaluate("savedCommandFavorites().has('/write-mode')"))
        self.page.evaluate('hidePalette(); openCommandPalette()')
        self.reply()
        self.assertEqual(self.names()[0], '/write-mode')

    def test_keyboard_toggle_keeps_focus_on_heart_and_never_runs_command(self):
        self.reply()
        self.heart('/write-mode').focus()
        self.page.keyboard.press('Space')
        self.assertTrue(self.heart('/write-mode').evaluate('e => e === document.activeElement'))
        self.assertEqual(self.heart('/write-mode').get_attribute('aria-pressed'), 'true')
        self.page.keyboard.press('Enter')
        self.assertTrue(self.heart('/write-mode').evaluate('e => e === document.activeElement'))
        self.assertEqual(self.heart('/write-mode').get_attribute('aria-pressed'), 'false')
        self.assertFalse(self.page.evaluate("sent.some(m => m.type === 'send_text' || m.type === 'send_keys')"))

    def test_storage_failure_does_not_pretend_to_save(self):
        self.page.evaluate("""() => {
          const original = Storage.prototype.setItem;
          Storage.prototype.setItem = function(key, value) {
            if (key === COMMAND_FAVORITES_KEY) throw new DOMException('Full', 'QuotaExceededError');
            return original.call(this, key, value);
          };
        }""")
        original = self.names()
        self.heart('/model').click()
        self.assertEqual(self.names(), original)
        self.assertEqual(self.heart('/model').get_attribute('aria-pressed'), 'false')
        self.assertIn('Could not save', self.page.locator('#cmdFavoriteStatus').inner_text())

    def test_corrupt_favorites_recover_and_untrusted_values_are_ignored(self):
        for corrupt in ('not json', 'null', '[]', '{"pi":42}'):
            self.page.evaluate('value => localStorage.setItem(COMMAND_FAVORITES_KEY, value)', corrupt)
            self.page.evaluate('filterCommands()')
            self.assertEqual(self.names()[0], '/compact')
            self.heart('/model').click()
            self.assertEqual(self.names()[0], '/model')
        self.page.evaluate("""localStorage.setItem(COMMAND_FAVORITES_KEY,
          JSON.stringify({pi:[null,42,['/compact'],'/bad\\n','/model','/model','<img src=x>']}));
          filterCommands()""")
        self.assertEqual(self.page.evaluate('[...savedCommandFavorites()]'), ['/model'])
        self.assertEqual(self.page.locator('#cmdList img').count(), 0)

    def test_favorites_work_offline_and_do_not_replace_shortcuts(self):
        self.page.evaluate("""localStorage.setItem(COMMAND_SHORTCUTS_KEY,
          JSON.stringify({pi:[{cmd:'/mine',desc:'My shortcut'}]})); ws.readyState=3; filterCommands()""")
        self.heart('/mine').click()
        self.assertEqual(self.names()[0], '/mine')
        self.assertIn('/mine', self.page.evaluate('localStorage.getItem(COMMAND_SHORTCUTS_KEY)'))
        self.assertFalse(self.page.evaluate("sent.some(m => m.type === 'send_text' || m.type === 'send_keys')"))

    def test_phone_dialog_has_no_horizontal_overflow(self):
        self.reply()
        for width in (320, 390, 600):
            self.page.set_viewport_size({'width':width, 'height':640})
            self.assertFalse(self.page.locator('.cmd-panel').evaluate('e => e.scrollWidth > e.clientWidth'))
            self.assertGreaterEqual(self.page.locator('.cmd-item').first.bounding_box()['height'], 44)
            heart = self.heart('/git-mode').bounding_box()
            command = self.page.locator('.cmd-item').filter(has_text='/git-mode').bounding_box()
            self.assertGreaterEqual(heart['width'], 44)
            self.assertGreaterEqual(heart['height'], 44)
            self.assertGreaterEqual(heart['x'], command['x'] + command['width'])


if __name__ == '__main__':
    unittest.main()
