import Combine
import Foundation
import PKMBrainKit
import SwiftUI
import UserNotifications

@MainActor
final class AppState: ObservableObject {
    enum Destination: String, CaseIterable, Identifiable {
        case today = "Today"
        case queue = "Queue"
        case wiki = "Wiki"
        case entities = "Entities"
        case ask = "Ask"
        case ops = "Ops"
        case settings = "Settings"

        var id: String { rawValue }

        var symbol: String {
            switch self {
            case .today: return "sun.max"
            case .queue: return "tray.full"
            case .wiki: return "doc.text"
            case .entities: return "person.2"
            case .ask: return "text.magnifyingglass"
            case .ops: return "slider.horizontal.3"
            case .settings: return "gearshape"
            }
        }
    }

    @Published var selectedDestination: Destination
    @Published var digest: Digest?
    @Published var todayBriefing: TodayBriefing?
    @Published var todayError: String?
    @Published var todayFeedbackMessage: String?
    @Published private(set) var isRunningTodayShadow = false
    @Published var todayShadowRunMessage: String?
    @Published var todayShadowRunError: String?
    @Published private(set) var queueSummary: QueueSummary?
    @Published var lastError: String?
    @Published var notificationsEnabled = true
    @Published var loginItemEnabled = false
    @Published var serveWeb = false
    @Published var homePath: String
    @Published var migrationPlan: MigrationPlan?
    @Published var migrationActionMessage: String?
    @Published var requestedWikiPath: String?
    @Published var requestedEntityID: String?
    @Published var requestedQueueState: String?

    let provisioner: RuntimeProvisioner
    let daemon: DaemonSupervisor
    private var didStart = false
    private var daemonCancellable: AnyCancellable?
    private var handshakeCancellable: AnyCancellable?
    private var monitorTask: Task<Void, Never>?
    private var queueSummaryDaemonPID: Int?
    private let queueBacklogThreshold = 100
    private let queueBacklogNotificationKey = "PKMBrain.queueBacklogNotificationAt"
    private let homeDefaultsKey = "PKMBrain.homePath"
    private let nightlyFailureNotificationKey = "PKMBrain.nightlyFailureNotification"
    private let daemonFailureNotificationKey = "PKMBrain.daemonFailureNotification"
    private let connectorFailureNotificationPrefix = "PKMBrain.connectorFailureNotification."

    init(initialDestination: Destination = .today) {
        let defaultHome = ProcessInfo.processInfo.environment["PKM_BRAIN_HOME"]
            ?? UserDefaults.standard.string(forKey: "PKMBrain.homePath")
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("brain").path
        selectedDestination = initialDestination
        homePath = defaultHome
        provisioner = RuntimeProvisioner()
        daemon = DaemonSupervisor(provisioner: provisioner)
        daemonCancellable = daemon.objectWillChange.sink { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.objectWillChange.send()
            }
        }
        handshakeCancellable = daemon.$handshake
            .map { $0?.pid }
            .removeDuplicates()
            .sink { [weak self] pid in
                Task { @MainActor [weak self] in
                    self?.handleDaemonPID(pid)
                }
            }
    }

    var homeURL: URL {
        URL(fileURLWithPath: NSString(string: homePath).expandingTildeInPath)
    }

    var menuBarSymbol: String {
        switch daemon.status {
        case .running:
            if daemon.scheduler?.paused_until != nil {
                return "pause.circle"
            }
            if daemon.scheduler?.jobs.contains(where: { $0.running || ($0.queued ?? false) }) == true {
                return "arrow.triangle.2.circlepath"
            }
            return "brain.head.profile"
        case .failed:
            return "exclamationmark.triangle"
        case .restarting:
            return "arrow.clockwise"
        default:
            return "circle"
        }
    }

    var queueTotal: Int {
        queueSummary?.actionable_total
            ?? digest?.queue_summary?.actionable_total
            ?? digest?.queue_counts.total
            ?? 0
    }

    func acceptQueueSummary(_ candidate: QueueSummary?) {
        guard let candidate else {
            return
        }
        let daemonPID = daemon.handshake?.pid
        if let serverPID = candidate.server_pid, serverPID != daemonPID {
            return
        }
        if let summaryHome = candidate.home,
           URL(fileURLWithPath: summaryHome).standardizedFileURL != homeURL.standardizedFileURL {
            return
        }
        if queueSummaryDaemonPID != daemonPID {
            queueSummary = nil
            queueSummaryDaemonPID = daemonPID
        }
        if let current = queueSummary, current.as_of > candidate.as_of {
            return
        }
        queueSummary = candidate
        notifyQueueBacklogIfNeeded(count: candidate.actionable_total)
    }

    func showWiki(path: String) {
        requestedWikiPath = path
        selectedDestination = .wiki
    }

    func showEntity(id: String) {
        requestedEntityID = id
        selectedDestination = .entities
    }

    func showQueue(state: String = "actionable") {
        requestedQueueState = state
        selectedDestination = .queue
    }

    func switchHome(to requestedPath: String) async -> Bool {
        let expanded = NSString(string: requestedPath).expandingTildeInPath
        let target = URL(fileURLWithPath: expanded, isDirectory: true).standardizedFileURL
        let previous = homeURL.standardizedFileURL
        guard expanded.hasPrefix("/"), target != previous else {
            return target == previous
        }

        await daemon.stop()
        await daemon.start(homeURL: target, serveWeb: serveWeb)
        if daemon.handshake.map({
            URL(fileURLWithPath: $0.home).standardizedFileURL == target
        }) == true {
            homePath = target.path
            UserDefaults.standard.set(target.path, forKey: homeDefaultsKey)
            queueSummary = nil
            digest = nil
            todayBriefing = nil
            todayError = nil
            todayFeedbackMessage = nil
            todayShadowRunMessage = nil
            todayShadowRunError = nil
            await refreshMigrationPlan()
            await refreshDigest()
            await refreshToday()
            lastError = nil
            return true
        }

        let targetFailure = daemon.status.label
        await daemon.stop()
        await daemon.start(homeURL: previous, serveWeb: serveWeb)
        homePath = previous.path
        await refreshMigrationPlan()
        await refreshDigest()
        await refreshToday()
        lastError = "Could not open \(target.path) (\(targetFailure)); restored \(previous.path)."
        return false
    }

    func waitForAPIClient(maxAttempts: Int = 100) async -> BrainAPIClient? {
        for _ in 0..<maxAttempts {
            guard !Task.isCancelled else {
                return nil
            }
            if let client = daemon.apiClient {
                return client
            }
            do {
                try await Task.sleep(nanoseconds: 200_000_000)
            } catch {
                return nil
            }
        }
        return daemon.apiClient
    }

    func start() async {
        guard !didStart else {
            return
        }
        didStart = true
        await daemon.start(homeURL: homeURL, serveWeb: serveWeb)
        if ProcessInfo.processInfo.environment["PKM_BRAIN_UI_TEST"] != "1" {
            await requestNotificationAuthorizationIfNeeded()
        }
        await refreshMigrationPlan()
        await refreshDigest()
        await refreshToday()
        notifyDaemonFailureIfNeeded()
        startMonitor()
    }

    func refreshMigrationPlan() async {
        guard let client = daemon.apiClient else {
            return
        }
        do {
            migrationPlan = try await client.migrationPlan()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func refreshDigest() async {
        guard let client = daemon.apiClient else {
            return
        }
        do {
            let latestDigest = try await client.digest()
            digest = latestDigest
            acceptQueueSummary(latestDigest.queue_summary)
            lastError = nil
            notifyNightlyFailureIfNeeded(run: latestDigest.latest_run)
            await notifyConnectorFailuresIfNeeded(client: client)
        } catch {
            lastError = error.localizedDescription
        }
    }

    func refreshToday() async {
        guard let client = daemon.apiClient else {
            return
        }
        do {
            let briefing = try await client.todayBriefing()
            guard briefing.schema_version == 1 else {
                todayBriefing = nil
                todayError = "This Today briefing uses an unsupported schema version."
                return
            }
            todayBriefing = briefing
            todayError = nil
        } catch APIClientError.httpStatus(404, _) {
            // Compatibility with an older daemon: retain the existing digest Today view.
            todayBriefing = nil
            todayError = nil
        } catch {
            todayError = error.localizedDescription
        }
    }

    func runTodayShadow() async {
        guard !isRunningTodayShadow else {
            return
        }
        guard let client = daemon.apiClient else {
            todayShadowRunMessage = nil
            todayShadowRunError = "The Brain service is not available."
            return
        }

        isRunningTodayShadow = true
        todayShadowRunMessage = "Running read-only Calendar and Gmail shadow scan…"
        todayShadowRunError = nil
        defer {
            isRunningTodayShadow = false
        }

        do {
            var result = try await client.runTodayShadow()
            todayShadowRunMessage = result.message
            while result.isInProgress {
                try await Task.sleep(nanoseconds: 2_000_000_000)
                result = try await client.todayShadowRunStatus()
                todayShadowRunMessage = result.message
            }
            if result.succeeded {
                todayShadowRunMessage = result.message
                await refreshToday()
            } else {
                todayShadowRunMessage = nil
                todayShadowRunError = result.message
            }
        } catch {
            todayShadowRunMessage = nil
            todayShadowRunError = error.localizedDescription
        }
    }

    func submitTodayFeedback(
        itemID: String,
        action: String,
        note: String? = nil,
        snoozedUntil: String? = nil
    ) async {
        guard let client = daemon.apiClient else {
            todayFeedbackMessage = "The Brain service is not available."
            return
        }
        do {
            let result = try await client.submitTodayFeedback(
                itemID: itemID,
                action: action,
                note: note,
                snoozedUntil: snoozedUntil
            )
            todayFeedbackMessage = result.message
            if result.status == "accepted" {
                await refreshToday()
            }
        } catch {
            todayFeedbackMessage = error.localizedDescription
        }
    }

    func reportMissingTodayItem(
        title: String,
        detail: String?,
        sourceHint: String?
    ) async {
        guard let client = daemon.apiClient else {
            todayFeedbackMessage = "The Brain service is not available."
            return
        }
        do {
            let result = try await client.reportMissingTodayItem(
                title: title,
                detail: detail,
                sourceHint: sourceHint
            )
            todayFeedbackMessage = result.message
            if result.status == "accepted" {
                await refreshToday()
            }
        } catch {
            todayFeedbackMessage = error.localizedDescription
        }
    }

    func openTodayBrainRoute(_ route: String) {
        if route.hasPrefix("wiki:") {
            showWiki(path: String(route.dropFirst("wiki:".count)))
            return
        }
        guard let components = URLComponents(string: route) else {
            todayFeedbackMessage = "The evidence route is not available."
            return
        }
        if components.path == "/wiki",
           let path = components.queryItems?.first(where: { $0.name == "path" })?.value {
            showWiki(path: path)
            return
        }
        if components.path.hasPrefix("/entities/") {
            let value = String(components.path.dropFirst("/entities/".count))
            showEntity(id: value.removingPercentEncoding ?? value)
            return
        }
        if components.path == "/queue" {
            let state = components.queryItems?.first(where: { $0.name == "state" })?.value
                ?? "actionable"
            showQueue(state: state)
            return
        }
        todayFeedbackMessage = "The evidence route is not available in this app build."
    }

    func runCaptureNow() async {
        await daemon.runCaptureNow()
        await refreshDigest()
    }

    func pauseOneHour() async {
        await daemon.pauseOneHour()
    }

    func resumeScheduler() async {
        await daemon.resume()
    }

    func installMigrationShims() async {
        guard let client = daemon.apiClient else {
            return
        }
        do {
            _ = try await client.installMigrationShims()
            let shimDir = migrationPlan?.shim_dir ?? "the app bin directory"
            migrationActionMessage = "CLI shims installed in \(shimDir)"
            await refreshMigrationPlan()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func dryRunLaunchAgentRetirement() async {
        guard let client = daemon.apiClient else {
            return
        }
        do {
            _ = try await client.dryRunLaunchAgentRetirement()
            migrationActionMessage = "LaunchAgent retirement dry run complete"
        } catch {
            lastError = error.localizedDescription
        }
    }

    func shutdown() async {
        monitorTask?.cancel()
        monitorTask = nil
        await daemon.stop()
        didStart = false
    }

    private func startMonitor() {
        monitorTask?.cancel()
        monitorTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 60_000_000_000)
                guard !Task.isCancelled else {
                    return
                }
                await self?.refreshDigest()
                await self?.refreshToday()
                self?.notifyDaemonFailureIfNeeded()
            }
        }
    }

    private func handleDaemonPID(_ pid: Int?) {
        guard queueSummaryDaemonPID != pid else {
            return
        }
        queueSummaryDaemonPID = pid
        queueSummary = nil
        digest = nil
        todayBriefing = nil
        todayError = nil
        todayFeedbackMessage = nil
        todayShadowRunMessage = nil
        todayShadowRunError = nil
        if pid != nil, didStart {
            Task {
                await refreshDigest()
                await refreshToday()
            }
        }
    }

    private func requestNotificationAuthorizationIfNeeded() async {
        guard notificationsEnabled else {
            return
        }
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
    }

    private func notifyQueueBacklogIfNeeded(count: Int) {
        guard notificationsEnabled, count >= queueBacklogThreshold else {
            return
        }
        let defaults = UserDefaults.standard
        let now = Date()
        let lastTimestamp = defaults.double(forKey: queueBacklogNotificationKey)
        if lastTimestamp > 0 {
            let last = Date(timeIntervalSince1970: lastTimestamp)
            guard now.timeIntervalSince(last) >= 7 * 24 * 60 * 60 else {
                return
            }
        }

        let content = UNMutableNotificationContent()
        content.title = "PKM Brain Queue"
        content.body = "\(count) items need review."
        content.sound = .default
        content.userInfo = ["destination": "queue"]
        let request = UNNotificationRequest(
            identifier: "pkm-brain-queue-backlog",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
        defaults.set(now.timeIntervalSince1970, forKey: queueBacklogNotificationKey)
    }

    private func notifyNightlyFailureIfNeeded(run: DigestRun?) {
        guard notificationsEnabled,
              let run,
              let status = run.status,
              status != "success"
        else {
            return
        }
        let jobName = run.job_name ?? ""
        guard jobName.contains("nightly") || jobName.isEmpty else {
            return
        }
        let marker = run.id
            ?? "\(jobName):\(run.finished_at ?? run.started_at ?? ""):\(status)"
        guard UserDefaults.standard.string(forKey: nightlyFailureNotificationKey) != marker else {
            return
        }
        postNotification(
            identifier: "pkm-brain-nightly-failed",
            title: "PKM Brain Nightly Failed",
            body: run.error ?? "The latest nightly run finished with status \(status).",
            destination: "ops"
        )
        UserDefaults.standard.set(marker, forKey: nightlyFailureNotificationKey)
    }

    private func notifyConnectorFailuresIfNeeded(client: BrainAPIClient) async {
        guard notificationsEnabled else {
            return
        }
        guard let response = try? await client.connectors() else {
            return
        }
        for connector in response.connectors where connector.health.consecutive_failures >= 3 {
            let marker = "\(connector.health.consecutive_failures):\(connector.health.last_run_at ?? "")"
            let key = connectorFailureNotificationPrefix + connector.id
            guard UserDefaults.standard.string(forKey: key) != marker else {
                continue
            }
            let displayName = connector.manifest.display_name
            let error = connector.health.last_error ?? connector.health.status
            postNotification(
                identifier: "pkm-brain-connector-\(connector.id)-failing",
                title: "\(displayName) Connector Failing",
                body: error,
                destination: "ops"
            )
            UserDefaults.standard.set(marker, forKey: key)
        }
    }

    private func notifyDaemonFailureIfNeeded() {
        guard notificationsEnabled else {
            return
        }
        let message: String?
        switch daemon.status {
        case .failed(let value):
            message = value
        case .restarting(let value) where value.lowercased().contains("retry 3"):
            message = value
        default:
            message = nil
        }
        guard let message else {
            return
        }
        let day = Calendar.current.startOfDay(for: Date()).timeIntervalSince1970
        let marker = "\(Int(day)):\(message)"
        guard UserDefaults.standard.string(forKey: daemonFailureNotificationKey) != marker else {
            return
        }
        postNotification(
            identifier: "pkm-brain-daemon-needs-attention",
            title: "PKM Brain Daemon Needs Attention",
            body: message,
            destination: "ops"
        )
        UserDefaults.standard.set(marker, forKey: daemonFailureNotificationKey)
    }

    private func postNotification(
        identifier: String,
        title: String,
        body: String,
        destination: String
    ) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.userInfo = ["destination": destination]
        let request = UNNotificationRequest(
            identifier: identifier,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }
}
