import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import extension from '../pi-extension/commands.ts';

const suffix = '.herdr-commands.json';

function setup(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'herdr-commands-test-'));
  const session = path.join(directory, 'session.jsonl');
  fs.writeFileSync(session, '{}\n');
  let sessionFile = session;
  let entries = [{ name: 'runtime', description: 'Loaded command', source: 'extension' }];
  const timers = [];
  const events = new Map();
  for (const name of ['setTimeout', 'setInterval']) {
    t.mock.method(globalThis, name, (callback, delay) => {
      const timer = { callback, delay, unreffed: false, cleared: false,
        unref() { this.unreffed = true; } };
      timers.push(timer);
      return timer;
    });
  }
  for (const name of ['clearTimeout', 'clearInterval']) {
    t.mock.method(globalThis, name, timer => { if (timer) timer.cleared = true; });
  }
  const api = { on(name, handler) { events.set(name, handler); }, getCommands() { return entries; } };
  extension(api);
  const ctx = { sessionManager: { getSessionFile() { return sessionFile; } } };
  const emit = name => events.get(name)({}, ctx);
  t.after(() => { emit('session_shutdown'); fs.rmSync(directory, { recursive: true, force: true }); });
  return { directory, session, timers, events, api, emit,
    setSession(value) { sessionFile = value; },
    setEntries(value) { entries = value; },
    read(file = session) { return JSON.parse(fs.readFileSync(file + suffix, 'utf8')); },
    tick() { timers.filter(timer => !timer.cleared).at(-1).callback(); },
  };
}

test('metadata is runtime-only, deferred, bounded, mapped, and private', t => {
  const s = setup(t);
  assert.equal(s.timers.length, 0, 'no resources during factory loading');
  s.emit('session_start');
  assert.equal(fs.existsSync(s.session + suffix), false);
  s.setEntries([
    { name: 'late', source: 'extension', description: 'After our session_start' },
    { name: 'late', source: 'skill', description: 'Duplicate' },
    { name: 'review', source: 'prompt', description: 'x'.repeat(300) },
    { name: 'skill:pdf', source: 'skill', description: 'PDF\nreader' },
    ...['bad\n', '/slash', 'x'.repeat(129), '', 'with space'].map(name => ({ name, source: 'extension' })),
    { name: 'builtin', source: 'builtin' },
  ]);
  s.timers[0].callback();
  const data = s.read();
  assert.equal(data.schemaVersion, 1);
  assert.equal(data.sessionFile, fs.realpathSync(s.session));
  assert.equal(data.pid, process.pid);
  assert.ok(Math.abs(data.updatedAt - Date.now()) < 1000);
  assert.deepEqual(data.commands.map(c => [c.name, c.source]),
    [['late', 'extension'], ['review', 'template'], ['skill:pdf', 'skill']]);
  assert.equal(data.commands[1].description.length, 200);
  assert.equal(data.commands[2].description, 'PDF reader');
  assert.equal(fs.statSync(s.session + suffix).mode & 0o777, 0o600);
  assert.ok(s.timers.every(timer => timer.unreffed));
  assert.equal(s.timers[1].delay, 15000);
  assert.deepEqual(fs.readdirSync(s.directory).sort(), ['session.jsonl', 'session.jsonl' + suffix]);
});

test('heartbeat captures changes and invalidates own inode on shutdown', t => {
  const s = setup(t);
  s.emit('session_start');
  s.tick();
  s.setEntries([{ name: 'added-later', source: 'extension' }]);
  s.tick();
  assert.equal(s.read().commands[0].name, 'added-later');
  s.emit('session_shutdown');
  assert.equal(s.read().updatedAt, 0);
  assert.deepEqual(s.read().commands, []);
  assert.ok(s.timers.every(timer => timer.cleared));
  s.emit('session_shutdown');
});

test('shutdown does not invalidate a newer replacement, even with the same PID', t => {
  const s = setup(t);
  s.emit('session_start');
  s.tick();
  const newer = { ...s.read(), commands: [{ name: 'newer', source: 'extension', description: '' }] };
  const temporary = path.join(s.directory, 'new.tmp');
  fs.writeFileSync(temporary, JSON.stringify(newer));
  fs.renameSync(temporary, s.session + suffix);
  s.emit('session_shutdown');
  assert.deepEqual(s.read(), newer);
});

test('switch, reload, and session allocation after startup publish only the current session', t => {
  const s = setup(t);
  s.emit('session_start');
  s.tick();
  const second = path.join(s.directory, 'second.jsonl');
  fs.writeFileSync(second, '{}');
  s.setSession(second);
  s.emit('session_switch');
  assert.equal(s.read().updatedAt, 0);
  s.tick();
  assert.equal(s.read(second).sessionFile, second);
  s.emit('session_start');
  assert.equal(s.read(second).updatedAt, 0);
  s.tick();
  assert.ok(s.read(second).updatedAt > 0);
  s.setSession(undefined);
  s.tick();
  assert.equal(s.read(second).updatedAt, 0);
  s.setSession(s.session);
  s.tick();
  assert.ok(s.read().updatedAt > 0);
});

test('fresh allocated session publishes real metadata without creating a transcript', t => {
  const s = setup(t);
  fs.unlinkSync(s.session);
  s.emit('session_start');
  s.timers[0].callback();
  const data = s.read();
  assert.equal(data.sessionFile, path.join(fs.realpathSync(s.directory), 'session.jsonl'));
  assert.deepEqual(data.commands, [{ name: 'runtime', description: 'Loaded command', source: 'extension' }]);
  assert.equal(data.pid, process.pid);
  assert.ok(Math.abs(data.updatedAt - Date.now()) < 1000);
  s.tick();
  assert.equal(fs.existsSync(s.session), false);
  assert.deepEqual(fs.readdirSync(s.directory), ['session.jsonl' + suffix]);
  fs.writeFileSync(s.session, 'first real conversation\n');
  s.tick();
  assert.equal(fs.readFileSync(s.session, 'utf8'), 'first real conversation\n');
});

test('invalid, traversal, nonregular, missing-parent and no-session paths write nothing', t => {
  const s = setup(t);
  const directory = path.join(s.directory, 'directory.jsonl');
  fs.mkdirSync(directory);
  const dangling = path.join(s.directory, 'dangling.jsonl');
  fs.symlinkSync(path.join(s.directory, 'absent.jsonl'), dangling);
  const directoryLink = path.join(s.directory, 'directory-link.jsonl');
  fs.symlinkSync(directory, directoryLink);
  const before = fs.readdirSync(s.directory).sort();
  s.emit('session_start');
  for (const candidate of [undefined, '', 'relative.jsonl', path.join(s.directory, 'missing'),
    s.directory, directory, dangling, directoryLink, path.join(s.directory, 'missing', 'new.jsonl'),
    `${s.directory}/../${path.basename(s.directory)}/new.jsonl`, `${s.directory}/./new.jsonl`,
    `${s.session}/`, `${s.directory}/bad\0.jsonl`, `${s.session}/new.jsonl`]) {
    s.setSession(candidate);
    assert.doesNotThrow(() => s.tick());
    assert.deepEqual(fs.readdirSync(s.directory).sort(), before);
  }
});

test('allocated parent and existing regular file links use canonical identity', t => {
  const s = setup(t);
  const realParent = path.join(s.directory, 'real');
  fs.mkdirSync(realParent);
  const alias = path.join(s.directory, 'alias');
  fs.symlinkSync(realParent, alias);
  const fresh = path.join(realParent, 'fresh.jsonl');
  s.setSession(path.join(alias, 'fresh.jsonl'));
  s.emit('session_start');
  s.tick();
  assert.equal(s.read(fresh).sessionFile, path.join(fs.realpathSync(realParent), 'fresh.jsonl'));
  assert.equal(fs.existsSync(fresh), false);
  const link = path.join(s.directory, 'link.jsonl');
  fs.symlinkSync(s.session, link);
  s.setSession(link);
  s.tick();
  assert.equal(s.read().sessionFile, fs.realpathSync(s.session));
  assert.equal(fs.readFileSync(s.session, 'utf8'), '{}\n');
});

test('500 entries and 128KiB are independent ceilings, including UTF-8', t => {
  const s = setup(t);
  s.emit('session_start');
  s.setEntries(Array.from({ length: 1000 }, (_, i) => ({ name: `command-${i}`, source: 'extension' })));
  s.tick();
  assert.equal(s.read().commands.length, 500);
  s.setEntries(Array.from({ length: 1000 }, (_, i) => ({
    name: `command-${i}`, source: 'extension', description: '界'.repeat(200),
  })));
  s.tick();
  assert.ok(s.read().commands.length < 500);
  assert.ok(fs.statSync(s.session + suffix).size <= 128 * 1024);
});

test('runtime and filesystem failures never escape, leave no partial catalogs', t => {
  const s = setup(t);
  s.emit('session_start');
  s.tick();
  s.api.getCommands = () => { throw new Error('private detail'); };
  assert.doesNotThrow(() => s.tick());
  assert.equal(s.read().updatedAt, 0);
  s.api.getCommands = () => [];
  fs.unlinkSync(s.session + suffix);
  fs.mkdirSync(s.session + suffix); // rename onto a directory must fail, even running as root
  assert.doesNotThrow(() => s.tick());
  assert.equal(fs.readdirSync(s.directory).some(name => name.endsWith('.tmp')), false);
});

test('disabled discovery starts no resources', t => {
  const previous = process.env.HERDR_COMMAND_DISCOVERY;
  process.env.HERDR_COMMAND_DISCOVERY = 'off';
  t.after(() => {
    if (previous === undefined) delete process.env.HERDR_COMMAND_DISCOVERY;
    else process.env.HERDR_COMMAND_DISCOVERY = previous;
  });
  const s = setup(t);
  s.emit('session_start');
  assert.equal(s.timers.length, 0);
  assert.equal(fs.existsSync(s.session + suffix), false);
});
