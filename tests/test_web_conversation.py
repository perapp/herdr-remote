"""Primary Conversation/Terminal modes, polling and safe transcript reconciliation."""
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


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipIf(_chrome() is None, "no chromium build available")
class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.page = _shared["browser"].new_page(viewport={"width": 390, "height": 844})
        self.page.goto(PAGE)
        self.page.evaluate("""() => {
          window.__sent = [];
          ws = {readyState: 1, send: raw => __sent.push(JSON.parse(raw))};
          window.setInterval = () => null; // ticks below are deliberate, not wall-clock races
          agents = [
            {pane_id:'pi', agent:'pi', project:'Demo', status:'working', has_session:true},
            {pane_id:'claude', agent:'claude', project:'Demo', status:'done', has_session:true},
            {pane_id:'other', agent:'codex', project:'Demo', status:'working'}];
          shellPanes = [{pane_id:'shell', project:'Shell'}];
        }""")

    def tearDown(self):
        self.page.close()

    def open(self, pane="pi"):
        self.page.evaluate("id => openTerminal(id)", pane)

    def requests(self):
        return self.page.evaluate("__sent.filter(m => m.type === 'get_history')")

    def reply(self, turns=(), **fields):
        request = self.requests()[-1]
        self.page.evaluate("msg => receiveSessionMessage(msg)", {
            "type": "history", "pane_id": request["pane_id"], "request_id": request["request_id"],
            "messages": list(turns), "total": len(turns), **fields,
        })

    def turns(self, count=40):
        return [{"uuid": str(i), "role": "assistant", "text": f"Turn {i}\n\n" + "Reading text. " * 15}
                for i in range(count)]

    def test_mobile_defaults_only_supported_agents_to_conversation(self):
        self.assertEqual(self.page.evaluate("agents.map(defaultSessionView)"),
                         ["conversation", "conversation", "terminal"])
        self.open()
        self.assertEqual(self.page.locator("#terminalView").get_attribute("data-session-view"), "conversation")
        self.assertEqual(self.requests()[-1]["include_tools"], True)
        self.assertFalse(self.page.locator("#termContent").is_visible())
        self.assertTrue(self.page.locator("#termInput").is_visible())
        self.open("shell")
        self.assertEqual(self.page.locator("#terminalView").get_attribute("data-session-view"), "terminal")
        self.page.set_viewport_size({"width": 601, "height": 844})
        self.assertEqual(self.page.evaluate("agents.map(defaultSessionView)"), ["terminal"] * 3)

    def test_explicit_choice_persists_and_modes_do_not_push_navigation(self):
        self.open()
        self.reply()
        depth = self.page.evaluate("navStack.length")
        self.page.click("#terminalModeBtn")
        self.assertEqual(self.page.locator("#terminalModeBtn").get_attribute("aria-pressed"), "true")
        self.page.click("#conversationModeBtn")
        self.assertEqual(self.page.evaluate("navStack.length"), depth)
        self.reply()
        self.page.click("#terminalModeBtn")
        self.open("shell")
        self.open()
        self.assertEqual(self.page.evaluate("sessionView"), "terminal")

    def test_blocked_update_preserves_view_scroll_content_and_request(self):
        self.open()
        self.reply(self.turns())
        self.page.evaluate("""() => {
          const body = document.getElementById('historyContent');
          body.scrollTop = 100;
          body.dispatchEvent(new Event('scroll'));
          window.__node = body.querySelector('.msg');
          window.__before = __sent.length;
          agents[0].status = 'blocked'; agents[0].options = ['Approve'];
          agents[0].prompt = 'Allow reading <private-file>?';
          openTerminal('pi');
        }""")
        self.assertEqual(self.page.evaluate("__sent.length === __before"), True)
        self.assertEqual(self.page.evaluate("document.querySelector('#historyContent .msg') === __node"), True)
        self.assertEqual(self.page.locator("#historyContent").evaluate("e => e.scrollTop"), 100)
        self.assertEqual(self.page.evaluate("historyFollowing"), False)
        self.assertEqual(self.page.locator("#quickActions > button").inner_text(), "Approve")
        self.assertEqual(self.page.locator("#sessionStatus").inner_text(), "Needs you")
        self.assertTrue(self.page.locator(".qa-context").is_visible())
        self.assertIn("Allow reading <private-file>?", self.page.locator(".qa-context").inner_text())
        self.assertEqual(self.page.locator(".qa-context private-file").count(), 0)

    def test_live_poll_has_one_request_and_keeps_unchanged_nodes_and_open_diffs(self):
        self.open()
        first = {"uuid": "tool", "role": "tool", "tool": "Edit", "text": "Edit x", "diff": "-old\n+new"}
        self.reply([first])
        self.page.evaluate("""() => {
          window.__node = document.querySelector('#historyContent details'); __node.open = true;
          mirrorTick(); mirrorTick(); mirrorTick();
        }""")
        self.assertEqual(len(self.requests()), 2)
        self.reply([first, {"uuid": "answer", "role": "assistant", "text": "Finished"}])
        self.assertEqual(self.page.evaluate("document.querySelector('#historyContent details') === __node && __node.open"), True)
        self.assertIn("Finished", self.page.locator("#historyContent").inner_text())
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), 3)

    def test_opening_tool_details_pauses_following_until_latest(self):
        self.open()
        self.reply([{"uuid": "tool", "role": "tool", "tool": "Read",
                     "target": "README.md", "text": "Read README.md"}])
        self.page.click("#historyContent summary")
        self.assertTrue(self.page.locator("#historyContent details").evaluate("e => e.open"))
        self.assertFalse(self.page.evaluate("historyFollowing"))
        count = len(self.requests())
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), count)
        self.assertTrue(self.page.locator("#historyLatestBtn").is_visible())

    def test_background_refresh_keeps_status_text_and_node_steady(self):
        self.open()
        self.assertIn("Loading", self.page.locator("#historyStatus").inner_text())
        self.reply([{"uuid": "one", "role": "assistant", "text": "Ready"}])
        label = self.page.locator("#historyStatus").inner_text()
        self.page.evaluate("""() => {
          window.statusNode = document.getElementById('historyStatus').firstChild;
          window.statusChanges = [];
          window.statusObserver = new MutationObserver(records => statusChanges.push(...records));
          statusObserver.observe(document.getElementById('historyStatus'), {childList:true, characterData:true, subtree:true});
          mirrorTick();
        }""")
        self.assertEqual(self.page.locator("#historyStatus").inner_text(), label)
        self.reply([{"uuid": "one", "role": "assistant", "text": "Ready"}])
        self.assertEqual(self.page.locator("#historyStatus").inner_text(), label)
        self.assertTrue(self.page.evaluate("statusNode === document.getElementById('historyStatus').firstChild"))
        self.assertEqual(self.page.evaluate("statusChanges.length"), 0)
        self.page.evaluate("historyFollowing = false; updateHistoryStatus()")
        self.assertIn("Paused", self.page.locator("#historyStatus").inner_text())
        self.page.evaluate("ws.readyState = 3; updateHistoryStatus()")
        self.assertIn("Offline", self.page.locator("#historyStatus").inner_text())

    def test_stale_pane_and_reopened_pane_replies_never_paint(self):
        self.open()
        stale = self.requests()[-1]
        self.open("claude")
        self.assertEqual(len(self.requests()), 1, "do not overlap reads while changing pane")
        self.page.evaluate("m => receiveSessionMessage({...m, type:'history', messages:[{uuid:'bad', text:'WRONG'}]})", stale)
        self.assertEqual(self.requests()[-1]["pane_id"], "claude")
        self.assertNotIn("WRONG", self.page.locator("#historyContent").inner_text())
        self.reply([{"uuid": "ok", "text": "Claude"}])
        self.page.evaluate("mirrorTick(); hideTerminal(); openTerminal('claude')")
        self.reply([{"uuid": "bad2", "text": "STALE REOPEN"}])
        self.assertNotIn("STALE REOPEN", self.page.locator("#historyContent").inner_text())
        self.reply([{"uuid": "ok2", "text": "Current reopen"}])
        self.assertIn("Current reopen", self.page.locator("#historyContent").inner_text())

    def test_tools_change_discards_inflight_page_without_overlapping_reads(self):
        self.open()
        self.page.evaluate("toggleHistoryTools()")
        self.assertEqual(len(self.requests()), 1)
        self.reply([{"uuid": "bad", "text": "OLD TOOLS"}])
        self.assertEqual(len(self.requests()), 2)
        self.assertFalse(self.requests()[-1]["include_tools"])
        self.assertNotIn("OLD TOOLS", self.page.locator("#historyContent").inner_text())
        self.reply([{"uuid": "ok", "text": "Prose"}])
        self.assertIn("Prose", self.page.locator("#historyContent").inner_text())

    def test_scroll_holds_even_inflight_update_until_latest(self):
        self.open()
        turns = self.turns()
        self.reply(turns)
        self.page.evaluate("""() => {
          mirrorTick();
          const el = document.getElementById('historyContent'); el.scrollTop = 120;
          el.dispatchEvent(new Event('scroll'));
        }""")
        self.reply(turns + [{"uuid": "new", "text": "NEW ARRIVAL"}])
        self.assertNotIn("NEW ARRIVAL", self.page.locator("#historyContent").inner_text())
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), 2)
        self.assertTrue(self.page.locator("#historyLatestBtn").is_visible())
        self.page.click("#historyLatestBtn")
        self.reply(turns + [{"uuid": "new", "text": "NEW ARRIVAL"}])
        self.assertIn("NEW ARRIVAL", self.page.locator("#historyContent").inner_text())
        self.assertTrue(self.page.evaluate("historyFollowing"))

    def test_selection_prevents_requests_and_inflight_dom_changes(self):
        self.open()
        turns = self.turns(3)
        self.reply(turns)
        self.page.evaluate("""() => {
          mirrorTick();
          const node = document.querySelector('#historyContent .msg-text p').firstChild;
          const range = document.createRange(); range.setStart(node, 0); range.setEnd(node, 6);
          getSelection().removeAllRanges(); getSelection().addRange(range);
        }""")
        self.reply([{"uuid": "changed", "text": "REPLACED"}])
        self.assertEqual(self.page.evaluate("getSelection().toString()"), "Turn 0")
        self.page.evaluate("mirrorTick(); loadOlderHistory()")
        self.assertEqual(len(self.requests()), 2)
        self.page.evaluate("getSelection().removeAllRanges(); mirrorTick()")
        self.assertEqual(len(self.requests()), 3)

    def test_paging_anchors_old_turns_and_latest_cancels_pending_older_page(self):
        self.open()
        turns = self.turns()
        self.reply(turns, has_more=True, total=80)
        self.page.evaluate("loadOlderHistory()")
        self.assertEqual(self.requests()[-1]["before"], "0")
        self.reply([{"uuid": "older", "text": "Older page"}], has_more=False, total=80)
        self.assertEqual(self.page.evaluate("history_.turns[0].uuid"), "older")
        count = len(self.requests())
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), count)
        self.page.evaluate("history_.hasMore = true; loadOlderHistory(); followConversation()")
        self.reply([{"uuid": "ancient", "text": "Wrong old page"}])
        self.assertNotIn("before", self.requests()[-1])
        self.assertNotIn("Wrong old page", self.page.locator("#historyContent").inner_text())

    def test_scoped_errors_and_unavailable_history_have_explanation_and_recovery(self):
        self.open()
        request = self.requests()[-1]
        self.page.evaluate("m => receiveSessionMessage({...m, type:'error', scope:'get_history', message:'Host unreachable'})", request)
        self.assertIn("Host unreachable", self.page.locator("#historyContent").inner_text())
        self.assertFalse(self.page.evaluate("history_.loading"))
        self.page.get_by_role("button", name="Retry", exact=True).click()
        self.reply(unavailable="no-log")
        self.assertIn("No transcript file", self.page.locator("#historyContent").inner_text())
        self.page.get_by_role("button", name="Open terminal", exact=True).click()
        self.assertEqual(self.page.evaluate("sessionView"), "terminal")

    def test_timeout_and_reconnect_release_loading_and_ignore_old_replies(self):
        self.page.clock.install()
        self.open()
        old = self.requests()[-1]
        self.page.clock.run_for(20001)
        self.assertFalse(self.page.evaluate("history_.loading"))
        self.assertIn("timed out", self.page.locator("#historyStatus").inner_text())
        self.page.evaluate("historyConnectionChanged(false); historyConnectionChanged(true)")
        self.assertEqual(len(self.requests()), 2)
        self.page.evaluate("m => receiveSessionMessage({...m, type:'history', messages:[{uuid:'bad',text:'EXPIRED'}]})", old)
        self.assertNotIn("EXPIRED", self.page.locator("#historyContent").inner_text())
        self.reply([{"uuid": "ok", "text": "Recovered"}])
        self.assertIn("Recovered", self.page.locator("#historyContent").inner_text())

    def test_initial_reply_while_terminal_or_panel_hidden_is_refetched_on_return(self):
        self.open()
        self.page.click("#terminalModeBtn")
        self.reply([{"uuid": "hidden", "text": "Hidden reply"}])
        self.page.click("#conversationModeBtn")
        self.assertEqual(len(self.requests()), 2)
        self.reply([{"uuid": "shown", "text": "Shown reply"}])
        self.page.evaluate("mirrorTick(); openPanel('settingsView')")
        self.reply(self.turns())
        count = len(self.requests())
        self.page.evaluate("openTerminal('pi'); mirrorTick(); mirrorTick()")
        self.assertEqual(len(self.requests()), count)
        self.assertFalse(self.page.locator("#terminalView").evaluate("e => e.classList.contains('active')"))
        self.assertTrue(self.page.evaluate("historyFollowing"))
        self.page.evaluate("hidePanel()")
        self.assertEqual(len(self.requests()), count + 1)

    def test_missing_log_retries_without_reopening(self):
        self.page.clock.install()
        self.open()
        self.reply(unavailable="no-log")
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), 1)
        self.page.clock.run_for(15001)
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), 2)
        self.reply([{"uuid": "created", "text": "Log now exists"}])
        self.assertIn("Log now exists", self.page.locator("#historyContent").inner_text())

    def test_resize_does_not_mistake_bottom_gap_for_reader_scrolling(self):
        self.open()
        self.reply(self.turns())
        self.page.evaluate("""() => {
          const body = document.getElementById('historyContent');
          body.style.height = (body.clientHeight - 120) + 'px'; body.style.flex = 'none';
          body.dispatchEvent(new Event('scroll')); mirrorTick();
        }""")
        self.assertTrue(self.page.evaluate("historyFollowing"))
        self.assertEqual(len(self.requests()), 2)

    def test_switch_scopes_composer_drafts_and_closes_auxiliary_controls(self):
        self.open()
        self.reply()
        self.page.evaluate("""() => {
          document.getElementById('termInput').value = 'Draft for pi'; resizeComposer();
          toggleComposerTools(); showDock('keys');
          document.getElementById('sessionActions').open = true;
          openTerminal('shell');
        }""")
        self.assertEqual(self.page.locator("#termInput").input_value(), "")
        self.assertFalse(self.page.locator("#composerTools").is_visible())
        self.assertFalse(self.page.locator("#termKeys").is_visible())
        self.assertFalse(self.page.locator("#sessionActions").evaluate("e => e.open"))
        self.open()
        self.assertEqual(self.page.locator("#termInput").input_value(), "Draft for pi")

    def test_non_diff_tool_expands_full_command_and_failure_on_phone(self):
        self.open()
        target = "run-command " + "long-argument " * 30
        self.reply([{"uuid": "tool", "role": "tool", "tool": "Bash", "target": target,
                     "text": "Tool summary", "error": True, "result": "Actual failure <detail>"}])
        tool = self.page.locator("#historyContent details")
        self.assertFalse(tool.evaluate("e => e.open"))
        tool.locator("summary").click()
        self.assertTrue(tool.locator(".tool-details").is_visible())
        self.assertIn(target, tool.locator(".tool-details").inner_text())
        self.assertIn("Actual failure <detail>", tool.locator(".tool-details").inner_text())
        self.assertEqual(tool.locator("detail").count(), 0)

    def test_find_routes_to_active_view_and_filter_holds_polling(self):
        self.open()
        self.reply(self.turns(3))
        self.page.evaluate("toggleSessionSearch()")
        self.assertTrue(self.page.locator("#historyFind").is_visible())
        self.assertFalse(self.page.locator("#termSearch").is_visible())
        self.page.fill("#historyFind", "Turn 1")
        self.page.evaluate("mirrorTick()")
        self.assertEqual(len(self.requests()), 1)
        self.page.click("#terminalModeBtn")
        self.page.evaluate("toggleSessionSearch()")
        self.assertTrue(self.page.locator("#termSearch").is_visible())
        self.assertFalse(self.page.locator("#termHistory").is_visible())
