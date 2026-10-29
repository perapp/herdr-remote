import AppKit
import Foundation
import Network
import Observation
import UserNotifications
import os.log

private let notificationLog = Logger(subsystem: "com.herdr.herdi", category: "Notifications")

@Observable
final class RelayConnection {
    var agents: [Agent] = []
    var isConnected = false
    var hostAddress = "ws://127.0.0.1:8375"
    var mode: ConnectionMode = .direct
    var herdrError: String? = nil  // Surfaces binary-not-found etc.

    enum ConnectionMode: String, CaseIterable {
        case direct = "Direct (herdr CLI)"
        case relay = "Relay (WebSocket)"
    }

    private var task: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)
    private var pollTimer: Timer?
    private var reconnectAttempt = 0
    private var reconnecting = false
    private var herdrPath: String = ""
    var remotes: [String] = [] // SSH targets, e.g. ["user@host"]

    init() {
        herdrPath = resolveHerdrPath()
        // Load saved remotes
        if let saved = UserDefaults.standard.stringArray(forKey: "herdi_remotes") {
            remotes = saved
        }
        startDirect()
    }

    /// Resolve herdr binary: UserDefaults override → HERDR_BIN env → PATH lookup → common locations
    private func resolveHerdrPath() -> String {
        // 1. UserDefaults override (set via Settings)
        if let custom = UserDefaults.standard.string(forKey: "herdi_herdr_path"),
           !custom.isEmpty, FileManager.default.isExecutableFile(atPath: custom) {
            return custom
        }
        // 2. HERDR_BIN environment variable
        if let envPath = ProcessInfo.processInfo.environment["HERDR_BIN"],
           FileManager.default.isExecutableFile(atPath: envPath) {
            return envPath
        }
        // 3. Resolve via PATH using /usr/bin/which
        let whichProcess = Process()
        whichProcess.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        whichProcess.arguments = ["herdr"]
        let pipe = Pipe()
        whichProcess.standardOutput = pipe
        whichProcess.standardError = FileHandle.nullDevice
        do {
            try whichProcess.run()
            whichProcess.waitUntilExit()
            if whichProcess.terminationStatus == 0 {
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                if let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !path.isEmpty, FileManager.default.isExecutableFile(atPath: path) {
                    return path
                }
            }
        } catch {}
        // 4. Common install locations
        let commonPaths = [
            "/opt/homebrew/bin/herdr",
            "/usr/local/bin/herdr",
            NSString(string: "~/.local/bin/herdr").expandingTildeInPath,
            NSString(string: "~/bin/herdr").expandingTildeInPath
        ]
        for path in commonPaths {
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        // Not found — return empty and set error in pollHerdr
        return ""
    }

    // MARK: - Direct Mode (polls herdr CLI)

    func startDirect() {
        mode = .direct
        agents = []
        paneRoutes = [:]
        task?.cancel(with: .normalClosure, reason: nil)
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollHerdr()
        }
        pollHerdr() // immediate first poll
    }

    private var polling = false

    private struct DirectRoute: Equatable {
        let kind: String
        let target: String
        var profileId = ""
        var session = ""
        var label = ""
        var key: String {
            let data = try! JSONSerialization.data(withJSONObject: [kind, profileId, session, target])
            return String(data: data, encoding: .utf8)!
        }
        var display: String { kind == "local" ? "local" : (label.isEmpty ? target : label) }
    }
    private struct PaneRoute {
        let source: DirectRoute
        let rawPaneId: String
    }
    private var paneRoutes: [String: PaneRoute] = [:]

    /// Selection is a CLI convenience, not an enable/disable flag.
    private func discoverMachines() -> [DirectRoute]? {
        let setting = ProcessInfo.processInfo.environment["HERDR_MACHINES", default: ""].lowercased()
        if ["off", "0", "false", "no"].contains(setting) { return [] }
        let output = runHerdr("machine", "list", "--json")
        guard let data = output.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data),
              let list = (json as? [[String: Any]]) ?? ((json as? [String: Any])?["machines"] as? [[String: Any]]) else { return nil }
        var seen = Set<String>()
        var routes: [DirectRoute] = []
        for item in list {
            guard let id = item["id"] as? String,
                  id.range(of: "^[A-Za-z0-9_.-]{1,128}$", options: .regularExpression) != nil,
                  seen.insert(id).inserted,
                  let target = item["target"] as? String, !target.isEmpty, !target.hasPrefix("-"),
                  !target.contains(where: { $0.isWhitespace }),
                  let session = item["session"] as? String, !session.isEmpty,
                  let enabled = item["enabled"] as? Bool else { return nil }
            if let label = item["label"], !(label is String), !(label is NSNull) { return nil }
            if enabled {
                let label = item["label"] as? String ?? ""
                routes.append(DirectRoute(kind: "machine", target: target, profileId: id, session: session, label: label.isEmpty ? id : label))
            }
        }
        return routes
    }

    /// Each command revalidates registry routing. Discovery failure never becomes local execution.
    private func runRoute(_ route: PaneRoute, _ args: [String]) -> String {
        switch route.source.kind {
        case "machine":
            guard let machines = discoverMachines(), machines.contains(where: { $0.key == route.source.key }) else { return "" }
            return runHerdr(arguments: ["--machine", route.source.profileId] + args)
        case "ssh":
            guard remotes.contains(route.source.target) else { return "" }
            return runSSH(route.source.target, arguments: ["herdr"] + args)
        case "local": return runHerdr(arguments: args)
        default: return ""
        }
    }

    private func currentRoute(_ paneId: String) -> PaneRoute? {
        guard mode == .direct, agents.contains(where: { $0.id == paneId }) else { return nil }
        return paneRoutes[paneId]
    }

    private func pollHerdr() {
        guard mode == .direct, !polling else { return }
        polling = true
        DispatchQueue.global(qos: .utility).async { [self] in
            defer { DispatchQueue.main.async { self.polling = false } }
            // Check if herdr binary is found
            if herdrPath.isEmpty {
                DispatchQueue.main.async { [self] in
                    isConnected = false
                    herdrError = "herdr not found. Install herdr or set path in Settings."
                    agents = []
                }
                return
            }
            
            let machines = discoverMachines()
            let sources = [DirectRoute(kind: "local", target: "")] + remotes.map { DirectRoute(kind: "ssh", target: $0) } + (machines ?? [])
            var allAgents: [ParsedAgent] = []
            var routes: [String: PaneRoute] = [:]
            for source in sources {
                let output = runRoute(PaneRoute(source: source, rawPaneId: ""), ["pane", "list"])
                let parsed = parseAgents(from: output, source: source)
                allAgents += parsed
                for a in parsed { routes[a.id] = PaneRoute(source: source, rawPaneId: a.rawPaneId) }
            }

            DispatchQueue.main.async { [self] in
                guard mode == .direct else { return }
                paneRoutes = routes
                isConnected = true
                herdrError = machines == nil ? "Machine discovery failed; registered machine actions unavailable." : nil
                var seen = Set<String>()
                for a in allAgents {
                    seen.insert(a.id)
                    if let existing = agents.first(where: { $0.id == a.id }) {
                        if existing.status != a.status {
                            if a.status == .blocked && existing.status != .blocked {
                                readPaneForBlocked(existing)
                            }
                            existing.status = a.status
                        }
                        if existing.project != a.project { existing.project = a.project }
                        if existing.host != a.host { existing.host = a.host }
                        if existing.name != a.name { existing.name = a.name }
                        if existing.agentKind != a.agentKind { existing.agentKind = a.agentKind }
                    } else {
                        let agent = Agent(id: a.id, name: a.name, status: a.status, project: a.project, cwd: a.cwd, host: a.host, agentKind: a.agentKind)
                        agents.append(agent)
                        if a.status == .blocked { readPaneForBlocked(agent) }
                    }
                }
                agents.removeAll { !seen.contains($0.id) }
            }
        }
    }

    private struct ParsedAgent {
        let id: String, rawPaneId: String, name: String, status: AgentStatus, project: String, cwd: String, host: String, agentKind: String
    }

    private struct PaneLocation {
        let workspaceId: String
        let tabId: String
    }

    private func parseAgents(from output: String, source: DirectRoute) -> [ParsedAgent] {
        guard let data = output.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let resultObj = json["result"] as? [String: Any],
              let panes = resultObj["panes"] as? [[String: Any]] else { return [] }

        return panes.compactMap { p in
            guard let agent = p["agent"] as? String, !agent.isEmpty else { return nil }
            guard let rawPaneId = p["pane_id"] as? String, !rawPaneId.isEmpty else { return nil }
            let identity = try! JSONSerialization.data(withJSONObject: [source.key, rawPaneId])
            let paneId = String(data: identity, encoding: .utf8)!
            let status = AgentStatus(rawValue: p["agent_status"] as? String ?? "unknown") ?? .unknown
            let cwd = p["cwd"] as? String ?? ""
            return ParsedAgent(id: paneId, rawPaneId: rawPaneId, name: paneTitle(p) ?? agent, status: status, project: (cwd as NSString).lastPathComponent, cwd: cwd, host: source.display, agentKind: agent)
        }
    }

    /// The name a human recognises: the agent's own terminal title, else a pane label set by
    /// hand, else nil so the caller can fall back to the agent binary. Nerd-font glyphs are
    /// stripped from labels, and shell titles are rejected -- herdr reports `user@host:~/path`
    /// for every pane, which says less than the cwd already shown beside it.
    private func paneTitle(_ pane: [String: Any]) -> String? {
        for key in ["terminal_title_stripped", "label"] {
            guard let raw = pane[key] as? String else { continue }
            let cleaned = raw.unicodeScalars
                .filter { $0.value < 0xE000 || ($0.value > 0xF8FF && $0.value < 0xF0000) }
                .reduce(into: "") { $0.unicodeScalars.append($1) }
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if cleaned.isEmpty { continue }
            if cleaned.contains("@") && cleaned.contains(":") { continue }
            return cleaned
        }
        return nil
    }

    private func parsePaneLocation(from output: String) -> PaneLocation? {
        guard let data = output.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let result = json["result"] as? [String: Any],
              let pane = result["pane"] as? [String: Any],
              let workspaceId = pane["workspace_id"] as? String,
              let tabId = pane["tab_id"] as? String else { return nil }
        return PaneLocation(workspaceId: workspaceId, tabId: tabId)
    }

    private func runSSH(_ remote: String, _ args: String...) -> String {
        runSSH(remote, arguments: args)
    }

    private func runSSH(_ remote: String, arguments args: [String]) -> String {
        let process = Process()
        let password = KeychainHelper.getPassword(for: remote)

        if let password, FileManager.default.fileExists(atPath: "/opt/homebrew/bin/sshpass") {
            // Use sshpass for password auth
            process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/sshpass")
            process.arguments = ["-p", password, "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", remote] + args
        } else {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
            process.arguments = ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote] + args
        }

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return "" }
            return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        } catch { return "" }
    }

    func addRemote(_ remote: String, password: String? = nil) {
        guard !remote.isEmpty, !remotes.contains(remote) else { return }
        remotes.append(remote)
        UserDefaults.standard.set(remotes, forKey: "herdi_remotes")
        if let password, !password.isEmpty {
            KeychainHelper.setPassword(password, for: remote)
        }
    }

    func removeRemote(_ remote: String) {
        remotes.removeAll { $0 == remote }
        UserDefaults.standard.set(remotes, forKey: "herdi_remotes")
        KeychainHelper.deletePassword(for: remote)
    }

    private func readPaneForBlocked(_ agent: Agent) {
        guard let route = currentRoute(agent.id) else { return }
        DispatchQueue.global(qos: .utility).async { [self] in
            let lines = readPromptLines(route)
            let ansi = readPromptANSI(route)
            DispatchQueue.main.async {
                guard self.currentRoute(agent.id)?.source.key == route.source.key else { return }
                self.showPrompt(lines, ansi: ansi, on: agent)
                self.sendNotification(agent: agent.name, project: agent.project)
            }
        }
    }

    private func showPrompt(_ lines: [String], ansi: [String] = [], on agent: Agent) {
        let menu = menuOptions(lines)
        agent.prompt = promptExcerpt(lines, menu)
        // Numbered here, as the relay's clients bind key N to option N: the number is what gets sent.
        agent.options = menu.labels.enumerated().map { "\($0.offset + 1). \($0.element)" }
        agent.choiceMenu = menu.labels.isEmpty ? detectChoiceMenu(plain: lines, ansi: ansi) : nil
    }

    /// The same screen as `readPromptLines`, with its colour codes. A row of choices side by side
    /// marks the selected one only by colour.
    private func readPromptANSI(_ route: PaneRoute) -> [String] {
        let raw = runRoute(route, ["pane", "read", route.rawPaneId, "--lines", "60", "--source", "visible", "--format", "ansi"])
        return raw.components(separatedBy: .newlines)
    }

    private func readPromptLines(_ route: PaneRoute) -> [String] {
        // `visible`, not `recent`, as the relay does (PROMPT_READ_SOURCE). `recent` past the
        // pane's viewport makes herdr harvest the extra rows by walking the agent's own
        // scroll interface, which moves the operator's terminal -- something a read fired by
        // a status change must never do. 20 rows sits inside most viewports, so this is
        // usually a no-op; on a pane split down under 20 rows it is not. The prompt is on
        // screen by definition, so nothing is given up either way.
        let raw = runRoute(route, ["pane", "read", route.rawPaneId, "--lines", "60", "--source", "visible"])
        // Read the whole viewport, not a 20-row slice of it. `--source visible` never scrolls
        // the pane whatever the count, so the old limit bought nothing and cost the options:
        // a question rendered with a preview panel beside it pushes them well above the tail.
        return Array(
            raw.components(separatedBy: .newlines)
                .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
                .suffix(60)
        )
    }

    /// Read the numbered choices out of the prompt the agent is actually showing, e.g.
    ///
    ///     Do you want to proceed?
    ///     > 1. Yes
    ///       2. No
    ///
    /// returns ["1. Yes", "2. No"]. The number is what gets typed back, so it is kept on the
    /// front of each entry. An unrecognised prompt returns nothing, which leaves the card with
    /// its free-text reply field rather than buttons that send a string the agent never offered.

    private func runHerdr(_ args: String...) -> String {
        runHerdr(arguments: args)
    }

    private func runHerdr(arguments args: [String]) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: herdrPath)
        process.arguments = Array(args)
        if args.first == "--machine" {
            var environment = ProcessInfo.processInfo.environment
            for key in ["HERDR_SESSION", "HERDR_SOCKET_PATH"] { environment.removeValue(forKey: key) }
            process.environment = environment
        }
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return "" }
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return ""
        }
    }

    // MARK: - Relay Mode (WebSocket)

    func connectRelay(to urlString: String) {
        guard let url = URL(string: urlString) else { return }
        mode = .relay
        agents = []
        paneRoutes = [:]
        hostAddress = urlString
        pollTimer?.invalidate()
        pollTimer = nil
        reconnecting = false
        task?.cancel(with: .normalClosure, reason: nil)
        task = session.webSocketTask(with: url)
        task?.resume()
        reconnectAttempt = 0
        listen()
    }

    func disconnect() {
        task?.cancel(with: .normalClosure, reason: nil)
        pollTimer?.invalidate()
        isConnected = false
    }

    func send(response: ResponseMessage) {
        if mode == .direct {
            let paneId = response.pane_id
            guard let route = currentRoute(paneId) else { return }
            let agent = agents.first(where: { $0.id == paneId })
            // The prompt the card showed when the answer was given. Read here, before the card
            // clears it, so the check below compares against what you actually saw.
            let shownPrompt = agent?.prompt
            DispatchQueue.global(qos: .userInitiated).async { [self] in
                // A menu number goes in as a key press, as the relay sends it, with no Enter: the
                // Enter would fall through to whatever the agent shows next. Free text still needs
                // its Enter.
                let isMenuChoice = !response.text.isEmpty && response.text.allSatisfy(\.isNumber)
                let verb = isMenuChoice ? "send-keys" : "send-text"
                let payload = isMenuChoice ? response.text : response.text + "\n"
                if isMenuChoice {
                    let lines = readPromptLines(route)
                    guard promptStillShown(shownPrompt, lines: lines) else {
                        // Not sent: the menu on screen is not the one on the card. Show the
                        // current one instead, so the next answer is to what is really there.
                        DispatchQueue.main.async {
                            guard let agent else { return }
                            self.showPrompt(lines, on: agent)
                            agent.status = .blocked
                        }
                        return
                    }
                }
                _ = runRoute(route, ["pane", verb, route.rawPaneId, payload])
            }

        } else {
            guard let data = try? JSONEncoder().encode(response) else { return }
            task?.send(.string(String(data: data, encoding: .utf8)!)) { _ in }
        }
    }

    func toggleQuestionOption(paneId: String, promptId: String, option: String) {
        guard mode == .relay,
              let data = try? JSONEncoder().encode(
                QuestionToggleMessage(pane_id: paneId, prompt_id: promptId, option: option)
              ) else { return }
        task?.send(.string(String(data: data, encoding: .utf8)!)) { _ in }
    }

    func submitQuestion(paneId: String, promptId: String) {
        guard mode == .relay,
              let data = try? JSONEncoder().encode(
                QuestionSubmitMessage(pane_id: paneId, prompt_id: promptId)
              ) else { return }
        task?.send(.string(String(data: data, encoding: .utf8)!)) { _ in }
    }

    func focusPane(_ paneId: String) {
        // Focusing has no relay protocol equivalent. Never reinterpret a relay ID locally.
        guard let route = currentRoute(paneId) else { return }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let output = runRoute(route, ["pane", "get", route.rawPaneId])
            guard let location = parsePaneLocation(from: output) else { return }
            _ = runRoute(route, ["workspace", "focus", location.workspaceId])
            _ = runRoute(route, ["tab", "focus", location.tabId])
        }
    }

    /// Press keys in an agent's pane, then show its screen again so the card follows the cursor.
    /// Used for menus with no numbers: a list with a cursor, or choices side by side such as
    /// opencode's "Allow once  Allow always  Reject". With `expecting`, the keys go in only while
    /// that menu, with that choice selected, is still on screen. With `shownPrompt`, they go in
    /// only while the card's prompt is still on screen. Relay mode cannot check either: the
    /// relay's send_keys message carries no prompt id.
    func sendKeys(_ keys: [String], to paneId: String, expecting menu: ChoiceMenu? = nil, shownPrompt: String? = nil) {
        guard mode == .direct else {
            let message: [String: Any] = ["type": "send_keys", "pane_id": paneId, "keys": keys]
            guard let data = try? JSONSerialization.data(withJSONObject: message),
                  let text = String(data: data, encoding: .utf8) else { return }
            task?.send(.string(text)) { _ in }
            return
        }
        guard let route = currentRoute(paneId) else { return }
        let agent = agents.first(where: { $0.id == paneId })
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            if menu != nil || shownPrompt != nil {
                let lines = readPromptLines(route)
                let ansi = readPromptANSI(route)
                let unchanged = menu.map { detectChoiceMenu(plain: lines, ansi: ansi) == $0 }
                    ?? promptStillShown(shownPrompt, lines: lines)
                guard unchanged else {
                    DispatchQueue.main.async {
                        guard let agent else { return }
                        self.showPrompt(lines, ansi: ansi, on: agent)
                    }
                    return
                }
            }
            // One call for every key, so nothing on screen can change between them.
            _ = runRoute(route, ["pane", "send-keys", route.rawPaneId] + keys)
            Thread.sleep(forTimeInterval: 0.2)
            let lines = readPromptLines(route)
            let ansi = readPromptANSI(route)
            DispatchQueue.main.async {
                guard let agent else { return }
                self.showPrompt(lines, ansi: ansi, on: agent)
            }
        }
    }

    func interruptPane(_ paneId: String) {
        if mode == .relay {
            sendKeys(["C-c"], to: paneId)
            return
        }
        guard let route = currentRoute(paneId) else { return }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            _ = runRoute(route, ["pane", "send-keys", route.rawPaneId, "Ctrl+c"])
        }
    }

    private func listen() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                DispatchQueue.main.async { if !self.isConnected { self.isConnected = true } }
                switch message {
                case .string(let text): self.handleWS(text)
                case .data(let data): self.handleWS(String(data: data, encoding: .utf8) ?? "")
                @unknown default: break
                }
                self.listen()
            case .failure:
                DispatchQueue.main.async {
                    self.isConnected = false
                    self.scheduleReconnect()
                }
            }
        }
    }

    private func scheduleReconnect() {
        guard !reconnecting, mode == .relay else { return }
        reconnecting = true
        reconnectAttempt += 1
        let delay = min(Double(1 << min(reconnectAttempt, 5)), 30.0)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.isConnected else { return }
            self.reconnecting = false
            self.connectRelay(to: self.hostAddress)
        }
    }

    private func handleWS(_ text: String) {
        guard let data = text.data(using: .utf8),
              let msg = try? JSONDecoder().decode(AgentMessage.self, from: data) else { return }
        DispatchQueue.main.async { [self] in
            switch msg.type {
            case "agents":
                guard let list = msg.agents else { return }
                var seen = Set<String>()
                for a in list {
                    seen.insert(a.pane_id)
                    upsertAgent(a)
                }
                agents.removeAll { !seen.contains($0.id) }
            case "agent_update":
                if let update = msg.agentData { upsertAgent(update) }
            case "blocked":
                if let pid = msg.pane_id, let agent = agents.first(where: { $0.id == pid }) {
                    agent.prompt = msg.prompt
                    agent.promptId = msg.prompt_id
                    agent.options = msg.options
                    agent.multiOptions = msg.multi_options ?? []
                    agent.selectedOptions = msg.selected_options ?? []
                    agent.interaction = msg.interaction
                    agent.isMultiSelect = msg.multi ?? false
                    agent.status = .blocked
                    if msg.update != true {
                        sendNotification(agent: agent.name, project: agent.project)
                    }
                }
            default: break
            }
        }
    }

    private func upsertAgent(_ data: AgentMessage.AgentData) {
        if let existing = agents.first(where: { $0.id == data.pane_id }) {
            existing.name = data.agent
            existing.agentKind = data.agent
            existing.status = AgentStatus(rawValue: data.status) ?? .unknown
            existing.project = data.project
            existing.cwd = data.cwd
            existing.host = data.source_label.flatMap { $0.isEmpty ? nil : $0 } ?? data.host ?? "local"
            return
        }
        agents.append(Agent(
            id: data.pane_id, name: data.agent,
            status: AgentStatus(rawValue: data.status) ?? .unknown,
            project: data.project, cwd: data.cwd, host: data.source_label.flatMap { $0.isEmpty ? nil : $0 } ?? data.host ?? "local",
            agentKind: data.agent
        ))
    }

    /// Fires a notification down the same path a blocked agent uses, so the Sound toggle can be
    /// checked without waiting for one. Silence here means the toggle is off, or macOS has the
    /// sound switched off for Herdi in System Settings; nothing appearing at all means the app
    /// was never granted notification permission.
    func sendTestNotification() {
        sendNotification(agent: "Herdi", project: "notification test")
    }

    private func sendNotification(agent: String, project: String) {
        let center = UNUserNotificationCenter.current()
        let content = UNMutableNotificationContent()
        content.title = "Agent Blocked"
        content.body = "\(agent) needs input in \(project)"
        let soundWanted = Quiet.shouldPlaySound
        content.sound = soundWanted ? .default : nil
        center.getNotificationSettings { settings in
            let macOSPlaysSound = settings.authorizationStatus == .authorized && settings.soundSetting == .enabled
            if Quiet.playsOwnSound(macOSPlaysSound: macOSPlaysSound, soundWanted: soundWanted) {
                DispatchQueue.main.async { NSSound(named: Quiet.ownSoundName)?.play() }
                notificationLog.notice("macOS will not play the alert sound, so Herdi played it")
            }
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }
}
