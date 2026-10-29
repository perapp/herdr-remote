#!/usr/bin/env python3
"""Reader for an agent's own conversation transcript.

Why files and not the terminal: an agent TUI runs on the alternate screen, so herdr retains no
scrollback for it (`scroll.max_offset_from_bottom` is 0 on every agent pane), and the one path that
does reach older rows -- a `recent` + text read, which walks the agent's own mouse-scroll interface
-- costs ~31ms per line, only works while the agent is idle, and visibly scrolls the operator's
terminal. The agent writes its own transcript anyway, with real message boundaries and timestamps,
so that is what we read.

Claude, Codex, pi and kimi-code transcripts are all understood. Adding another harness means adding a
locate+parse pair, one line in HARNESSES (plus PATH_HARNESSES if it hands over a file path rather
than an id) and, when its session ids are not bare uuids, one line in ID_VALUE_RES -- nothing else
in here or in the relay is harness-specific.
"""
import difflib
from datetime import datetime, timezone
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading

# The session ref herdr reports is `{kind: "id", value: "<uuid>"}`. Everything downstream of this
# regex participates in a filesystem path or a remote shell word, so nothing that isn't a uuid is
# ever allowed past it.
UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# kimi-code names its sessions `session_<uuid>`, and the whole value is the session DIRECTORY's
# name under ~/.kimi-code/sessions -- the prefix is as load-bearing as the hex, so it is validated
# as part of the value rather than stripped, exactly as constrained as a bare uuid.
SESSION_ID_RE = re.compile(r"\Asession_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                           r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]")

COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
SHELL_WRAP_RE = re.compile(
    r"</?(?:local-command-stdout|local-command-stderr|bash-input|bash-stdout|bash-stderr)>"
)

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
# Characters of turn text one page may carry, plus a rough per-turn JSON overhead.
PAGE_TEXT_BUDGET = 64 * 1024
TURN_OVERHEAD = 120
TEXT_LIMIT = 4000
TOOL_TEXT_LIMIT = 200
CACHE_SIZE = 4
REMOTE_TIMEOUT = 25


def _int_env(name, default):
    """An int from the environment, falling back rather than refusing to start the relay."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _roots_env(name, default):
    raw = os.environ.get(name, "")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or default


ENABLED = os.environ.get("HERDR_TRANSCRIPT", "1").strip().lower() not in {"0", "false", "no", "off"}
LOCAL_ROOTS = _roots_env("HERDR_CLAUDE_ROOTS", [os.path.expanduser("~/.claude/projects")])
CODEX_ROOTS = _roots_env("HERDR_CODEX_ROOTS", [os.path.join(
    os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"), "sessions")])
# pi writes one JSONL per session under here; its session ref hands over the absolute path
# (kind "path"), so these roots are a containment check rather than a search space.
PI_ROOTS = _roots_env("HERDR_PI_ROOTS", [os.path.expanduser("~/.pi/agent/sessions")])
# kimi-code writes one directory per session (`<root>/<workspace>/session_<uuid>/agents/main/`);
# its session ref hands over the `session_<uuid>` leaf (kind "id"), which locate globs for.
KIMI_ROOTS = _roots_env("HERDR_KIMI_ROOTS", [os.path.expanduser("~/.kimi-code/sessions")])
# Remote roots stay unexpanded: they are shell words for the remote host, whose $HOME is not ours.
REMOTE_ROOTS = _roots_env("HERDR_REMOTE_CLAUDE_ROOTS", ["$HOME/.claude/projects"])
REMOTE_CODEX_ROOTS = _roots_env("HERDR_REMOTE_CODEX_ROOTS", ["$HOME/.codex/sessions"])
REMOTE_PI_ROOTS = _roots_env("HERDR_REMOTE_PI_ROOTS", ["~/.pi/agent/sessions"])
MAX_BYTES = _int_env("HERDR_TRANSCRIPT_MAX_BYTES", 64 * 1024 * 1024)
TAIL_BYTES = _int_env("HERDR_TRANSCRIPT_TAIL_BYTES", 8 * 1024 * 1024)
# Remote is stingier than local: the bytes cross a network, and the biggest transcript on this
# machine is 33MB. A tail means remote history is recency-bounded, which the payload says out loud
# through `file_truncated` so the UI can too.
REMOTE_TAIL_BYTES = _int_env("HERDR_TRANSCRIPT_REMOTE_TAIL_BYTES", 4 * 1024 * 1024)


# ---------------------------------------------------------------------------- text helpers


def clip(text, limit):
    """Strip ANSI, normalise newlines, and cap length. Returns (text, truncated)."""
    clean = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(clean) <= limit:
        return clean, False
    return clean[:limit], True


def _first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


# ---------------------------------------------------------------------------- diffs
#
# A file-editing tool call carries both sides of the change in its own input, so the diff is
# already in the transcript -- it just never survived parsing. Measured over the 9,826 tool_use
# blocks in the 25 largest transcripts on this machine: Bash 6109, Edit 1840, Read 943, Write 350,
# everything else under 200. Edit is the file-modifying tool that matters, and the only one whose
# input holds the before and the after verbatim.
#
# Ceilings: across those 1,840 Edit calls the diff runs 10 lines / 494 chars at the median, 40
# lines / 1.9KB at p90, and 321 lines / 14KB at the top. 40 lines shows nine edits in ten whole
# and keeps the tail from crowding out the rest of the page -- a diff spends from the same
# PAGE_TEXT_BUDGET the prose does. A Write is the other shape: one side only, median 90 lines and
# up to 1,529, so it is always the head of the file rather than all of it.
DIFF_MAX_LINES = 40
DIFF_MAX_CHARS = 2000
DIFF_CONTEXT = 3
# The gap between two hunks. Not "@@ -1,4 +1,6 @@": see _diff_lines.
DIFF_GAP = "..."


def _diff_lines(old, new):
    """`-`/`+`/context lines for one before/after pair.

    No `@@` header and no line numbers. Edit's old_string and new_string are FRAGMENTS of a file,
    not the file, so every number difflib prints is relative to the fragment and would not match
    the editor the reader is about to open. A jump between hunks becomes a bare `...` row instead,
    which is the only thing in that header a reader actually needs.

    The two header lines are skipped by position, not by prefix: a removed line whose own text
    starts with `--` renders as `---...` and a prefix test would silently eat it.
    """
    out = []
    for index, line in enumerate(difflib.unified_diff(
            old.splitlines(), new.splitlines(), n=DIFF_CONTEXT, lineterm="")):
        if index < 2:
            continue  # the `--- ` / `+++ ` pair difflib always emits first
        if line.startswith("@@"):
            if out:
                out.append(DIFF_GAP)
            continue
        out.append(line)
    return out


def _tool_diff(name, args):
    """(text, added, removed, clipped) for a tool call that describes a file change, or None."""
    if not isinstance(args, dict):
        return None
    if name == "Edit" and isinstance(args.get("old_string"), str):
        lines = _diff_lines(args["old_string"], args.get("new_string") or "")
    elif name == "MultiEdit" and isinstance(args.get("edits"), list):
        lines = []
        for edit in args["edits"]:
            if not isinstance(edit, dict) or not isinstance(edit.get("old_string"), str):
                continue
            if lines:
                lines.append(DIFF_GAP)
            lines.extend(_diff_lines(edit["old_string"], edit.get("new_string") or ""))
    elif name == "Write" and isinstance(args.get("content"), str):
        # A whole new file (or a whole replaced one -- the input does not say which, and claiming
        # either would be a guess). Every line is an addition.
        lines = ["+" + line for line in args["content"].splitlines()]
    else:
        return None
    if not lines:
        return None
    # Counted before clipping, so the header stays true when the body is cut.
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    clipped = False
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES]
        clipped = True
    text = "\n".join(lines)
    if len(text) > DIFF_MAX_CHARS:
        text = text[:DIFF_MAX_CHARS]
        clipped = True
    return text, added, removed, clipped


# ---------------------------------------------------------------------------- claude parser
#
# Row-type disposition, from the actual distribution in a 2747-row session on this machine
# (1036 assistant, 724 user, 190 each of mode/permission-mode/ai-title/last-prompt, 128 attachment,
# 74 file-history-*, 17 system, 8 misc):
#
#   assistant  -> text blocks become `assistant`, tool_use blocks become a one-line `tool` turn,
#                 thinking blocks are dropped. An isApiErrorMessage row is a `note`, not the agent
#                 talking.
#   user       -> see _parse_user: 683 of those 724 rows are tool_result traffic that folds into
#                 the tool turn it answers; the interesting minority is a few dozen real messages.
#   system     -> only compact_boundary and away_summary carry content a reader wants; the rest is
#                 timing metadata (turn_duration) or hook noise.
#   ai-title   -> not a turn; the last one wins as the session title.
#   everything else -> dropped. An unknown `type` is dropped rather than raised, so a format drift
#                 in claude costs a few turns instead of the whole panel.


def _turn(row, role, text, index, limit=TEXT_LIMIT):
    uuid = row.get("uuid")
    uuid = uuid if isinstance(uuid, str) else ""
    body, truncated = clip(text, limit)
    return {
        "uuid": uuid if index == 0 else f"{uuid}#{index}",
        "role": role,
        "text": body,
        "ts": row.get("timestamp") or "",
        "truncated": truncated,
    }


# The argument that says what a call was about, in the order we would rather have it. Read off the
# real input shapes: Bash carries `command`, Edit/Read/Write `file_path`, Grep `pattern`, and the
# rest fall back to their own JSON.
TOOL_TARGET_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "description",
                    "prompt", "notebook_path", "skill")


def _tool_target(args):
    """The one argument worth showing next to the tool's name, flattened to a single line."""
    if not isinstance(args, dict):
        return ""
    for key in TOOL_TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    try:
        return " ".join(json.dumps(args, ensure_ascii=False).split())
    except (TypeError, ValueError):
        return ""


def _tool_summary(block):
    name = block.get("name")
    name = name if isinstance(name, str) and name else "tool"
    detail = _tool_target(block.get("input"))
    return f"{name}({detail})" if detail else name


def _annotate_tool(turn, block):
    """Structured fields beside the one-line summary, so a client can render the call instead of
    re-parsing the sentence -- and so a file edit can be shown as a diff at all.

    `text` is left exactly as it was: the macOS, iOS, Windows and TUI clients render that string,
    and none of them knows about these fields.
    """
    name = block.get("name")
    turn["tool"] = name if isinstance(name, str) and name else "tool"
    args = block.get("input")
    target = _tool_target(args)
    if target:
        turn["target"] = target[:TOOL_TEXT_LIMIT]
    diff = _tool_diff(turn["tool"], args)
    if diff:
        turn["diff"], turn["added"], turn["removed"], clipped = diff
        if clipped:
            turn["diff_clipped"] = True


def _fold_tool_result(block, tool_turns):
    """Attach a tool_result to the tool_use turn it answers instead of making it its own turn."""
    turn = tool_turns.get(block.get("tool_use_id"))
    if turn is None:
        return
    body = block.get("content")
    if isinstance(body, list):
        body = "\n".join(
            piece.get("text", "") for piece in body
            if isinstance(piece, dict) and piece.get("type") == "text"
        )
    if not isinstance(body, str):
        body = ""
    marker = "!" if block.get("is_error") else "→"
    head = _first_line(body)
    if block.get("is_error"):
        turn["error"] = True
        # Duplicated from `text` on purpose, and only on the failures: a client showing the reason
        # a call failed should not have to split the summary sentence back apart to find it.
        turn["result"] = head[:TOOL_TEXT_LIMIT]
    if not head and not block.get("is_error"):
        return
    turn["text"], turn["truncated"] = clip(f"{turn['text']} {marker} {head or 'error'}", TOOL_TEXT_LIMIT)


def _parse_assistant(row, turns, tool_turns):
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return
    role = "note" if row.get("isApiErrorMessage") else "assistant"
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                turns.append(_turn(row, role, text, index))
        elif kind == "tool_use":
            turn = _turn(row, "tool", _tool_summary(block), index, limit=TOOL_TEXT_LIMIT)
            _annotate_tool(turn, block)
            turns.append(turn)
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                tool_turns[tool_id] = turn
        # thinking / redacted_thinking: deliberately dropped -- it is not part of the conversation
        # a person is scrolling back through, and it is the bulk of the bytes.


def classify_user_text(text):
    """(role, text) for a string-content user row, or (None, "") when the row is not a turn.

    Claude wraps a lot of machinery in the user channel. Rows flagged isMeta are handled by the
    caller; these are the tagged envelopes that carry no flag.
    """
    stripped = text.strip()
    # A slash command. The tags come in either order (<command-message> can precede
    # <command-name>), so search rather than test the prefix.
    match = COMMAND_NAME_RE.search(stripped)
    if match:
        name = match.group(1).strip()
        args_match = COMMAND_ARGS_RE.search(stripped)
        args = args_match.group(1).strip() if args_match else ""
        rendered = f"{name} {args}".strip()
        return ("user", rendered) if rendered else (None, "")
    if stripped.startswith(("<local-command-stdout>", "<local-command-stderr>",
                            "<bash-stdout>", "<bash-stderr>")):
        body = SHELL_WRAP_RE.sub("", stripped).strip()
        return ("note", body) if body else (None, "")
    if stripped.startswith("<bash-input>"):
        body = SHELL_WRAP_RE.sub("", stripped).strip()
        return ("user", f"! {body}") if body else (None, "")
    if stripped.startswith("<task-notification>"):
        match = SUMMARY_RE.search(stripped)
        return "note", (match.group(1).strip() if match else "task notification")
    # Written for the model, never shown in the agent's own transcript view.
    if stripped.startswith(("<system-reminder>", "<local-command-caveat>")):
        return None, ""
    return "user", stripped


def _parse_user(row, turns, tool_turns):
    # isMeta marks an injected envelope -- a caveat block, an image placeholder, a skill body --
    # rather than something a person typed. Verified across 60 transcripts on this machine: every
    # isMeta user row was injected, and no real message carried the flag.
    if row.get("isMeta"):
        return
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if row.get("isCompactSummary"):
        if isinstance(content, str) and content.strip():
            turns.append(_turn(row, "note", content, 0))
        return
    if isinstance(content, list):
        spoken = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                _fold_tool_result(block, tool_turns)
            elif block.get("type") == "text":
                piece = block.get("text")
                if isinstance(piece, str) and piece.strip():
                    spoken.append(piece)
        if spoken:
            # "[Request interrupted by user]" and friends arrive on this shape; the flag is what
            # tells them apart from a person typing.
            role = "note" if row.get("interruptedMessageId") else "user"
            turns.append(_turn(row, role, "\n".join(spoken), 0))
        return
    if not isinstance(content, str) or not content.strip():
        return
    role, text = classify_user_text(content)
    if role:
        turns.append(_turn(row, role, text, 0))


def _parse_system(row, turns):
    if row.get("subtype") not in {"compact_boundary", "away_summary"}:
        return
    content = row.get("content")
    if isinstance(content, str) and content.strip():
        turns.append(_turn(row, "note", content, 0))


def parse_claude(lines):
    """(turns, title) from an iterable of JSONL lines. Oldest first."""
    turns = []
    tool_turns = {}
    seen_rows = set()
    title = ""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            # A transcript being appended to has a torn last line. That is a normal state.
            continue
        if not isinstance(row, dict):
            continue
        if row.get("isSidechain"):
            continue  # subagent traffic, not this conversation
        # Real transcripts replay rows: one session here has 591 of 2602 rows written twice, same
        # uuid, same timestamp, same content (a resumed session re-appending what it loaded). The
        # uuid is the row's identity, so the second copy is a duplicate, not a second turn -- and
        # deduping here is also what keeps a turn id usable as a pagination cursor.
        row_uuid = row.get("uuid")
        if isinstance(row_uuid, str) and row_uuid:
            if row_uuid in seen_rows:
                continue
            seen_rows.add(row_uuid)
        row_type = row.get("type")
        if row_type == "assistant":
            _parse_assistant(row, turns, tool_turns)
        elif row_type == "user":
            _parse_user(row, turns, tool_turns)
        elif row_type == "system":
            _parse_system(row, turns)
        elif row_type == "ai-title":
            candidate = row.get("aiTitle")
            if isinstance(candidate, str) and candidate.strip():
                title = candidate.strip()
    for index, turn in enumerate(turns):
        if not turn["uuid"] or turn["uuid"].startswith("#"):
            turn["uuid"] = f"turn-{index}"
    return turns, title


# ---------------------------------------------------------------------------- codex parser
#
# Codex rollouts carry both response_item and event_msg rows for the same conversation. Read
# only the canonical response items: message text, function/custom tool calls and their results.
# Session metadata, developer/system messages, reasoning and streaming events are not turns.


def _codex_text(content):
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
                     and isinstance(block.get("text"), str) and block["text"].strip())


def _codex_tool_result(payload, tool_turns):
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or call_id not in tool_turns:
        return
    output = payload.get("output")
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except ValueError:
            decoded = None
        # Older shell tools wrap the output and exit code in a JSON string.
        if isinstance(decoded, dict) and isinstance(decoded.get("output"), str):
            output = decoded
    failed = False
    if isinstance(output, dict):
        metadata = output.get("metadata")
        exit_code = metadata.get("exit_code") if isinstance(metadata, dict) else None
        failed = isinstance(exit_code, int) and exit_code != 0
        output = output.get("output")
    if isinstance(output, list):
        output = _codex_text(output)
    _fold_tool_result({"tool_use_id": call_id, "content": output, "is_error": failed}, tool_turns)


def parse_codex(lines):
    """(turns, title) from a Codex rollout JSONL, without duplicating its event stream."""
    turns, tool_turns, seen_rows = [], {}, set()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue  # a live rollout can end with a partially written row
        if not isinstance(row, dict) or row.get("type") != "response_item":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        item_id = payload.get("id")
        # Older rollouts have no item ids. Hash the row, not its index: a tail read or an append
        # must not renumber existing pagination cursors. Replayed response items are deduped too.
        key = item_id if isinstance(item_id, str) and item_id else hashlib.sha256(raw.encode()).hexdigest()
        if key in seen_rows:
            continue
        seen_rows.add(key)
        turn_row = {"uuid": key, "timestamp": row.get("timestamp")}
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role not in ("user", "assistant") or payload.get("channel") == "analysis":
                continue
            text = _codex_text(payload.get("content"))
            if text:
                turns.append(_turn(turn_row, role, text, 0))
        elif kind in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            name = name if isinstance(name, str) and name else "tool"
            args = _pi_tool_args(payload) if kind == "function_call" else {
                "command": payload.get("input") if isinstance(payload.get("input"), str) else ""}
            block = {"name": name, "input": args}
            turn = _turn(turn_row, "tool", _tool_summary(block), 0, limit=TOOL_TEXT_LIMIT)
            _annotate_tool(turn, block)
            turns.append(turn)
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                tool_turns[call_id] = turn
        elif kind in ("function_call_output", "custom_tool_call_output"):
            _codex_tool_result(payload, tool_turns)
    return turns, ""


def locate_codex(session_value, roots=None):
    """Find a dated (or older flat) rollout by validated session UUID, never by pane cwd."""
    for root in (roots if roots is not None else CODEX_ROOTS):
        root = os.path.realpath(os.path.expanduser(root))
        for prefix in (os.path.join("*", "*", "*"), ""):
            pattern = os.path.join(glob.escape(root), prefix, f"rollout-*-{session_value}.jsonl")
            for path in sorted(glob.glob(pattern)):
                real = os.path.realpath(path)
                if os.path.commonpath([root, real]) == root and os.path.isfile(real):
                    return real
    return None


# ---------------------------------------------------------------------------- pi parser
#
# pi's JSONL is an event stream: one row per `type`, and a conversation turn is a
# `type:"message"` row carrying a nested `message` with `role` and a content block list.
# Block kinds differ from claude's: `text`, `thinking`, `toolCall` (name+arguments, arguments a
# JSON string), and `toolResult` arrives as its own `role:"toolResult"` message rather than
# folded into a user row. Everything else (session, model_change, custom) is not a turn.

def _pi_content_blocks(row):
    message = row.get("message")
    if not isinstance(message, dict):
        return None, []
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return message.get("role"), []
    return message.get("role"), content


def _pi_tool_args(block):
    """pi stores toolCall arguments as a JSON string; hand back a dict for _tool_target."""
    args = block.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def parse_pi(lines):
    """(turns, title) from pi's JSONL. Oldest first, same turn shape as parse_claude."""
    turns = []
    tool_turns = {}
    seen_rows = set()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue  # torn last line while the file is being appended to
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            if row_id in seen_rows:
                continue
            seen_rows.add(row_id)
        # pi rows key their id under "id"; _turn reads "uuid". Alias it so turn ids stay usable
        # as pagination cursors, exactly like claude's uuid.
        if "uuid" not in row and isinstance(row_id, str):
            row["uuid"] = row_id
        role, content = _pi_content_blocks(row)
        if role == "user":
            spoken = [b.get("text") for b in content
                      if isinstance(b, dict) and b.get("type") == "text"
                      and isinstance(b.get("text"), str) and b.get("text").strip()]
            if spoken:
                turns.append(_turn(row, "user", "\n".join(spoken), 0))
        elif role == "assistant":
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        turns.append(_turn(row, "assistant", text, index))
                elif kind == "toolCall":
                    args = _pi_tool_args(block)
                    name = block.get("name")
                    name = name if isinstance(name, str) and name else "tool"
                    detail = _tool_target(args)
                    summary = f"{name}({detail})" if detail else name
                    turn = _turn(row, "tool", summary, index, limit=TOOL_TEXT_LIMIT)
                    turn["tool"] = name
                    if detail:
                        turn["target"] = detail[:TOOL_TEXT_LIMIT]
                    diff = _tool_diff(name, args)
                    if diff:
                        turn["diff"], turn["added"], turn["removed"], clipped = diff
                        if clipped:
                            turn["diff_clipped"] = True
                    turns.append(turn)
                    tool_id = block.get("id")
                    if isinstance(tool_id, str) and tool_id:
                        tool_turns[tool_id] = turn
                # thinking: dropped, same as claude -- bulk of the bytes, not the conversation.
        elif role == "toolResult":
            # pi emits the result as its own message; fold it onto the toolCall it answers.
            tool_id = (row.get("message") or {}).get("toolCallId") or row.get("toolCallId")
            turn = tool_turns.get(tool_id)
            if turn is not None:
                body = "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text")
                head = _first_line(body)
                if head:
                    turn["text"], turn["truncated"] = clip(
                        f"{turn['text']} \u2192 {head}", TOOL_TEXT_LIMIT)
    for index, turn in enumerate(turns):
        if not turn["uuid"] or turn["uuid"].startswith("#"):
            turn["uuid"] = f"turn-{index}"
    return turns, ""


def locate_pi(path_value, roots=None):
    """pi hands over an absolute path; return it only if it sits inside a configured root.

    The value comes from herdr, not a client, but a containment check is cheap insurance against
    a path ref ever pointing outside the sessions tree.
    """
    if not isinstance(path_value, str) or not path_value:
        return None
    real = os.path.realpath(path_value)
    for root in (roots if roots is not None else PI_ROOTS):
        root_real = os.path.realpath(os.path.expanduser(root))
        if real == root_real or real.startswith(root_real + os.sep):
            return real if os.path.isfile(real) else None
    return None


# ---------------------------------------------------------------------------- kimi parser
#
# kimi-code's wire.jsonl is an event stream, and the same conversation arrives on three of its
# channels: `agent.message.appended` carries each finished message (role user/assistant/tool),
# `context.append_message` repeats the typed prompts and adds the machinery's injections, and
# `context.append_loop_event` is the streaming trace the finished message supersedes. Only the
# first is read, so nothing is counted twice. Message shapes measured across the 105 sessions on
# this machine:
#
#   user       -> content is text blocks; meta.source "input" is a person typing, "notify" is the
#                 background-task channel (a cron fire, a finished watcher) and reads as a note.
#   assistant  -> content is `think` + `text` blocks (think dropped, same policy as the others);
#                 toolCalls carry {id, name, arguments} with arguments a JSON string like pi's.
#   tool       -> a tool RESULT, one message per call, linked by toolCallId; content is text and
#                 image_url blocks, and the images are base64 in the row -- text only, ever.


def _kimi_timestamp(row):
    """kimi stamps rows in epoch milliseconds; every other harness and every client speaks ISO."""
    try:
        ms = int(row.get("time"))
    except (TypeError, ValueError):
        return ""
    at = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def _kimi_blocks(message):
    blocks = message.get("content")
    if isinstance(blocks, str):
        return [{"type": "text", "text": blocks}]
    return blocks if isinstance(blocks, list) else []


def _kimi_spoken(blocks):
    """The text blocks' texts joined, or "" -- think and image_url never contribute."""
    pieces = [block.get("text") for block in blocks
              if isinstance(block, dict) and block.get("type") == "text"
              and isinstance(block.get("text"), str) and block.get("text").strip()]
    return "\n".join(pieces)


def parse_kimi(lines):
    """(turns, title) from kimi-code's wire.jsonl. Oldest first, same turn shape as parse_claude."""
    turns = []
    tool_turns = {}
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue  # torn last line while the file is being appended to
        if not isinstance(row, dict) or row.get("type") != "agent.message.appended":
            continue
        outer = row.get("message")
        message = outer.get("message") if isinstance(outer, dict) else None
        if not isinstance(message, dict):
            continue
        # _turn reads uuid and timestamp off the row; kimi rows carry neither, but they do carry
        # epoch-ms `time`, which becomes the timestamp every client already renders.
        row = {"timestamp": _kimi_timestamp(row)}
        role = message.get("role")
        blocks = _kimi_blocks(message)
        if role == "user":
            spoken = _kimi_spoken(blocks)
            if not spoken:
                continue
            source = (outer.get("meta") or {}).get("source") if isinstance(outer, dict) else None
            turns.append(_turn(row, "note" if source == "notify" else "user", spoken, 0))
        elif role == "assistant":
            for index, block in enumerate(blocks):
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    turns.append(_turn(row, "assistant", text, index))
            for call in message.get("toolCalls") or []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                name = name if isinstance(name, str) and name else "tool"
                args = _pi_tool_args(call)  # kimi stores arguments as a JSON string too
                detail = _tool_target(args)
                summary = f"{name}({detail})" if detail else name
                turn = _turn(row, "tool", summary, 0, limit=TOOL_TEXT_LIMIT)
                _annotate_tool(turn, {"name": name, "input": args})
                turns.append(turn)
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id:
                    tool_turns[call_id] = turn
        elif role == "tool":
            turn = tool_turns.get(message.get("toolCallId"))
            head = _first_line(_kimi_spoken(blocks))
            if turn is not None and head:
                turn["text"], turn["truncated"] = clip(
                    f"{turn['text']} \u2192 {head}", TOOL_TEXT_LIMIT)
    for index, turn in enumerate(turns):
        if not turn["uuid"] or turn["uuid"].startswith("#"):
            turn["uuid"] = f"turn-{index}"
    return turns, ""


def locate_kimi(session_value, roots=None):
    """The wire.jsonl for a `session_<uuid>` ref, or None.

    A glob on the leaf name rather than a derived path: the workspace directory in the middle
    (`wd_<slug>_<hash>`) is keyed to the cwd kimi started in, which the pane's shell may have
    left, exactly like claude's project directory. The value is SESSION_ID_RE-validated before it
    gets here, so the pattern cannot be steered out of a root.
    """
    for root in (roots if roots is not None else KIMI_ROOTS):
        matches = sorted(glob.glob(
            os.path.join(os.path.expanduser(root), "*", session_value, "agents", "main", "wire.jsonl")))
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------- locating


def locate_claude(session_value, roots=None):
    """The transcript path for a session uuid, or None.

    A glob on the uuid rather than deriving the project directory from the pane's cwd: the rule is
    real (every `/`, `.` and `_` becomes `-`) but the pane's cwd is the shell's, while claude's
    project directory is fixed at ITS startup cwd, and the two drift. The uuid is globally unique
    and the glob measured 0.7ms.
    """
    for root in (roots if roots is not None else LOCAL_ROOTS):
        matches = sorted(glob.glob(os.path.join(os.path.expanduser(root), "*", f"{session_value}.jsonl")))
        if matches:
            return matches[0]
    return None


# A remote root has to reach the far shell unquoted -- `$HOME/.claude/projects` is the default and
# our $HOME is not theirs -- so it is constrained instead of quoted. It comes from the relay's own
# environment, never from a client, and a root that does not fit is dropped rather than sent.
REMOTE_ROOT_RE = re.compile(r"\A[A-Za-z0-9_./~$-]+\Z")


def remote_probe_script(session_value, roots, expected_size, tail_bytes, harness="claude"):
    """A POSIX script that answers NOFILE / CACHED / SIZE+tail in one round trip.

    Only `ls`, `wc`, `tail` and `head` -- no python on the far side. `wc -c < file` is read through
    `set --` because BSD wc pads its output with spaces and GNU wc does not.

    `expected_size` is what our cache last saw. When the file has not grown, the answer is CACHED
    and the bytes never move; when it has, we pay one transfer. Pagination is therefore cheap even
    for a remote pane, which matters because "load older" is a per-click round trip.
    """
    safe_roots = [root for root in roots if REMOTE_ROOT_RE.match(root)]
    if not safe_roots:
        raise ValueError("no usable remote transcript root")
    if harness not in {"claude", "codex"} or not UUID_RE.match(session_value):
        raise ValueError("no usable remote transcript ref")
    suffixes = (f"*/*/*/rollout-*-{session_value}.jsonl", f"rollout-*-{session_value}.jsonl") \
        if harness == "codex" else (f"*/{session_value}.jsonl",)
    candidates = " ".join(f"{root}/{suffix}" for root in safe_roots for suffix in suffixes)
    shortcut = (
        f'[ "$s" = "{int(expected_size)}" ] && {{ echo CACHED; exit 0; }}; '
        if expected_size > 0 else ""
    )
    return (
        f'f=$(ls -1 {candidates} 2>/dev/null | head -1); '
        '[ -n "$f" ] || { echo NOFILE; exit 0; }; '
        'set -- $(wc -c < "$f"); s=$1; '
        + shortcut +
        'echo "SIZE $s"; '
        f'tail -c {int(tail_bytes)} "$f"'
    )


class RemotePiUnavailable(Exception):
    """An explicit remote Pi capability or path failure (never fall back to local IO)."""


# Executed only on the Pi host. No shell expansion of roots or session paths, directory
# harvesting, third-party imports, or writes. The final open is nonblocking/no-follow and
# checked against lstat so a special file or replacement cannot hang the SSH worker.
_REMOTE_PI_HELPER = r'''
import json, os, stat, sys

request = json.loads(sys.argv[1])

def emit(frame):
    print(frame, flush=True)

def contained(path, roots):
    return any(os.path.commonpath([path, root]) == root for root in roots)

def read_file(path, limit, tail=False, expected=0):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("not regular")
    if not tail and before.st_size > limit:
        raise ValueError("too large")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or os.path.realpath(path, strict=True) != path):
            raise ValueError("file changed")
        size = opened.st_size
        if tail:
            if expected > 0 and size == expected:
                return "CACHED", b""
            handle.seek(max(0, size - limit))
            return "SIZE " + str(size), handle.read(min(size, limit))
        blob = handle.read(limit + 1)
        if len(blob) > limit:
            raise ValueError("too large")
        return "CATALOG", blob

try:
    path = request["path"]
    if (not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path
            or not path.endswith(".jsonl")
            or any(part in (".", "..") for part in path.split("/"))):
        emit("NOSESSION")
        sys.exit(0)
    roots = [os.path.realpath(os.path.expanduser(os.path.expandvars(root)))
             for root in request["roots"]]
    try:
        parent = os.path.realpath(os.path.dirname(path), strict=True)
    except OSError:
        emit("NOSESSION")
        sys.exit(0)
    if not os.path.isdir(parent) or not contained(parent, roots):
        emit("NOSESSION")
        sys.exit(0)
    allocated = os.path.join(parent, os.path.basename(path))
    try:
        os.lstat(allocated)
    except FileNotFoundError:
        canonical = allocated
    else:
        try:
            canonical = os.path.realpath(allocated, strict=True)
        except OSError:
            emit("NOSESSION")
            sys.exit(0)
        if (not contained(canonical, roots) or not canonical.endswith(".jsonl")
                or not stat.S_ISREG(os.stat(canonical).st_mode)):
            emit("NOSESSION")
            sys.exit(0)
    if request["mode"] == "catalog":
        frame, blob = read_file(canonical + ".herdr-commands.json", 128 * 1024)
        emit(frame + " " + json.dumps(canonical))
    else:
        frame, blob = read_file(canonical, request["limit"], True, request["expected"])
        emit(frame)
    sys.stdout.buffer.write(blob)
except FileNotFoundError:
    emit("NOFILE")
except (OSError, ValueError, TypeError, RecursionError, OverflowError):
    emit("ERROR")
'''


def remote_pi_script(value, *, mode="transcript", expected_size=0, tail_bytes=REMOTE_TAIL_BYTES):
    """A bounded stdlib reader; all untrusted values are a quoted JSON argv word."""
    request = {"path": value, "roots": REMOTE_PI_ROOTS, "mode": mode,
               "expected": int(expected_size), "limit": max(1, int(tail_bytes))}
    return "python3 -c " + shlex.quote(_REMOTE_PI_HELPER) + " " + shlex.quote(json.dumps(request))


def check_remote_pi_frame(returncode, blob):
    if returncode == 127:
        raise RemotePiUnavailable("python3-unavailable")
    if returncode != 0:
        raise RemotePiUnavailable("remote-error")
    header = blob.partition(b"\n")[0]
    if header == b"NOSESSION":
        raise RemotePiUnavailable("no-session")
    if header == b"ERROR":
        raise RemotePiUnavailable("error")


def _default_remote_runner(remote, script, ssh_args=()):
    cmd = ["ssh", *ssh_args, remote, "sh -c " + shlex.quote(script)]
    proc = subprocess.run(cmd, capture_output=True, timeout=REMOTE_TIMEOUT)
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------- reading


def drop_partial_line(blob):
    """A tail starts mid-line. Drop that fragment rather than hand the parser a torn JSON row."""
    cut = blob.find(b"\n")
    return blob[cut + 1:] if cut != -1 else b""


def _decode(blob):
    return blob.decode("utf-8", "replace").splitlines()


def read_local(path):
    """(lines, truncated) for a local transcript, reading only the tail of a huge one."""
    size = os.path.getsize(path)
    if size <= MAX_BYTES:
        with open(path, "rb") as handle:
            return _decode(handle.read()), False
    with open(path, "rb") as handle:
        handle.seek(-min(TAIL_BYTES, size), os.SEEK_END)
        blob = handle.read()
    return _decode(drop_partial_line(blob)), True


# ---------------------------------------------------------------------------- cache
#
# Parsed turns, not raw bytes: the largest session on this machine is 33MB of JSONL but only 0.25MB
# of turns, and the raw text is dropped as soon as it is parsed, so peak memory is one file.
# Invalidated on size (plus mtime locally). A transcript is append-only, so a size that has not
# moved means nothing was added -- which is also what lets the remote probe answer CACHED.

_cache = {}
_cache_order = []
_cache_lock = threading.Lock()


def cache_get(key, fingerprint):
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None or entry[0] != fingerprint:
            return None
        _cache_order.remove(key)
        _cache_order.append(key)
        return entry[1]


def cache_put(key, fingerprint, value):
    with _cache_lock:
        if key in _cache:
            _cache_order.remove(key)
        _cache[key] = (fingerprint, value)
        _cache_order.append(key)
        while len(_cache_order) > CACHE_SIZE:
            _cache.pop(_cache_order.pop(0), None)


def cache_peek_size(key):
    """The size our cached parse was made from, for the remote CACHED shortcut. 0 when unknown."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return 0
        fingerprint = entry[0]
        return fingerprint[0] if isinstance(fingerprint, tuple) and fingerprint else 0


def cache_clear():
    with _cache_lock:
        _cache.clear()
        _cache_order.clear()


# ---------------------------------------------------------------------------- pagination


def paginate(turns, limit, before, include_tools):
    """(page, total, has_more) anchored on the newest turn, walking backwards from `before`.

    Two ceilings, whichever bites first: `limit` turns and PAGE_TEXT_BUDGET characters. The budget
    exists because turn counts and payload sizes are only loosely related -- 200 turns measured
    97KB of JSON on one session here and 324KB on another -- and a phone opening a panel should not
    wait on a third of a megabyte. Whatever the budget cuts is still reachable through `has_more`.
    """
    visible = turns if include_tools else [turn for turn in turns if turn["role"] != "tool"]
    total = len(visible)
    end = total
    if before:
        index = next((i for i, turn in enumerate(visible) if turn["uuid"] == before), None)
        if index is not None:
            end = index
        # Unknown cursor (file rewritten, stale client): fall back to the newest page. The user
        # asked for "older" and we owe them something, not a blank panel.
    floor = max(0, end - max(1, limit))
    start, used = end, 0
    while start > floor:
        turn = visible[start - 1]
        # A diff is payload like any other. Left out of the count, a page of file edits would
        # quietly ship several times the budget it advertises.
        cost = len(turn["text"]) + len(turn.get("diff") or "") + TURN_OVERHEAD
        if used + cost > PAGE_TEXT_BUDGET and start < end:
            break  # always yield at least the newest turn, however long it is
        used += cost
        start -= 1
    return visible[start:end], total, start > 0


# ---------------------------------------------------------------------------- entry point


def _unavailable(reason, agent=""):
    return {
        "messages": [], "total": 0, "has_more": False, "title": "",
        "agent": agent, "file_truncated": False, "unavailable": reason,
    }


# Each harness is (locate, parse). PATH_HARNESSES also names the session-ref kind it accepts:
# claude, codex and kimi use "id" (a value we glob for), pi uses "path" (an absolute file it hands over).
HARNESSES = {"claude": (locate_claude, parse_claude), "codex": (locate_codex, parse_codex),
             "pi": (locate_pi, parse_pi), "kimi": (locate_kimi, parse_kimi)}
PATH_HARNESSES = {"pi"}
# The shape an "id" value must take, per harness. Anything absent defaults to a bare uuid, so an
# unlisted harness keeps the tightest rule until somebody writes its reader.
ID_VALUE_RES = {"kimi": SESSION_ID_RE}


def history(session, remote=None, limit=DEFAULT_LIMIT, before=None, include_tools=False,
            agent="", ssh_args=(), remote_runner=None, log=None):
    """The history payload body for one pane's session ref.

    `session` is the raw `agent_session` record herdr reports, straight out of the relay's
    pane_session_map -- clients never see or send a session uuid, they send a pane_id.
    """
    if not ENABLED:
        return _unavailable("disabled", agent)
    if not isinstance(session, dict):
        return _unavailable("no-session", agent)
    harness = session.get("agent") or agent
    kind = session.get("kind")
    value = session.get("value")
    # A path harness (pi) hands over an absolute file; an id harness (claude, kimi) hands over a
    # value we glob for. Validate the ref shape against what this harness actually uses -- a ref
    # we cannot make sense of is "no session", whatever the harness.
    if harness in PATH_HARNESSES:
        if kind != "path" or not isinstance(value, str) or not value:
            if log and session:
                log.info("transcript: %r session ref kind %r not usable", harness, kind)
            return _unavailable("no-session", agent)
    else:
        if kind != "id":
            if log and session:
                log.info("transcript: session ref kind %r not supported", kind)
            return _unavailable("no-session", agent)
        # kimi's ids carry a `session_` prefix (see SESSION_ID_RE); every other id harness is a
        # bare uuid. The pattern is what keeps the value safe to glue into a path.
        if not isinstance(value, str) or not ID_VALUE_RES.get(harness, UUID_RE).match(value):
            return _unavailable("no-session", agent)
    entry = HARNESSES.get(harness)
    if entry is None:
        # A pane running a harness this relay cannot parse is a different sentence from a pane
        # with no session at all, and the UI should be able to say which.
        if log:
            log.info("transcript: no reader for harness %r", harness)
        return _unavailable("unsupported", harness)
    locate, parse = entry
    # Remote lookup needs a harness-specific path, not another harness's glob (or a raw path ref).
    if remote and harness not in {"claude", "codex", "pi"}:
        return _unavailable("unsupported", harness)

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
    try:
        if remote:
            parsed = _load_remote(value, parse, remote, ssh_args, remote_runner, harness=harness)
        else:
            parsed = _load_local(value, locate, parse)
    except RemotePiUnavailable as exc:
        return _unavailable(str(exc), harness)
    except Exception:
        if log:
            log.exception("transcript read failed for session %s (remote=%s)", value, remote)
        return _unavailable("error", agent)
    if parsed is None:
        return _unavailable("no-log", agent)

    turns, title, file_truncated = parsed
    page, total, has_more = paginate(turns, limit, before, include_tools)
    return {
        "messages": page,
        "total": total,
        "has_more": has_more,
        "title": title,
        "agent": harness,
        "file_truncated": file_truncated,
        "unavailable": None,
    }


def _load_local(value, locate, parse):
    path = locate(value)
    if not path:
        return None
    stat = os.stat(path)
    key = (None, path)
    fingerprint = (stat.st_size, stat.st_mtime_ns)
    cached = cache_get(key, fingerprint)
    if cached is not None:
        return cached
    lines, file_truncated = read_local(path)
    turns, title = parse(lines)
    parsed = (turns, title, file_truncated)
    cache_put(key, fingerprint, parsed)
    return parsed


def _load_remote(value, parse, remote, ssh_args, remote_runner, harness="claude"):
    """Read the harness's own path shape, keeping its remote parse cache separate too."""
    key = (remote, harness, value)
    roots = REMOTE_CODEX_ROOTS if harness == "codex" else REMOTE_ROOTS
    runner = remote_runner or _default_remote_runner

    def probe(expected):
        if harness == "pi":
            return remote_pi_script(value, expected_size=expected, tail_bytes=REMOTE_TAIL_BYTES)
        return remote_probe_script(value, roots, expected, REMOTE_TAIL_BYTES, harness=harness)

    script = probe(cache_peek_size(key))
    returncode, blob = runner(remote, script, ssh_args)
    if harness == "pi":
        check_remote_pi_frame(returncode, blob)
    if returncode != 0:
        raise OSError(f"ssh transcript read failed on {remote} with exit {returncode}")
    header, _, body = blob.partition(b"\n")
    header = header.strip()
    if header == b"NOFILE":
        return None
    if header == b"CACHED":
        cached = cache_get(key, (cache_peek_size(key),))
        if cached is not None:
            return cached
        # The cache was evicted between the probe and now. Ask again without the shortcut.
        script = probe(0)
        returncode, blob = runner(remote, script, ssh_args)
        if harness == "pi":
            check_remote_pi_frame(returncode, blob)
        if returncode != 0:
            raise OSError(f"ssh transcript read failed on {remote} with exit {returncode}")
        header, _, body = blob.partition(b"\n")
        header = header.strip()
        if header == b"NOFILE":
            return None
    if not header.startswith(b"SIZE "):
        raise ValueError(f"unexpected transcript frame from {remote}: {header[:40]!r}")
    size = int(header.split()[1])
    file_truncated = len(body) < size
    if file_truncated:
        body = drop_partial_line(body)
    turns, title = parse(_decode(body))
    parsed = (turns, title, file_truncated)
    cache_put(key, (size,), parsed)
    return parsed
