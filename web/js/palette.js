// --- Command Palette ---
const COMMANDS = {
  claude: [
    {cmd: '/compact', desc: 'Summarize context to free tokens', common: true},
    {cmd: '/clear', desc: 'Start fresh conversation', common: true, danger: true},
    {cmd: '/model', desc: 'Switch model', common: true},
    {cmd: '/status', desc: 'Show version and connectivity', common: true},
    {cmd: '/context', desc: 'Visualize context-window usage', common: true},
    {cmd: '/review', desc: 'Review a pull request', common: true},
    {cmd: '/diff', desc: 'Show uncommitted changes', common: false},
    {cmd: '/resume', desc: 'Resume previous conversation', common: false},
    {cmd: '/help', desc: 'Show all commands', common: false},
  ],
  codex: [
    {cmd: '/compact', desc: 'Summarize history to free context', common: true},
    {cmd: '/clear', desc: 'Reset and start new chat', common: true, danger: true},
    {cmd: '/diff', desc: 'Show git diff of working tree', common: true},
    {cmd: '/model', desc: 'Switch model', common: true},
    {cmd: '/status', desc: 'Show model and token usage', common: true},
    {cmd: '/review', desc: 'Code review working tree', common: true},
    {cmd: '/mention', desc: 'Attach files to context', common: false},
    {cmd: '/plan', desc: 'Enter plan mode', common: false},
  ],
  pi: [
    {cmd: '/compact', desc: 'Compact context', common: true},
    {cmd: '/new', desc: 'Start new session', common: true, danger: true},
    {cmd: '/model', desc: 'Switch model', common: true},
    {cmd: '/session', desc: 'Show session info', common: true},
    {cmd: '/tree', desc: 'Jump to earlier point', common: true},
    {cmd: '/share', desc: 'Share session as gist', common: true},
    {cmd: '/copy', desc: 'Copy last response', common: false},
    {cmd: '/reload', desc: 'Reload extensions and skills', common: false},
  ],
  opencode: [
    {cmd: '/compact', desc: 'Compact current session', common: true},
    {cmd: '/new', desc: 'Start new session', common: true, danger: true},
    {cmd: '/models', desc: 'List and switch models', common: true},
    {cmd: '/undo', desc: 'Undo last turn and revert', common: true, danger: true},
    {cmd: '/share', desc: 'Share session', common: true},
    {cmd: '/diff', desc: 'Show working changes', common: false},
    {cmd: '/export', desc: 'Export to Markdown', common: false},
  ],
};

// Loaded commands come from the agent, never from evaluating its extension files in the relay.
// Saved shortcuts are explicitly user-authored fallbacks and remain available offline.
const COMMAND_SHORTCUTS_KEY = 'herdr_command_shortcuts_v1';
const COMMAND_NAME = /^\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
let commandSerial = 0, commandRequest = null, commandCatalog = [], commandPane = null;
let commandAgent = '', commandSocket = null;

function commandAgentKey() {
  const key = (agents.find(a => a.pane_id === activePane)?.agent || '').toLowerCase();
  if (key.startsWith('claude')) return 'claude';
  if (key.startsWith('codex')) return 'codex';
  if (key.startsWith('pi') || key === 'kiro') return 'pi';
  if (key.startsWith('opencode')) return 'opencode';
  return key;
}

function savedCommandShortcuts(agent = commandAgentKey()) {
  try {
    const all = JSON.parse(localStorage.getItem(COMMAND_SHORTCUTS_KEY) || '{}');
    if (!Array.isArray(all[agent])) return [];
    return all[agent].filter(c => c && typeof c.cmd === 'string' && COMMAND_NAME.test(c.cmd) && typeof c.desc === 'string')
      .slice(0, 100).map(c => ({cmd: c.cmd, desc: c.desc.slice(0, 200), common: true, source: 'shortcut'}));
  } catch (e) { return []; }
}

function getAgentCommands() {
  const key = commandAgentKey();
  if (!key) return [];
  const merged = new Map((COMMANDS[key] || []).map(c => [c.cmd, {...c, source: 'built-in fallback'}]));
  for (const c of savedCommandShortcuts(key)) merged.set(c.cmd, c);
  if (commandPane === activePane && commandAgent === key && commandSocket === ws) {
    for (const c of commandCatalog) merged.set(c.cmd, {...merged.get(c.cmd), ...c, common: true});
  }
  return [...merged.values()];
}

function setCommandStatus(text) {
  const el = document.getElementById('cmdStatus');
  if (el.textContent !== text) el.textContent = text;
}

function clearCommandRequest() {
  if (commandRequest) clearTimeout(commandRequest.timer);
  commandRequest = null;
}

function requestAgentCommands() {
  clearCommandRequest();
  if (!commandAgent || !activePane) { setCommandStatus('This pane has no agent commands.'); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setCommandStatus('Offline · showing built-ins and saved shortcuts.'); return;
  }
  const request = {id: `commands-${++commandSerial}`, pane: activePane, socket: ws};
  commandRequest = request;
  setCommandStatus('Discovering loaded commands…');
  request.timer = setTimeout(() => {
    if (commandRequest !== request) return;
    commandRequest = null;
    setCommandStatus('Discovery unavailable · showing built-ins and saved shortcuts.');
  }, 8000);
  ws.send(JSON.stringify({type: 'get_commands', pane_id: request.pane, request_id: request.id}));
}

function receiveCommands(msg) {
  const request = commandRequest;
  if (!request || msg.request_id !== request.id || msg.pane_id !== request.pane
      || request.pane !== activePane || request.socket !== ws) return;
  clearCommandRequest();
  const allowedSources = new Set(['extension', 'template', 'skill']);
  commandCatalog = (Array.isArray(msg.commands) ? msg.commands : []).filter(c =>
    c && typeof c.cmd === 'string' && COMMAND_NAME.test(c.cmd) && typeof c.desc === 'string' && allowedSources.has(c.source))
    .slice(0, 500).map(c => ({cmd: c.cmd, desc: c.desc.slice(0, 200), source: c.source}));
  const reasons = {
    'no-session': 'Session metadata is unavailable for this pane.',
    'no-catalog': 'No Pi command catalog yet. Check the discovery bridge and run /reload in Pi.',
    stale: 'Pi command catalog expired. Run /reload to reconnect the discovery bridge.',
    unsupported: 'Live discovery is not supported for this agent.',
    remote: 'Live discovery is unavailable on this remote host.',
    disabled: 'Command discovery is disabled on this relay.',
  };
  if (msg.type === 'error' || msg.unavailable) {
    commandCatalog = [];
    setCommandStatus((reasons[msg.unavailable] || 'Command discovery failed.') + ' Built-ins and saved shortcuts still work.');
  } else {
    setCommandStatus(`${commandCatalog.length} loaded commands · plus built-ins and saved shortcuts`);
  }
  filterCommands();
}

function openCommandPalette() {
  if(window.cue) cue('page');
  commandPane = activePane;
  commandAgent = commandAgentKey();
  commandSocket = ws;
  commandCatalog = [];
  document.getElementById('cmdPalette').style.display = '';
  navPush('palette', hidePalette);
  document.getElementById('cmdSearch').value = '';
  document.getElementById('cmdShortcutEditor').open = false;
  document.getElementById('cmdShortcutLabel').textContent = `Shortcuts for ${commandAgent || 'this agent'} in this browser`;
  document.getElementById('cmdShortcuts').value = savedCommandShortcuts().map(c => `${c.cmd} | ${c.desc}`).join('\n');
  document.getElementById('cmdShortcutStatus').textContent = '';
  filterCommands();
  requestAgentCommands();
  document.getElementById('cmdSearch').focus();
}
function closePalette() { navClose('palette', hidePalette); }
function hidePalette() {
  clearCommandRequest();
  commandCatalog = [];
  commandPane = null;
  document.getElementById('cmdPalette').style.display = 'none';
}

function saveCommandShortcuts() {
  const status = document.getElementById('cmdShortcutStatus');
  if (!commandAgent || commandPane !== activePane) { status.textContent = 'Open Commands for an agent first.'; return; }
  const rows = document.getElementById('cmdShortcuts').value.split('\n').map(s => s.trim()).filter(Boolean);
  if (rows.length > 100) { status.textContent = 'Use at most 100 shortcuts.'; return; }
  const parsed = [];
  for (const row of rows) {
    const [name, ...description] = row.split('|');
    const cmd = name.trim(), desc = description.join('|').trim();
    if (!COMMAND_NAME.test(cmd) || desc.length > 200) {
      status.textContent = 'Use /command | description, one per line (description: 200 characters max).'; return;
    }
    parsed.push({cmd, desc});
  }
  try {
    let all;
    try { all = JSON.parse(localStorage.getItem(COMMAND_SHORTCUTS_KEY) || '{}'); } catch (e) { all = {}; }
    if (!all || typeof all !== 'object' || Array.isArray(all)) all = {};
    Object.defineProperty(all, commandAgent, {value: parsed, enumerable: true, configurable: true});
    localStorage.setItem(COMMAND_SHORTCUTS_KEY, JSON.stringify(all));
  } catch (e) { status.textContent = 'Could not save shortcuts in this browser.'; return; }
  status.textContent = 'Saved.';
  filterCommands();
}

function filterCommands() {
  const q = document.getElementById('cmdSearch').value.trim().toLowerCase();
  const cmds = getAgentCommands();
  const filtered = q ? cmds.filter(c => c.cmd.toLowerCase().includes(q) || c.desc.toLowerCase().includes(q))
    : cmds.filter(c => c.common);
  const el = document.getElementById('cmdList');
  el.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement('p');
    empty.className = 'cmd-empty'; empty.textContent = 'No commands match. You can add a saved shortcut below.';
    el.appendChild(empty); return;
  }
  for (const c of filtered) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'cmd-item';
    if (c.danger) button.classList.add('danger');
    const name = document.createElement('span'); name.className = 'cmd-name'; name.textContent = c.cmd;
    const desc = document.createElement('span'); desc.className = 'cmd-desc'; desc.textContent = c.desc;
    const source = document.createElement('span'); source.className = 'cmd-source'; source.textContent = c.source;
    button.append(name, desc, source);
    const pane = activePane;
    button.addEventListener('click', () => { if (activePane === pane) runCommand(c.cmd); });
    el.appendChild(button);
  }
}

function runCommand(cmd) {
  if (typeof cmd !== 'string' || !COMMAND_NAME.test(cmd) || commandPane !== activePane || commandSocket !== ws) return;
  if (!ws || ws.readyState !== WebSocket.OPEN || !activePane) {
    setCommandStatus('Not connected. Reconnect before running a command.'); return;
  }
  closePalette();
  ws.send(JSON.stringify({type:'send_text', pane_id: activePane, text: cmd}));
  ws.send(JSON.stringify({type:'send_keys', pane_id: activePane, keys:['Enter']}));
  setTimeout(mirrorTick, 500);
}

// A native button handles Enter/Space. The dialog owns Escape and Tab while it is open.
document.getElementById('cmdPalette').addEventListener('keydown', event => {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePalette(); return; }
  if (event.key !== 'Tab') return;
  const controls = [...document.querySelectorAll('#cmdPalette button, #cmdPalette input, #cmdPalette textarea, #cmdPalette summary')]
    .filter(el => !el.disabled && el.getClientRects().length);
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});
