import * as fs from "node:fs";
import { basename, dirname, isAbsolute, join, sep } from "node:path";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const SUFFIX = ".herdr-commands.json";
const MAX_BYTES = 128 * 1024;
const MAX_COMMANDS = 500;
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const HEARTBEAT_MS = 15_000;

type Command = { name: string; description: string; source: "extension" | "template" | "skill" };
type Catalog = {
  schemaVersion: 1; sessionFile: string; pid: number; updatedAt: number; commands: Command[];
};

/** Runtime metadata only: no registered tool, command, socket, or source-file scan. */
export default function (pi: ExtensionAPI) {
  let context: ExtensionContext | undefined;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let deferred: ReturnType<typeof setTimeout> | undefined;
  // Keep the descriptor of OUR last atomic replacement. Invalidating this inode cannot touch
  // a newer publisher's replacement, even if it uses the same PID (e.g. /reload).
  let owned: { fd: number; catalog: Catalog } | undefined;

  function invalidate() {
    const previous = owned;
    owned = undefined;
    if (!previous) return;
    try {
      const blob = JSON.stringify({ ...previous.catalog, updatedAt: 0, commands: [] });
      fs.writeSync(previous.fd, blob, 0, "utf8");
      fs.ftruncateSync(previous.fd, Buffer.byteLength(blob));
    } catch { /* Read-only directories/disappearing sessions must never break Pi. */ }
    finally { try { fs.closeSync(previous.fd); } catch { /* Already closed. */ } }
  }

  function allocatedSession(): string | undefined {
    const candidate = context?.sessionManager.getSessionFile();
    if (typeof candidate !== "string" || !isAbsolute(candidate) || candidate.includes("\0")
        || !candidate.endsWith(".jsonl")
        || candidate.split(sep === "\\" ? /[\\/]/ : /\//).some(part => part === "." || part === "..")) return;
    // Pi allocates the filename before saving its first message. Only the final file may
    // be absent; never create directories or touch the transcript to publish metadata.
    const parent = fs.realpathSync(dirname(candidate));
    if (!fs.statSync(parent).isDirectory()) return;
    const allocated = join(parent, basename(candidate));
    try {
      fs.lstatSync(allocated);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return allocated;
      throw error;
    }
    // Existing symlinks must resolve to a regular file, not a dangling target or device.
    const canonical = fs.realpathSync(allocated);
    return canonical.endsWith(".jsonl") && fs.statSync(canonical).isFile() ? canonical : undefined;
  }

  function publish() {
    let temporary: string | undefined;
    let fd: number | undefined;
    try {
      const sessionFile = allocatedSession();
      if (!sessionFile) { invalidate(); return; }
      if (owned && owned.catalog.sessionFile !== sessionFile) invalidate();
      if (typeof pi.getCommands !== "function") { invalidate(); return; }
      const catalog: Catalog = {
        schemaVersion: 1, sessionFile, pid: process.pid, updatedAt: Date.now(), commands: [],
      };
      const seen = new Set<string>();
      let bytes = Buffer.byteLength(JSON.stringify(catalog));
      for (const entry of pi.getCommands()) {
        if (!entry || typeof entry.name !== "string" || NAME.exec(entry.name)?.[0] !== entry.name
            || entry.name.length > 128 || seen.has(entry.name)) continue;
        const source = entry.source === "prompt" ? "template" : entry.source;
        if (source !== "extension" && source !== "template" && source !== "skill") continue;
        const command: Command = {
          name: entry.name,
          description: typeof entry.description === "string"
            ? entry.description.replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 200) : "",
          source,
        };
        const cost = Buffer.byteLength(JSON.stringify(command)) + (catalog.commands.length ? 1 : 0);
        if (bytes + cost > MAX_BYTES) break;
        seen.add(entry.name);
        catalog.commands.push(command);
        bytes += cost;
        if (catalog.commands.length === MAX_COMMANDS) break;
      }
      const blob = JSON.stringify(catalog);
      if (Buffer.byteLength(blob) > MAX_BYTES) return;
      temporary = sessionFile + SUFFIX + "." + randomUUID() + ".tmp";
      fd = fs.openSync(temporary, "wx+", 0o600);
      fs.writeFileSync(fd, blob, "utf8");
      fs.renameSync(temporary, sessionFile + SUFFIX);
      temporary = undefined;
      if (owned) fs.closeSync(owned.fd);
      owned = { fd, catalog };
      fd = undefined;
    } catch {
      // Includes missing session directories and permission failures. No sensitive path logging.
      invalidate();
    } finally {
      if (fd !== undefined) { try { fs.closeSync(fd); } catch { /* Best effort. */ } }
      if (temporary) { try { fs.unlinkSync(temporary); } catch { /* Best effort. */ } }
    }
  }

  function stop() {
    clearInterval(heartbeat);
    clearTimeout(deferred);
    heartbeat = undefined;
    deferred = undefined;
    context = undefined;
    invalidate();
  }

  function start(_event: unknown, current: ExtensionContext) {
    stop();
    if (["0", "false", "no", "off"].includes(
      (process.env.HERDR_COMMAND_DISCOVERY ?? "1").trim().toLowerCase())) return;
    context = current;
    // Other extensions' session_start handlers may register commands after this handler.
    // A deferred first pass and recurring heartbeat capture late/async registrations too.
    deferred = setTimeout(publish, 0);
    deferred.unref();
    heartbeat = setInterval(publish, HEARTBEAT_MS);
    heartbeat.unref();
  }

  pi.on("session_start", start);
  pi.on("session_switch", start);
  pi.on("session_shutdown", stop);
}
