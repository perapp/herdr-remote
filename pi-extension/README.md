# Pi runtime command bridge

`commands.ts` exports the commands actually loaded in a running Pi session through
`pi.getCommands()`. It does not scan/evaluate extension source, register a command or
tool, read conversation content, or send anything over the network. Built-in Pi
commands are not returned by that API; the web client supplies its own fallback list.

## Enable

Requires a Pi version exposing `ExtensionAPI.getCommands()` (verified with Pi 0.87.1).
Try it for one invocation, from the repository root:

```sh
pi --extension ./pi-extension/commands.ts
```

For all sessions, add the **absolute path** to `pi-extension/commands.ts` to the
`extensions` array in `~/.pi/agent/settings.json`, preserving existing entries. Then
run `/reload` in each already-running Pi session. Alternatively copy the file into
`~/.pi/agent/extensions/herdr-commands.ts`. Use only one installation method.

The bridge needs an allocated absolute `.jsonl` session path with an existing parent
directory, not a saved transcript. Fresh sessions are discoverable at startup/reload
before their first conversation message; the bridge never creates or modifies the
transcript or creates session directories. Sessions without an allocated path become
discoverable within 15 seconds after allocation. Custom session directories must also
be allowed by the relay's comma-separated `HERDR_PI_ROOTS` setting.

Set `HERDR_COMMAND_DISCOVERY=0` in the relay environment to refuse discovery; set it
in Pi's environment before launch to disable publication as well. Remove the
extension configuration and `/reload` to stop publishing without changing Pi's own
commands. A hard-killed process's last catalog expires after 75 seconds.

## Contract and privacy

Next to the canonical session path the bridge atomically replaces
`<session-file>.herdr-commands.json`, mode `0600`, at startup/reload/session switch
and every 15 seconds. Initial publication is deferred so later `session_start`
handlers can register commands; asynchronous registrations appear on the next
heartbeat. Writes are synchronous, small and serialized. Timers are unreferenced
and cleared at shutdown/switch.

The JSON schema is:

```json
{
  "schemaVersion": 1,
  "sessionFile": "/absolute/canonical/session.jsonl",
  "pid": 1234,
  "updatedAt": 1780000000000,
  "commands": [{"name": "review", "description": "Review changes", "source": "template"}]
}
```

`updatedAt` is Unix epoch milliseconds. Sources are `extension`, `template`
(mapped from Pi's `prompt`), and `skill`; skill names already include `skill:`.
Names match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`, descriptions are at most 200
characters, names are deduplicated, and catalogs stop at 500 commands or 128 KiB.
Only command metadata and the session identity above are stored: no tokens,
implementation paths, tool definitions, or session messages. Command descriptions
are extension-provided metadata and should not contain secrets.

On shutdown/switch the publisher invalidates its own last file inode with
`updatedAt: 0` and an empty command list. It retains that inode's descriptor, so a
new publisher's atomic replacement cannot be deleted or invalidated, even across
reloads sharing a PID. The small stale sidecar may remain on disk. Filesystem/API
failures are swallowed without logging paths or exception details.

The relay accepts authenticated `get_commands` requests with a known `pane_id`
and optional nonempty string `request_id` (maximum 128 characters). It looks up
the session **only from its own pane state**, validates an absolute `.jsonl` path
against `transcript.PI_ROOTS`, and requires its real parent directory to exist inside
an approved root. Traversal components, missing parents, nonregular session targets,
and symlink escapes are rejected. An absent final transcript is allowed; existing
session links must resolve to regular files inside an approved root. It rejects
symlink/nonregular/oversized sidecars, validates schema and session identity, and
enforces a 75-second TTL (5-second future-clock tolerance).
It returns:

```json
{"type":"commands","pane_id":"w0:p1","request_id":"commands-1","commands":[{"cmd":"/review","desc":"Review changes","source":"template","common":true}]}
```

Unavailable replies have an empty `commands` list and `unavailable` set to
`no-session`, `no-catalog`, `stale`, `unsupported`, `remote`, `disabled`, or `error`.
Request errors use `type: "error"`, `scope: "get_commands"`, and valid correlation
fields. Session paths and process IDs never cross this WebSocket interface.

**SSH panes are deliberately unavailable (`remote`).** No remote command discovery
or static source scanning is attempted. The browser retains built-in commands and
browser-configured saved shortcuts when runtime discovery is unavailable. A relay
running locally on the remote machine can read that machine's catalogs normally.

Discovery is metadata, not an execution guarantee: commands may change after a
refresh, and commands opening terminal dialogs still need interaction with Pi.

## Tests

No new dependencies or running Pi/relay process are needed:

```sh
python3 -m unittest tests.test_command_catalog tests.test_pi_command_extension
node --experimental-strip-types --test tests/test_pi_commands.mjs
```

The Node tests use a stub Pi API and controlled timers with temporary session files;
Node 22.6+ is needed for TypeScript stripping. The Python test wrapper runs them
when that support is available, including under the normal unittest discovery.
