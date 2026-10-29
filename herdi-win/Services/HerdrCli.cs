using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Herdi.Models;

namespace Herdi.Services;

/// <summary>Outcome of one herdr invocation.</summary>
public sealed record HerdrResult(bool Ok, string Output, string? Error)
{
    public static HerdrResult Fail(string error) => new(false, string.Empty, error);
}

/// <summary>
/// Runs herdr commands, locally or over SSH. The process half of herdi-mac's
/// RelayConnection (runHerdr / runSSH, Sources/RelayConnection.swift:175).
///
/// The SSH flags, the remote binary name and the 15s timeout are lifted from the relay
/// (herdr_relay.py:175) rather than from the mac app, so any host the relay can poll is
/// reachable from here on the same terms — including its per-host serialisation, which
/// exists because concurrent herdr commands down one SSH connection interleave badly.
/// </summary>
public sealed class HerdrCli
{
    private static readonly TimeSpan CommandTimeout = TimeSpan.FromSeconds(15);

    private readonly SettingsStore _settings;
    private readonly Dictionary<string, SemaphoreSlim> _hostGates = new(StringComparer.Ordinal);

    private string? _localBinary;
    private string? _sshBinary;
    private bool _resolved;

    public HerdrCli(SettingsStore settings) => _settings = settings;

    /// <summary>
    /// The local herdr binary, or null when there is none. Null is the normal case on
    /// Windows — herdr runs on the machine the agents run on — so direct mode treats a
    /// missing local binary as "no local host" instead of the hard error the mac app
    /// raises, and polls only the SSH targets.
    /// </summary>
    public string? LocalBinary
    {
        get
        {
            Resolve();
            return _localBinary;
        }
    }

    /// <summary>The OpenSSH client, or null when that optional Windows feature is absent.</summary>
    public string? SshBinary
    {
        get
        {
            Resolve();
            return _sshBinary;
        }
    }

    /// <summary>herdr's name on the remote host. Matches the relay's HERDR_REMOTE_BIN.</summary>
    private static string RemoteBinary =>
        Environment.GetEnvironmentVariable("HERDR_REMOTE_BIN") is { Length: > 0 } name ? name : "herdr";

    /// <summary>Re-run binary discovery, e.g. after the herdr path setting changed.</summary>
    public void Refresh() => _resolved = false;

    /// <summary>
    /// Run `herdr <args>` on <paramref name="host"/>, or locally when it is null.
    /// </summary>
    public async Task<HerdrResult> RunAsync(
        string? host, IReadOnlyList<string> args, CancellationToken token = default)
    {
        if (host is null)
        {
            return LocalBinary is { } local
                ? await RunProcessAsync(local, args, token)
                : HerdrResult.Fail("herdr not found — set its path in Settings");
        }

        if (SshBinary is not { } ssh)
        {
            return HerdrResult.Fail("no ssh.exe — add the OpenSSH Client optional feature");
        }

        // BatchMode=yes forbids every prompt, so the host has to accept a key or an
        // ssh-agent identity. Windows has no sshpass, and the mac app's password path
        // puts the secret in argv where any process can read it, so it is not ported.
        var argv = new List<string>
        {
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            host,
            RemoteBinary,
        };
        argv.AddRange(args);

        var gate = GateFor(host);
        await gate.WaitAsync(token);
        try
        {
            return await RunProcessAsync(ssh, argv, token);
        }
        finally
        {
            gate.Release();
        }
    }

    public static bool MachinesEnabled => !new[] { "off", "0", "false", "no" }.Contains(
        Environment.GetEnvironmentVariable("HERDR_MACHINES")?.Trim(), StringComparer.OrdinalIgnoreCase);

    /// <summary>Read the local registry, not the selected machine. Malformed discovery fails closed.</summary>
    public async Task<List<DirectRoute>> DiscoverMachinesAsync(CancellationToken token = default)
    {
        if (!MachinesEnabled || LocalBinary is null) return new();
        var result = await RunAsync(null, new[] { "machine", "list", "--json" }, token);
        if (!result.Ok) throw new InvalidOperationException(result.Error ?? "machine discovery failed");
        using var doc = JsonDocument.Parse(result.Output.Trim().TrimStart('\uFEFF'));
        var machines = doc.RootElement;
        if (machines.ValueKind == JsonValueKind.Object) machines = machines.GetProperty("machines");
        if (machines.ValueKind != JsonValueKind.Array) throw new InvalidOperationException("invalid machine list");
        var routes = new List<DirectRoute>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in machines.EnumerateArray())
        {
            var id = AgentData.Str(item, "id") ?? "";
            var target = AgentData.Str(item, "target") ?? "";
            var session = AgentData.Str(item, "session") ?? "";
            if (item.TryGetProperty("label", out var rawLabel) &&
                rawLabel.ValueKind is not (JsonValueKind.String or JsonValueKind.Null))
                throw new InvalidOperationException("invalid machine label");
            var label = AgentData.Str(item, "label");
            if (string.IsNullOrEmpty(label)) label = id;
            if (!Regex.IsMatch(id, @"\A[A-Za-z0-9_.-]{1,128}\z") || !seen.Add(id) ||
                target.Length == 0 || target.StartsWith('-') || target.Any(char.IsWhiteSpace) || session.Length == 0 ||
                !item.TryGetProperty("enabled", out var enabled) ||
                enabled.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new InvalidOperationException("invalid machine profile");
            if (enabled.GetBoolean()) routes.Add(new DirectRoute("machine", target, session, id, label));
        }
        return routes;
    }

    /// <summary>Never infer a route from a label or a pane prefix, even after discovery failure.</summary>
    public async Task<HerdrResult> RunRouteAsync(DirectRoute? route, IReadOnlyList<string> args, CancellationToken token = default)
    {
        if (route is null) return HerdrResult.Fail("pane route unavailable — refresh the pane list");
        if (route.Kind == "local")
            return LocalBinary is null ? HerdrResult.Fail("herdr not found") : await RunAsync(null, args, token);
        if (route.Kind == "ssh")
            return _settings.Remotes.Contains(route.Target, StringComparer.Ordinal)
                ? await RunAsync(route.Target, args, token)
                : HerdrResult.Fail("SSH source is no longer configured");
        if (route.Kind != "machine") return HerdrResult.Fail("unknown pane route");
        try
        {
            var current = await DiscoverMachinesAsync(token);
            if (!current.Any(r => r.Key == route.Key))
                return HerdrResult.Fail("machine removed, disabled or changed — refresh the pane list");
            var routed = new List<string> { "--machine", route.ProfileId };
            routed.AddRange(args);
            return await RunAsync(null, routed, token);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex) { return HerdrResult.Fail("machine discovery: " + ex.Message); }
    }

    private SemaphoreSlim GateFor(string host)
    {
        lock (_hostGates)
        {
            if (!_hostGates.TryGetValue(host, out var gate))
            {
                gate = new SemaphoreSlim(1, 1);
                _hostGates[host] = gate;
            }
            return gate;
        }
    }

    private static async Task<HerdrResult> RunProcessAsync(
        string executable, IReadOnlyList<string> args, CancellationToken token)
    {
        var info = new ProcessStartInfo(executable)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            // herdr emits UTF-8 JSON; the console default would mangle non-ASCII paths.
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        // The profile, not the desktop process's inherited pane/session, owns routing.
        if (args.Count >= 2 && args[0] == "--machine")
        {
            info.Environment.Remove("HERDR_SESSION");
            info.Environment.Remove("HERDR_SOCKET_PATH");
        }
        // ArgumentList quotes each argument for us, which matters for send-text payloads
        // carrying spaces, quotes or newlines.
        foreach (var arg in args) info.ArgumentList.Add(arg);

        using var process = new Process { StartInfo = info };
        try
        {
            if (!process.Start()) return HerdrResult.Fail($"could not start {Path.GetFileName(executable)}");
        }
        catch (Exception ex)
        {
            return HerdrResult.Fail(ex.Message);
        }

        // Both pipes are drained before waiting: a child that fills one while we wait on
        // exit would deadlock.
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(CommandTimeout);
        try
        {
            await process.WaitForExitAsync(deadline.Token);
        }
        catch (OperationCanceledException)
        {
            try
            {
                process.Kill(entireProcessTree: true);
            }
            catch (Exception)
            {
                // Already gone, or gone before we could reach it. Either way it is over.
            }

            if (token.IsCancellationRequested) throw;
            return HerdrResult.Fail($"timed out after {CommandTimeout.TotalSeconds:0}s");
        }

        var output = await stdout;
        var error = await stderr;
        return process.ExitCode == 0
            ? new HerdrResult(true, output, null)
            : new HerdrResult(false, output, FirstLine(error) ?? $"exit code {process.ExitCode}");
    }

    private void Resolve()
    {
        if (_resolved) return;
        _resolved = true;
        _localBinary = ResolveLocalBinary();
        _sshBinary = FindOnPath("ssh") ?? SystemOpenSsh();
    }

    /// <summary>
    /// Settings override, then HERDR_BIN, then PATH — the same order as herdi-mac's
    /// resolveHerdrPath, minus its list of Homebrew install locations.
    /// </summary>
    private string? ResolveLocalBinary()
    {
        if (_settings.HerdrPath is { Length: > 0 } configured && File.Exists(configured)) return configured;

        var fromEnv = Environment.GetEnvironmentVariable("HERDR_BIN");
        if (fromEnv is { Length: > 0 } && File.Exists(fromEnv)) return fromEnv;

        return FindOnPath("herdr");
    }

    private static string? SystemOpenSsh()
    {
        var root = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        if (root.Length == 0) return null;
        var path = Path.Combine(root, "System32", "OpenSSH", "ssh.exe");
        return File.Exists(path) ? path : null;
    }

    /// <summary>
    /// PATH lookup honouring PATHEXT. Stands in for the `/usr/bin/which` call herdi-mac
    /// shells out to; Windows has `where.exe`, but not spawning a process to find one is
    /// cheaper and cannot be defeated by a missing PATH entry for where itself.
    /// </summary>
    private static string? FindOnPath(string name)
    {
        var extensions = (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD;.BAT")
            .Split(';', StringSplitOptions.RemoveEmptyEntries);

        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? string.Empty)
                 .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            string candidate;
            try
            {
                candidate = Path.Combine(directory.Trim('"'), name);
            }
            catch (ArgumentException)
            {
                // A PATH entry with invalid path characters is not worth failing over.
                continue;
            }

            if (File.Exists(candidate)) return candidate;
            foreach (var extension in extensions)
            {
                if (File.Exists(candidate + extension)) return candidate + extension;
            }
        }

        return null;
    }

    private static string? FirstLine(string text)
    {
        foreach (var line in text.Split('\n'))
        {
            var trimmed = line.Trim();
            if (trimmed.Length > 0) return trimmed.Length > 120 ? trimmed[..120] : trimmed;
        }
        return null;
    }
}
