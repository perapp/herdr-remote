// ---- the mirror's reconciler -------------------------------------------------------------------
//
// Measured in chromium, on every way there is to update text under a selection:
//
//   el.replaceChildren(...)  -> every range inside collapses to (el, 0)
//   node.data = next         -> every range inside collapses to (node, 0)   [spec: replaceData]
//   node.appendData(extra)   -> ranges are untouched
//
// So a node cannot be rewritten without moving what is anchored in it, and what decides whether the
// reader notices is the BLAST RADIUS. `ansiFragment` emits one span per styled RUN and a run spans
// newlines, so a pane with no colour in it is ONE text node holding the whole buffer: a change on
// the last line moved a caret sitting on line 3 to the top of the output. That is the reported bug.
// A touch drag leaves a CARET behind -- and the freeze deliberately ignores carets, because
// freezing on the collapsed range every tap leaves behind would stop the mirror for good -- so the
// tick rebuilt the buffer, the caret came back at (el, 0), and the reader's next drag highlighted
// the FIRST LINE. Collie polls the same mirror every few seconds with no selection code at all,
// because React renders one keyed node per line: a change on line 7 never touches line 3's node.
//
// So the mirror is a line per styled run from here on, and an update is a reconcile:
//   - identical content              -> no DOM at all (most ticks: an idle pane repeats itself)
//   - the buffer scrolled by k lines -> the lines that stayed keep their NODES (mirrorShift)
//   - a line that only grew          -> appendData, the one range-safe mutation
//   - a line rewritten              -> that LINE rebuilds; the rest of the buffer is not touched
//
// The newline between two lines is a text node BETWEEN the spans rather than inside one, so
// `el.textContent` is byte-identical to the old flat render -- doSearch counts offsets in it.

function terminalUrlParts(text) {
  const parts = [];
  const urlPattern = /https?:\/\/[^\s<>"'`]+/gi;
  let cursor = 0;

  for (const match of text.matchAll(urlPattern)) {
    let url = match[0];
    const closingDelimiters = {')': '(', ']': '[', '}': '{'};
    while (url) {
      const last = url.at(-1);
      if (/[.,;:!?]/.test(last)) {
        url = url.slice(0, -1);
        continue;
      }
      const open = closingDelimiters[last];
      if (open && url.split(last).length > url.split(open).length) {
        url = url.slice(0, -1);
        continue;
      }
      break;
    }

    if (match.index > cursor) parts.push({text: text.slice(cursor, match.index)});
    parts.push({text: url, href: url});
    if (url.length < match[0].length) parts.push({text: match[0].slice(url.length)});
    cursor = match.index + match[0].length;
  }

  if (cursor < text.length) parts.push({text: text.slice(cursor)});
  return parts;
}

// Decorate the visible output after ANSI parsing, so a URL split across colour/style sequences is
// still one link. Cloned spans retain every style, while text and href are assigned as properties:
// terminal output can never become markup.
function terminalFragment(input) {
  const source = ansiFragment(input);
  const parts = terminalUrlParts(source.textContent);
  if (!parts.some(part => part.href)) return source;

  const out = document.createDocumentFragment();
  const runs = [...source.childNodes];
  let runIndex = 0, runOffset = 0;
  for (const part of parts) {
    const target = part.href ? document.createElement('a') : out;
    if (part.href) {
      target.href = part.href;
      target.target = '_blank';
      target.rel = 'noopener noreferrer';
    }
    let remaining = part.text.length;
    while (remaining > 0) {
      const run = runs[runIndex];
      const available = run.textContent.length - runOffset;
      const take = Math.min(remaining, available);
      const copy = run.cloneNode(false);
      copy.textContent = run.textContent.slice(runOffset, runOffset + take);
      target.appendChild(copy);
      remaining -= take;
      runOffset += take;
      if (runOffset === run.textContent.length) {
        runIndex += 1;
        runOffset = 0;
      }
    }
    if (part.href) out.appendChild(target);
  }
  return out;
}

function mirrorLineNodes(content) {
  const lines = [];
  let line = document.createElement('span');
  line.className = 'term-line';
  // Snapshotted, because appendChild MOVES a node out of the live NodeList being walked.
  for (const run of [...terminalFragment(content).childNodes]) {
    const text = run.textContent;
    // A run that does not straddle a newline -- almost all of them -- is adopted whole rather than
    // cloned. Measured on a 1000-line coloured buffer, that is the difference between building 5000
    // nodes a tick and 3000.
    if (!text.includes('\n')) { line.appendChild(run); continue; }
    const parts = text.split('\n');
    for (let i = 0; i < parts.length; i++) {
      if (i > 0) {
        lines.push(line);
        line = document.createElement('span');
        line.className = 'term-line';
      }
      if (!parts[i]) continue;
      const piece = run.cloneNode(false);   // same tag, same inline style, no children
      piece.textContent = parts[i];
      line.appendChild(piece);
    }
  }
  lines.push(line);
  return lines;
}

// Same run, meaning the same box with the same styling -- its TEXT is compared separately, because
// text is the part that can sometimes be healed instead of replaced.
function mirrorSameRun(a, b) {
  if (a.nodeName !== b.nodeName
      || (a.nodeType === 1 && a.getAttribute('style') !== b.getAttribute('style'))) return false;
  if (a.nodeName !== 'A') return true;
  const have = [...a.childNodes], want = [...b.childNodes];
  return have.length === want.length && have.every((child, i) => mirrorSameRun(child, want[i]));
}

function mirrorTextNode(node) {
  if (node.nodeType === 3) return node;
  return node.childNodes.length === 1 ? mirrorTextNode(node.firstChild) : null;
}

// One line against what it should say. Returns whether the DOM moved.
function mirrorPatchLine(line, want) {
  const have = [...line.childNodes], next = [...want.childNodes];
  let i = 0, changed = false;
  for (; i < have.length && i < next.length; i++) {
    const a = have[i], b = next[i];
    if (!mirrorSameRun(a, b)) break;
    const was = a.textContent, now = b.textContent;
    if (was === now) continue;
    const text = mirrorTextNode(a);
    if (!text || !now.startsWith(was)) break;
    text.appendData(now.slice(was.length));
    // Link text and its destination grow together as terminal output arrives. Keeping the anchor
    // lets a selection inside it survive; leaving the old href would open a truncated URL.
    if (a.nodeName === 'A') a.href = b.getAttribute('href');
    changed = true;
  }
  if (i < have.length) { line.replaceChildren(...next); return true; }
  for (; i < next.length; i++) { line.appendChild(next[i]); changed = true; }
  return changed;
}

// How far the buffer scrolled between two reads, or 0. A terminal that printed k lines reports the
// same text k rows up, and every line that stayed is a node that can stay with it -- which is the
// only way a range survives a pane that is actually working. Verified in FULL before it is acted on,
// since a wrong k would delete lines that are still on screen, and capped because a jump of more
// than a screenful is a repaint rather than a scroll.
const MIRROR_SHIFT_MAX = 64;
function mirrorShift(have, want) {
  if (have.length !== want.length) return 0;
  for (let k = 1; k <= Math.min(MIRROR_SHIFT_MAX, have.length - 1); k++) {
    let all = true;
    for (let i = 0; i + k < have.length; i++) {
      if (have[i + k] !== want[i]) { all = false; break; }
    }
    if (all) return k;
  }
  return 0;
}

// A caret in a line that is about to be deleted is DROPPED, not left to fall back to (el, 0) --
// which is precisely where the reader's next drag would extend from, highlighting the first line of
// the output. Only ever a caret: a real selection stops the whole patch before it starts.
function mirrorDropCaretIn(node) {
  const sel = window.getSelection && window.getSelection();
  if (!sel || !sel.rangeCount || !sel.isCollapsed) return;
  if (node.contains(sel.anchorNode)) sel.removeAllRanges();
}

// Returns whether anything changed, because the scroll fix-up above -- which pins an unscrolled
// mirror to the bottom -- is only owed on a change.
function mirrorPatch(el, content) {
  if (el.__mirror === content) return false;
  const want = mirrorLineNodes(content);
  const lines = [...el.children];
  // n spans with the n-1 newlines between them is the structure this function builds. Anything else
  // -- the initial "Loading…", a search rewrite, a pane opened before any of this ran -- is built
  // from scratch rather than reconciled against.
  if (el.__mirror == null || !lines.length || el.childNodes.length !== 2 * lines.length - 1) {
    el.__mirror = content;
    const flat = [];
    want.forEach((node, i) => { if (i) flat.push(document.createTextNode('\n')); flat.push(node); });
    el.replaceChildren(...flat);
    return true;
  }
  el.__mirror = content;
  let changed = false;
  const shift = mirrorShift(lines.map(l => l.textContent), want.map(l => l.textContent));
  for (let i = 0; i < shift; i++) {
    const gap = lines[i].nextSibling;          // the newline that followed it
    mirrorDropCaretIn(lines[i]);
    lines[i].remove();
    if (gap && gap.nodeType === 3) gap.remove();
    changed = true;
  }
  lines.splice(0, shift);
  const shared = Math.min(lines.length, want.length);
  for (let i = 0; i < shared; i++) if (mirrorPatchLine(lines[i], want[i])) changed = true;
  for (let i = lines.length - 1; i >= want.length; i--) {
    const gap = lines[i].previousSibling;
    mirrorDropCaretIn(lines[i]);
    lines[i].remove();
    if (gap && gap.nodeType === 3) gap.remove();
    changed = true;
  }
  for (let i = lines.length; i < want.length; i++) {
    if (el.childNodes.length) el.appendChild(document.createTextNode('\n'));
    el.appendChild(want[i]);
    changed = true;
  }
  return changed;
}

// The interval's whole body, named so the rule can be measured rather than read. The read is not
// even sent while a selection is held: it would cost the relay a herdr call -- an SSH round trip
// for a remote host -- to fetch content this tick has already decided it may not render.
function mirrorTick() {
  if (!sessionVisible()) return;
  updateSessionStatus();
  if (sessionView === 'conversation') { conversationTick(); return; }
  // History does not change under you. Once the reader has paged back, the live screen is no
  // longer what is on display, so there is nothing for a tick to keep up to date.
  if (!paneFollowing) return;
  if (selectionInside(document.getElementById('termContent'))) return;
  refreshPane();
}

function closeTerminal() { navClose('terminal', hideTerminal); }

function hideTerminal() {
  resetSessionControls(null);
  activePane = null; clearInterval(refreshInterval);
  invalidateHistory();
  document.getElementById('terminalView').classList.remove('active');
  document.getElementById('agentListView').style.display = '';
}

// The mirror's scrollback window. herdr clamps pane.read at ~1000 lines and doesn't say so --
// 1000, 1500 and 5000 all come back with the same 1000 rows -- so the old 5000 ceiling only ever
// bought bigger requests that fetched nothing new.
const PANE_LINES_BASE = 200, PANE_LINES_STEP = 400, PANE_LINES_MAX = 1000;
let paneLines = PANE_LINES_BASE;

// How deep a scrollback read on the open pane could ever go (relay: `scrollback`, from herdr's
// scroll.max_offset_from_bottom). 0 means herdr retained nothing behind the viewport, so "load
// older" can never return a line it doesn't already have. undefined means an older relay that
// doesn't report it: stay permissive.
//
// This is NOT a way to tell an agent pane from a terminal, and the mirror must not read it as one.
// It used to: an agent TUI runs on the alternate screen, so every agent pane herdr 0.8.0 reported
// answered 0, and the read source was picked off that. On 0.8.2 the number is not even stable for
// one pane: an earlier measurement on this host had 9 of 10 agent panes reporting a ring (151, 434,
// 594, 938, 1262, 1267, 1400, 1662, 8439), a later one had all 9 reporting 0 -- same host, same
// herdr, and w5:p1 is in both sets.
function paneScrollback() {
  const a = paneById(activePane);
  return a ? a.scrollback : undefined;
}
function canLoadMore() {
  if (paneScrollback() === 0) return false;
  return paneLines < PANE_LINES_MAX;
}
// Following the live screen, as opposed to holding a page of scrollback the reader asked for.
// Only this mode auto-refreshes -- see mirrorTick.
let paneFollowing = true;
function refreshPane() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !activePane) return;
  // Every read is `recent`, following or not. `visible` is the rendered grid and nothing else, so
  // following opened the mirror onto one screenful with no history behind it. Priming with a single
  // `recent` read would not have survived either: mirrorPatch reconciles the WHOLE buffer, so the
  // next 3s pass deletes every line the viewport no longer holds.
  //
  // And the per-pane rule this replaces -- `recent` wherever a ring exists -- was a distinction
  // without a difference. Measured on this host (herdr 0.8.2, all 35 live panes, ansi, `recent` 200
  // against `visible` at the pane's own height): all 9 agent panes report no ring, so the two come
  // back BYTE-IDENTICAL. The gap exists only on shell panes holding a ring (+1.4KB to +52KB), and
  // that ring is exactly the scrollback the reader opened the pane to see.
  //
  // Bounded by construction: a follow read is always PANE_LINES_BASE, because loadMore stops the
  // tick before paneLines can grow past it.
  const request = {
    type: 'read_pane',
    pane_id: activePane,
    lines: paneLines,
    format: 'ansi',
    source: 'recent',
  };
  // One extra CLI call on the relay -- one SSH round trip for a remote host -- so it is asked
  // once per terminal, not on the 3s mirror tick. A directory does not identify a shell pane;
  // "zsh" versus "vim" versus an hour-old build does.
  if (shellPane(activePane) && !paneProcess[activePane]) request.process = true;
  ws.send(JSON.stringify(request));
}
// Back to the live screen, and the only way back once the reader has paged into scrollback. The
// refresh button is this rather than a bare re-read, because in held mode a re-read would fetch
// the same page of history again -- a button that visibly does nothing.
function followPane() {
  paneFollowing = true;
  paneLines = PANE_LINES_BASE;
  refreshPane();
}
function loadMore() {
  if (!canLoadMore()) return;
  // A bigger read answers with a DIFFERENT content -- hundreds of lines arriving in FRONT of what
  // is on screen -- so it is the one update mirrorPatch cannot make non-destructively. It waits,
  // like the tick does, rather than collapsing the selection to the top of the output.
  if (selectionInside(document.getElementById('termContent'))) return;
  // Paging back stops the follow. Two reasons, and either alone is enough: the tick would replace
  // the page under the reader on its next pass, and until it did it would be re-fetching whatever
  // paneLines had grown to -- 125.7KB per tick at the 1000-line ceiling, 42KB/s down a tunnel, for
  // output the reader has already scrolled away from.
  paneFollowing = false;
  paneLines = Math.min(paneLines + PANE_LINES_STEP, PANE_LINES_MAX);
  refreshPane();
}

// Every toggle in the session view says whether its panel is open through `aria-pressed`, and the
// CSS fills the chip off that attribute alone. One helper, so a new toggle cannot be added that
// looks pressed without being pressed.
function setPressed(id, on) {
  const el = document.getElementById(id);
  if (el) el.setAttribute('aria-pressed', String(!!on));
}

// Find in output
let searchMatches = [];
let searchIndex = 0;
let originalContent = '';

function toggleSearch() {
  const bar = document.getElementById('termSearch');
  const input = document.getElementById('searchInput');
  if (bar.style.display === 'none') {
    bar.style.display = 'flex';
    setPressed('searchBtn', true);
    input.focus();
  } else {
    hideSearch();
  }
}

function hideSearch() {
  document.getElementById('termSearch').style.display = 'none';
  document.getElementById('searchInput').value = '';
  setPressed('searchBtn', false);
  clearSearch();
}

// Conversation and Terminal are primary views, not navigation overlays.
let sessionView = 'terminal';
const sessionViewChoices = new Map();
const sessionDrafts = new Map();

function sessionVisible() {
  return !!activePane && document.visibilityState !== 'hidden'
    && document.getElementById('terminalView').classList.contains('active');
}

function resetSessionControls(nextPane) {
  const input = document.getElementById('termInput');
  if (activePane) {
    if (input.value) sessionDrafts.set(activePane, input.value);
    else sessionDrafts.delete(activePane);
  }
  input.value = sessionDrafts.get(nextPane) || '';
  if (typeof hideComposerTools === 'function') hideComposerTools();
  if (typeof closeSessionActions === 'function') closeSessionActions();
  if (typeof showDock === 'function') showDock(null);
  if (typeof clearKeyQueue === 'function') clearKeyQueue();
  if (typeof resizeComposer === 'function') resizeComposer();
}
function defaultSessionView(pane) {
  return matchMedia('(max-width: 600px)').matches && pane &&
    ['pi', 'claude'].includes(pane.agent) ? 'conversation' : 'terminal';
}

function setSessionView(view, remember = true) {
  if (!activePane || !['conversation', 'terminal'].includes(view)) return;
  if (remember) sessionViewChoices.set(activePane, view);
  const changed = sessionView !== view;
  sessionView = view;
  document.getElementById('terminalView').dataset.sessionView = view;
  document.getElementById('termHistory').style.display = view === 'conversation' ? 'flex' : 'none';
  document.getElementById('termContent').style.display = view === 'terminal' ? '' : 'none';
  setPressed('conversationModeBtn', view === 'conversation');
  setPressed('terminalModeBtn', view === 'terminal');
  if (view === 'conversation') {
    hideSearch();
    setPressed('searchBtn', document.getElementById('historyFind').style.display !== 'none');
    if (!history_.loaded) loadHistory();
    else if (changed) conversationTick();
  } else {
    setPressed('searchBtn', document.getElementById('termSearch').style.display !== 'none');
    if (changed) refreshPane();
  }
  updateSessionStatus();
}

// Kept for callers on older cached markup. No extra Back entry is created.
function positionHistoryPanel() {}
function toggleHistory() { setSessionView(sessionView === 'conversation' ? 'terminal' : 'conversation'); }
function hideHistory() { document.getElementById('termHistory').style.display = 'none'; }
function toggleSessionSearch() {
  if (sessionView === 'conversation') toggleHistoryFind(); else toggleSearch();
}
function refreshSessionView() {
  if (sessionView === 'conversation') followConversation(); else followPane();
}

function updateSessionStatus() {
  const el = document.getElementById('sessionStatus');
  if (!el) return;
  const pane = paneById(activePane);
  const state = pane && pane.status;
  const connected = ws && ws.readyState === WebSocket.OPEN;
  const label = connected
    ? (state === 'blocked' ? 'Needs you' : state === 'working' ? 'Working' : state === 'done' ? 'Ready' : 'Connected')
    : (ws && ws.readyState === WebSocket.CONNECTING ? 'Connecting…' : 'Offline');
  if (el.textContent !== label) el.textContent = label;
  el.dataset.state = connected ? (state || 'connected') : 'offline';
}

function resetConversation() {
  invalidateHistory();
  history_ = { turns: [], total: 0, hasMore: false, fileTruncated: false,
    tools: history_.tools, loading: false, unavailable: null, loaded: false };
  historyFollowing = true;
  historyScrollTop = 0;
  historyOpen.clear();
  historyNodes.clear();
  closeHistoryFind();
  document.getElementById('historyTitle').textContent = 'Conversation';
  document.getElementById('historyCount').textContent = '';
  document.getElementById('historyContent').replaceChildren(histEdge('Loading conversation…'));
  setPressed('historyToolsBtn', history_.tools);
  updateHistoryStatus();
}

// The conversation, read from the agent's own transcript rather than the terminal. The relay keeps
// the session reference server-side; all we ever send is the pane and a cursor it gave us.
const HISTORY_PAGE = 200;
// `tools: true` by default. A tool call is most of what an agent's turn actually consists of -- on
// the largest session here 674 of 794 turns were tool calls -- so a history that hides them reads
// as a conversation with holes in it. The cost is real and known: tool turns spend page slots and
// characters from the same budget as the prose, so a page reaches less far back. The Tools chip
// turns them off for exactly that case.
let history_ = { turns: [], total: 0, hasMore: false, fileTruncated: false,
                 tools: true, loading: false, unavailable: null, loaded: false };
let historyFollowing = true, historyEpoch = 0, historySerial = 0;
let historyPending = null, historyReload = false, historyScrollTop = 0;
const HISTORY_TIMEOUT = 20000;
const historyNodes = new Map();

function invalidateHistory() {
  historyEpoch++;
  historyReload = false;
  history_.loading = false;
  // Keep the transport slot until its reply/timeout: changing tools or panes must not pile up
  // transcript reads. The epoch makes that reply inert, even after closing and reopening a pane.
}

function finishHistoryRequest() {
  if (historyPending) clearTimeout(historyPending.timer);
  historyPending = null;
  history_.loading = false;
}

function updateHistoryStatus(note) {
  const el = document.getElementById('historyStatus');
  const selected = selectionInside(document.getElementById('historyContent'));
  const held = !historyFollowing || selected || !!document.getElementById('historyFind').value.trim();
  // Background checks are transport activity, not a change in reading state. Keep this live
  // region steady between requests, including its text node (screen readers announce mutations).
  const label = note || (history_.error || (
    !ws || ws.readyState !== WebSocket.OPEN ? 'Offline · reconnecting'
    : history_.loading && !history_.loaded ? 'Loading conversation…'
    : history_.unavailable ? (history_.retryAt ? 'Waiting for transcript · retries automatically' : 'Conversation unavailable')
    : held ? 'Paused · reading earlier turns' : 'Following · checks every 3 seconds'));
  if (el && el.textContent !== label) el.textContent = label;
  const latest = document.getElementById('historyLatestBtn');
  if (latest) latest.hidden = !held && !history_.error;
}

function conversationTick() {
  if (!sessionVisible()) return;
  updateHistoryStatus();
  if (!historyFollowing || history_.error || history_.loading) return;
  if (history_.unavailable && (!history_.retryAt || Date.now() < history_.retryAt)) return;
  if (selectionInside(document.getElementById('historyContent'))) return;
  if (document.getElementById('historyFind').value.trim()) return;
  loadHistory(undefined, true);
}

function loadHistory(before, live = false) {
  if (!sessionVisible() || sessionView !== 'conversation') return;
  if (!ws || ws.readyState !== WebSocket.OPEN) { updateHistoryStatus(); return; }
  if (historyPending) {
    if (historyPending.epoch !== historyEpoch) historyReload = true;
    return;
  }
  if (selectionInside(document.getElementById('historyContent'))) { updateHistoryStatus(); return; }
  if (!before && !live) closeHistoryFind();
  const request = {id: `history-${++historySerial}`, pane: activePane, epoch: historyEpoch,
    tools: history_.tools, before, live};
  historyPending = request;
  history_.loading = true;
  history_.error = null;
  request.timer = setTimeout(() => {
    if (historyPending !== request) return;
    const current = request.epoch === historyEpoch && request.pane === activePane;
    finishHistoryRequest();
    if (current) historyFailure('Transcript request timed out. Retry or open Terminal.');
    if (historyReload) { historyReload = false; loadHistory(); }
  }, HISTORY_TIMEOUT);
  updateHistoryStatus();
  ws.send(JSON.stringify({
    type: 'get_history', pane_id: activePane, limit: HISTORY_PAGE, request_id: request.id,
    include_tools: history_.tools, ...(before ? { before } : {}),
  }));
}

function loadOlderHistory() {
  if (historyPending || !history_.hasMore || !history_.turns.length) return;
  if (selectionInside(document.getElementById('historyContent'))) return;
  historyFollowing = false;
  loadHistory(history_.turns[0].uuid);
}

function followConversation() {
  if (selectionInside(document.getElementById('historyContent'))) {
    updateHistoryStatus('Clear the text selection to follow the latest turns.');
    return;
  }
  if (historyPending && historyPending.before) invalidateHistory();
  historyFollowing = true;
  history_.error = null;
  history_.unavailable = null;
  closeHistoryFind();
  const el = document.getElementById('historyContent');
  el.scrollTop = el.scrollHeight;
  historyScrollTop = el.scrollTop;
  loadHistory();
}

document.getElementById('historyContent').addEventListener('scroll', () => {
  if (!sessionVisible() || sessionView !== 'conversation' || !history_.loaded) return;
  const el = document.getElementById('historyContent');
  // A smaller viewport (keyboard, composer, filter) also increases the bottom gap. Only an
  // upward scroll is the reader asking to hold, not a resize or a sideways code-block pan.
  if (el.scrollTop < historyScrollTop && el.scrollHeight - el.scrollTop - el.clientHeight > 48) historyFollowing = false;
  historyScrollTop = el.scrollTop;
  updateHistoryStatus();
}, {passive: true});
document.addEventListener('selectionchange', () => {
  if (activePane && sessionView === 'conversation') updateHistoryStatus();
});
// Keep a following transcript at the bottom as the keyboard/composer changes the available
// height. ResizeObserver watches the viewport box, not tool contents or individual turns.
new ResizeObserver(() => {
  const el = document.getElementById('historyContent');
  if (!sessionVisible() || sessionView !== 'conversation' || !el.clientHeight || !history_.loaded
      || !historyFollowing || selectionInside(el) || document.getElementById('historyFind').value.trim()) return;
  el.scrollTop = el.scrollHeight;
  historyScrollTop = el.scrollTop;
}).observe(document.getElementById('historyContent'));

// The filter opens IN PLACE of the conversation title, which is what keeps the header one row in
// both states. Closing it clears the needle: a filter that is hiding turns while its input is not
// on screen is a trap, and the panel would look like a conversation with pieces missing.
function toggleHistoryFind() {
  const input = document.getElementById('historyFind');
  const on = input.style.display === 'none';
  input.style.display = on ? '' : 'none';
  document.getElementById('historyTitle').style.display = on ? 'none' : '';
  setPressed('historyFindBtn', on);
  setPressed('searchBtn', on);
  if (on) { input.focus(); return; }
  const had = !!input.value;
  closeHistoryFind();
  if (had) renderHistory();
}

// Back to the title, needle dropped. Also how a fresh page starts: a filter left over from the last
// conversation would hide most of the new one before it had drawn.
function closeHistoryFind() {
  const input = document.getElementById('historyFind');
  input.value = '';
  input.style.display = 'none';
  document.getElementById('historyTitle').style.display = '';
  setPressed('historyFindBtn', false);
  if (sessionView === 'conversation') setPressed('searchBtn', false);
}

function toggleHistoryTools() {
  if (selectionInside(document.getElementById('historyContent'))) return;
  history_.tools = !history_.tools;
  invalidateHistory();
  historyFollowing = true;
  history_.loaded = false;
  history_.unavailable = null;
  history_.error = null;
  setPressed('historyToolsBtn', history_.tools);
  loadHistory();
}

// Merge a page in. `before` pages arrive as the turns immediately older than what we have, so they
// go on the front; anything already held wins, in case a cursor was resolved loosely server-side.
function receiveHistory(msg) {
  const request = historyPending;
  if (!request || msg.request_id !== request.id || msg.pane_id !== request.pane) return;
  finishHistoryRequest();
  if (request.epoch !== historyEpoch || request.pane !== activePane || request.tools !== history_.tools) {
    if (historyReload) { historyReload = false; loadHistory(); }
    return;
  }
  if (!sessionVisible() || sessionView !== 'conversation') return;
  const el = document.getElementById('historyContent');
  if (selectionInside(el) || (!request.before && !historyFollowing)
      || (request.live && document.getElementById('historyFind').value.trim())) {
    updateHistoryStatus();
    return;
  }
  const older = !!request.before;
  history_.loaded = true;
  history_.error = null;
  history_.unavailable = msg.unavailable || null;
  const pane = paneById(activePane);
  const transient = msg.unavailable === 'no-log' || (msg.unavailable === 'no-session'
    && pane && ['pi', 'claude'].includes(pane.agent));
  history_.retryAt = transient ? Date.now() + 15000 : null;
  history_.total = msg.total || 0;
  history_.hasMore = !!msg.has_more;
  history_.fileTruncated = !!msg.file_truncated;
  const incoming = (msg.messages || []).filter(Boolean);
  if (history_.unavailable) history_.turns = [];
  else if (older) {
    const held = new Set(history_.turns.map(t => t.uuid));
    history_.turns = incoming.filter(t => !held.has(t.uuid)).concat(history_.turns);
  } else history_.turns = incoming;
  document.getElementById('historyTitle').textContent = (msg.title || '').trim() || 'Conversation';
  const previousHeight = el.scrollHeight, previousTop = el.scrollTop;
  renderHistory();
  if (older) el.scrollTop = previousTop + (el.scrollHeight - previousHeight);
  else if (historyFollowing) el.scrollTop = el.scrollHeight;
  historyScrollTop = el.scrollTop;
  updateHistoryStatus();
}

function historyFailure(message) {
  history_.error = message;
  if (!history_.turns.length) { history_.unavailable = 'error'; renderHistory(); }
  updateHistoryStatus();
}

function receiveHistoryError(msg) {
  if (msg.scope !== 'get_history') return false;
  const request = historyPending;
  if (!request || request.id !== msg.request_id || request.pane !== msg.pane_id) return true;
  finishHistoryRequest();
  if (request.epoch === historyEpoch && request.pane === activePane) {
    historyFailure(msg.message || 'Could not read the transcript. Retry or open Terminal.');
  }
  if (historyReload) { historyReload = false; loadHistory(); }
  return true;
}

function historyConnectionChanged(connected) {
  finishHistoryRequest();
  historyReload = false;
  historyEpoch++;
  if (connected) {
    history_.error = null;
    if (history_.unavailable === 'error') history_.unavailable = null;
    if (activePane && sessionView === 'conversation') conversationTick();
  }
  updateHistoryStatus();
  updateSessionStatus();
}

// Why the history panel is empty, in the user's terms. Each is an ordinary state, not an error.
const HISTORY_UNAVAILABLE = {
  'no-session': 'This pane names no agent session, so there is no transcript to read.',
  'no-log': 'No transcript file was found for this pane\'s session yet.',
  'unsupported': 'This relay cannot read this agent\'s transcript format yet.',
  'disabled': 'Transcript history is switched off on this relay.',
  'error': 'Could not read the transcript. Retry or open Terminal.',
};

const HISTORY_ROLE_CLASS = { user: 'msg-user', assistant: 'msg-assistant' };

// A tool call the relay could turn into a diff opens on tap. Kept by turn uuid rather than in the
// DOM, because typing in the filter re-renders the whole list on every keystroke and an open diff
// snapping shut under you is worse than the state it saves.
const historyOpen = new Set();

function historyToolNode(turn) {
  const row = document.createElement('details');
  row.className = 'msg tool' + (turn.error ? ' failed' : '');
  const head = document.createElement('summary');
  head.className = 'tool-head';
  const name = document.createElement('span');
  name.className = 'tool-name';
  name.textContent = turn.tool || 'tool';
  head.appendChild(name);
  const target = document.createElement('span');
  target.className = 'tool-target';
  const full = turn.target || turn.text || '';
  // Verbatim -- this is a path or a shell command, and markdown would eat the asterisks in a
  // glob. Paths are shortened here rather than by `direction: rtl`, which does keep the
  // interesting end visible but reorders the punctuation at both ends of a path or a quoted
  // command. The whole value stays in the tooltip.
  target.textContent = shortenPath(full);
  if (target.textContent !== full) target.title = full;
  head.appendChild(target);
  if (turn.error && turn.result) {
    const why = document.createElement('span');
    why.className = 'tool-error';
    why.textContent = turn.result;
    head.appendChild(why);
  }
  if (turn.diff) {
    const stat = document.createElement('span');
    stat.className = 'tool-stat';
    // The counts are the whole change; the body below may be only its head (see diff_clipped).
    const added = document.createElement('span');
    added.className = 'stat-add'; added.textContent = `+${turn.added || 0}`;
    const removed = document.createElement('span');
    removed.className = 'stat-del'; removed.textContent = `\u2212${turn.removed || 0}`;
    stat.append(added, removed);
    head.appendChild(stat);
  }
  row.appendChild(head);
  // Opening details is reading, not following. Pause before the expanded body changes the
  // scroll height; otherwise the next unchanged transcript poll would jump to its bottom.
  head.addEventListener('click', () => {
    if (!row.open) { historyFollowing = false; updateHistoryStatus(); }
  });
  row.open = historyOpen.has(turn.uuid);
  row.addEventListener('toggle', () => {
    if (row.open) historyOpen.add(turn.uuid); else historyOpen.delete(turn.uuid);
  });
  if (!turn.diff) {
    const body = document.createElement('div');
    body.className = 'tool-details';
    body.textContent = [...new Set([turn.target, turn.text, turn.error ? turn.result : ''])]
      .filter(Boolean).join('\n\n');
    row.appendChild(body);
  }
  if (turn.diff) {
    row.appendChild(diffFragment(turn.diff));
    if (turn.diff_clipped) {
      const edge = document.createElement('div');
      edge.className = 'diff-edge';
      edge.textContent = `showing the first ${turn.diff.split('\n').length} lines`;
      row.appendChild(edge);
    }
  }
  return row;
}

// The last few segments of a path, which is the part that identifies the file. Anything without
// a separator (a shell command, a search pattern) is left alone for CSS to ellipsize.
function shortenPath(text) {
  if (!text.includes('/') || text.includes(' ')) return text;
  const parts = text.split('/').filter(Boolean);
  return parts.length > 3 ? '…/' + parts.slice(-3).join('/') : text;
}

const HISTORY_ROLE_LABEL = { user: 'you', assistant: 'agent', note: 'note' };

/** A turn's time, in the reader's own zone.
 *
 *  Claude writes every transcript timestamp in UTC -- all 4,450 sampled rows on this machine end in
 *  `Z` -- and this used to be `ts.slice(11, 16)`, five characters lifted straight out of that
 *  string. So a turn made at 17:08 in UTC+8 was stamped 09:08, silently, and only for a reader
 *  whose zone was not UTC.
 *
 *  The date rides along when the turn is not from today, because paging back is the whole point of
 *  the panel and `09:08` alone cannot say which day it belongs to. A string the platform will not
 *  parse falls back to the old slice: wrong by an offset still beats blank. */
function turnStamp(ts) {
  if (!ts) return null;
  const at = new Date(ts);
  const stamp = document.createElement('span');
  stamp.className = 'msg-time';
  if (isNaN(at.getTime())) {
    // Not a date this platform knows. The old five characters are still the best guess, and an
    // empty one means the string had nothing there at all -- no span rather than a blank one.
    const raw = String(ts).slice(11, 16);
    if (!raw) return null;
    stamp.textContent = raw;
    return stamp;
  }
  const time = at.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const sameDay = at.toDateString() === new Date().toDateString();
  stamp.textContent = sameDay ? time
    : `${at.toLocaleDateString([], {month: 'numeric', day: 'numeric'})} ${time}`;
  // The full local date and time, for the one case the short form cannot answer.
  stamp.title = at.toLocaleString();
  return stamp;
}

function historyTurnNode(turn) {
  const role = turn.role || 'note';
  if (role === 'tool') return historyToolNode(turn);
  const block = document.createElement('div');
  block.className = `msg ${role}`;
  const head = document.createElement('div');
  head.className = `msg-role ${HISTORY_ROLE_CLASS[role] || ''}`;
  head.textContent = HISTORY_ROLE_LABEL[role] || role;
  const stamp = turnStamp(turn.ts);
  if (stamp) head.appendChild(stamp);
  block.appendChild(head);
  const body = document.createElement('div');
  body.className = 'msg-text';
  // Agent prose is markdown, and so is much of what a person pastes into the composer. The
  // renderer builds nodes, so nothing in a transcript can become markup on the way through.
  body.appendChild(mdFragment(turn.text || ''));
  if (turn.truncated) {
    const clip = document.createElement('span');
    clip.className = 'msg-clip';
    clip.textContent = ' …clipped';
    body.appendChild(clip);
  }
  block.appendChild(body);
  return block;
}

function renderHistory() {
  const el = document.getElementById('historyContent');
  const count = document.getElementById('historyCount');
  if (history_.unavailable) {
    const copy = history_.error || HISTORY_UNAVAILABLE[history_.unavailable] || 'Conversation history is unavailable.';
    const edge = histEdge(copy);
    const actions = document.createElement('div');
    actions.className = 'hist-fallback-actions';
    for (const [label, action] of [['Open terminal', () => setSessionView('terminal')], ['Retry', followConversation]]) {
      const button = document.createElement('button');
      button.textContent = label;
      button.addEventListener('click', action);
      actions.appendChild(button);
    }
    edge.appendChild(actions);
    el.replaceChildren(edge);
    count.textContent = '';
    return;
  }
  const needle = document.getElementById('historyFind').value.trim().toLowerCase();
  const shown = needle
    ? history_.turns.filter(t => (t.text || '').toLowerCase().includes(needle))
    : history_.turns;
  // Say what the filter covers: turns not yet paged in are not searched, and pretending otherwise
  // would read as "no matches in this conversation".
  count.textContent = needle
    ? `${shown.length} in ${history_.turns.length} loaded`
    : (history_.total ? `${history_.turns.length} of ${history_.total}` : '');
  if (!history_.turns.length) {
    el.innerHTML = '<div class="hist-edge">This conversation has no turns yet.</div>';
    return;
  }
  const out = [];
  if (history_.hasMore) {
    const more = document.createElement('button');
    more.className = 'hist-more';
    more.textContent = 'Load older turns';
    more.addEventListener('click', loadOlderHistory);
    out.push(more);
  }
  else if (history_.fileTruncated) out.push(histEdge('Older turns were not fetched from this host.'));
  else out.push(histEdge('Start of the conversation.'));
  // Reuse unchanged turns, including expanded details and text nodes. Moving every node into a
  // fragment would still disturb scroll anchors, even with a node cache.
  const retained = new Set(history_.turns.map(t => t.uuid));
  for (const id of historyNodes.keys()) if (!retained.has(id)) historyNodes.delete(id);
  if (shown.length) shown.forEach(turn => {
    const key = JSON.stringify(turn);
    let cached = historyNodes.get(turn.uuid);
    if (!cached || cached.key !== key) {
      if (cached && cached.node.tagName === 'DETAILS' && cached.node.open) historyOpen.add(turn.uuid);
      cached = {key, node: historyTurnNode(turn)};
      cached.node.dataset.turnId = turn.uuid;
      historyNodes.set(turn.uuid, cached);
    }
    out.push(cached.node);
  });
  else out.push(histEdge('No loaded turn matches that.'));
  if (el.firstChild && el.firstChild.outerHTML === out[0].outerHTML) out[0] = el.firstChild;
  let cursor = el.firstChild;
  for (const node of out) {
    if (node === cursor) cursor = cursor.nextSibling;
    else el.insertBefore(node, cursor);
  }
  while (cursor) { const next = cursor.nextSibling; cursor.remove(); cursor = next; }
  updateHistoryStatus();
}

function histEdge(text) {
  const edge = document.createElement('div');
  edge.className = 'hist-edge';
  edge.textContent = text;
  return edge;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// For attribute values, which escapeHtml is not safe for: textContent leaves quotes alone, so a
// name carrying one closes the attribute early. Names are herdr's, and herdr's are the operator's
// -- a tab or agent can be renamed to anything from this very UI.
function escapeAttr(text) {
  return String(text ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function doSearch() {
  const query = document.getElementById('searchInput').value.toLowerCase();
  const el = document.getElementById('termContent');
  const countEl = document.getElementById('searchCount');
  
  // Restore original content first
  if (!originalContent) originalContent = el.innerHTML;
  el.innerHTML = originalContent;
  // Written behind mirrorPatch's back, so its record of what the DOM says is void: the next tick
  // reconciles against the live text instead of trusting a string that predates the marks.
  el.__mirror = null;
  
  if (!query || query.length < 2) {
    countEl.textContent = '';
    searchMatches = [];
    return;
  }
  
  // Find and highlight matches
  const text = el.textContent;
  const regex = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  let match;
  searchMatches = [];
  
  while ((match = regex.exec(text)) !== null) {
    searchMatches.push(match.index);
  }
  
  if (searchMatches.length === 0) {
    countEl.textContent = '0/0';
    return;
  }
  
  // Highlight all matches in HTML
  let html = originalContent;
  const tempDiv = document.createElement('div');
  tempDiv.innerHTML = html;
  const walker = document.createTreeWalker(tempDiv, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  
  let matchIdx = 0;
  textNodes.forEach(node => {
    const nodeText = node.textContent;
    const parts = nodeText.split(regex);
    if (parts.length > 1) {
      const frag = document.createDocumentFragment();
      const matches = nodeText.match(regex) || [];
      parts.forEach((part, i) => {
        frag.appendChild(document.createTextNode(part));
        if (matches[i]) {
          const mark = document.createElement('mark');
          mark.className = matchIdx === searchIndex ? 'search-current' : 'search-highlight';
          mark.textContent = matches[i];
          mark.dataset.matchIndex = matchIdx++;
          frag.appendChild(mark);
        }
      });
      node.parentNode.replaceChild(frag, node);
    }
  });
  
  el.innerHTML = tempDiv.innerHTML;
  searchIndex = 0;
  countEl.textContent = `1/${searchMatches.length}`;
  scrollToMatch();
}

function searchNav(dir) {
  if (!searchMatches.length) return;
  searchIndex = (searchIndex + dir + searchMatches.length) % searchMatches.length;
  document.getElementById('searchCount').textContent = `${searchIndex + 1}/${searchMatches.length}`;
  
  // Update highlight classes
  document.querySelectorAll('.search-highlight, .search-current').forEach((el, i) => {
    el.className = i === searchIndex ? 'search-current' : 'search-highlight';
  });
  scrollToMatch();
}

function scrollToMatch() {
  const current = document.querySelector('.search-current');
  if (current) current.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function clearSearch() {
  const el = document.getElementById('termContent');
  if (originalContent) {
    el.innerHTML = originalContent;
    el.__mirror = null;
    originalContent = '';
  }
  searchMatches = [];
  searchIndex = 0;
  document.getElementById('searchCount').textContent = '';
}

// Long-press context menu
let longPressTimer = null;
let contextTarget = null;

function showContextMenu(e, type, id, name) {
  e.preventDefault();
  const menu = document.getElementById('contextMenu');
  const items = document.getElementById('contextMenuItems');
  contextTarget = { type, id, name };
  
  // Focus first on every kind: it is the one action that answers "take me to it", and it is what
  // the relay can do for a space that holds no agent at all.
  const focusItemHtml = `
      <div class="ctx-item" onclick="focusItem()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>
        Focus in herdr
      </div>`;
  let html = '';
  if (type === 'tab') {
    html = `${focusItemHtml}
      <div class="ctx-item" onclick="renameItem()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
        Rename Tab
      </div>
      <div class="ctx-item danger" onclick="closeItem()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
        Close Tab
      </div>
    `;
  } else if (type === 'workspace') {
    html = focusItemHtml;
  } else if (type === 'agent') {
    html = `${focusItemHtml}
      <div class="ctx-item" onclick="renameItem()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
        Rename Agent
      </div>
      <div class="ctx-item" onclick="interruptAgent()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
        Interrupt (Esc)
      </div>
    `;
  }
  items.innerHTML = html;
  
  // Position menu
  const x = Math.min(e.clientX || e.touches?.[0]?.clientX || 100, window.innerWidth - 180);
  const y = Math.min(e.clientY || e.touches?.[0]?.clientY || 100, window.innerHeight - 120);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.display = 'block';
  navPush('ctxmenu', hideMenu);
  
  if (window.cue) cue('tick');
}

function hideContextMenu() { navClose('ctxmenu', hideMenu); }

function hideMenu() {
  document.getElementById('contextMenu').style.display = 'none';
  contextTarget = null;
}

function renameItem() {
  if (!contextTarget) return;
  const newName = prompt(`Rename ${contextTarget.type}:`, contextTarget.name || '');
  if (newName && ws) {
    if (contextTarget.type === 'tab') {
      const {host, id} = splitKey(contextTarget.id);
      ws.send(JSON.stringify({ type: 'rename_tab', tab_id: id, host, label: newName }));
    } else if (contextTarget.type === 'agent') {
      // `herdr agent rename`, which sets the label every client shows. The old path typed
      // `/rename <name>` into the pane, and claude has no such command -- it landed in the
      // composer as literal text for the operator to clear by hand.
      ws.send(JSON.stringify({ type: 'rename_agent', pane_id: contextTarget.id, label: newName }));
    }
    if (window.cue) cue('success');
  }
  hideContextMenu();
}

function closeItem() {
  if (!contextTarget || contextTarget.type !== 'tab') return;
  if (confirm(`Close tab "${contextTarget.name || contextTarget.id}"?`)) {
    const {host, id} = splitKey(contextTarget.id);
    if (ws) ws.send(JSON.stringify({ type: 'close_tab', tab_id: id, host }));
    if (window.cue) cue('droplet');
  }
  hideContextMenu();
}

function interruptAgent() {
  if (!contextTarget || contextTarget.type !== 'agent') return;
  if (ws) ws.send(JSON.stringify({ type: 'send_keys', pane_id: contextTarget.id, keys: ['Escape'] }));
  if (window.cue) cue('toggle');
  hideContextMenu();
}

// Close context menu on click outside
document.addEventListener('click', (e) => {
  if (!e.target.closest('#contextMenu')) hideContextMenu();
});

// Long press handler for touch
function setupLongPress(el, type, id, name) {
  el.addEventListener('touchstart', (e) => {
    longPressTimer = setTimeout(() => showContextMenu(e, type, id, name), 500);
  }, { passive: true });
  el.addEventListener('touchend', () => clearTimeout(longPressTimer));
  el.addEventListener('touchmove', () => clearTimeout(longPressTimer));
  // Right-click for desktop
  el.addEventListener('contextmenu', (e) => showContextMenu(e, type, id, name));
}

function sendText() {
  if(imeComposing)return;
  const i=document.getElementById('termInput');
  if(!i.value||!ws||!activePane)return;
  const agent=agents.find(a=>a.pane_id===activePane);
  if (!agent && shellPane(activePane)) {
    // One message rather than send_text + Enter: the relay runs both halves and audits it as
    // respond_shell, which is the line that says a command was run rather than text typed.
    ws.send(JSON.stringify({type:'respond',pane_id:activePane,text:i.value}));
    i.value='';
    if (typeof resizeComposer === 'function') resizeComposer();
    setTimeout(mirrorTick,400);
    return;
  }
  if(agent&&agent.status==='blocked') {
    ws.send(JSON.stringify({type:'respond',pane_id:activePane,prompt_id:agent.prompt_id,text:i.value}));
  } else {
    ws.send(JSON.stringify({type:'send_text',pane_id:activePane,text:i.value}));
    ws.send(JSON.stringify({type:'send_keys',pane_id:activePane,keys:['Enter']}));
  }
  i.value='';
  if (typeof resizeComposer === 'function') resizeComposer();
  setTimeout(mirrorTick,500);
}
function sendKey(k){if(!ws||!activePane)return;ws.send(JSON.stringify({type:'send_keys',pane_id:activePane,keys:[k]}));setTimeout(mirrorTick,300);}
function sendKeys(k){if(!ws||!activePane)return;ws.send(JSON.stringify({type:'send_keys',pane_id:activePane,keys:k}));setTimeout(mirrorTick,300);}

