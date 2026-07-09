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

        var id: String { rawValue }

        var symbol: String {
            switch self {
            case .today: return "sun.max"
            case .queue: return "tray.full"
            case .wiki: return "doc.text"
            case .entities: return "person.2"
            case .ask: return "text.magnifyingglass"
            case .ops: return "slider.horizontal.3"
            }
        }
    }

    @Published var selectedDestination: Destination = .today
    @Published var digest: Digest?
    @Published var lastError: String?
    @Published var notificationsEnabled = true
    @Published var loginItemEnabled = false
    @Published var serveWeb = false
    @Published var homePath: String
    @Published var migrationPlan: MigrationPlan?
    @Published var migrationActionMessage: String?

    let provisioner: RuntimeProvisioner
    let daemon: DaemonSupervisor
    private var didStart = false
    private var monitorTask: Task<Void, Never>?
    private let queueBacklogThreshold = 100
    private let queueBacklogNotificationKey = "PKMBrain.queueBacklogNotificationAt"
    private let nightlyFailureNotificationKey = "PKMBrain.nightlyFailureNotification"
    private let daemonFailureNotificationKey = "PKMBrain.daemonFailureNotification"
    private let connectorFailureNotificationPrefix = "PKMBrain.connectorFailureNotification."

    init() {
        let defaultHome = ProcessInfo.processInfo.environment["PKM_BRAIN_HOME"]
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("brain").path
        homePath = defaultHome
        provisioner = RuntimeProvisioner()
        daemon = DaemonSupervisor(provisioner: provisioner)
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
        digest?.queue_counts.total ?? 0
    }

    func start() async {
        guard !didStart else {
            return
        }
        didStart = true
        await daemon.start(homeURL: homeURL, serveWeb: serveWeb)
        await requestNotificationAuthorizationIfNeeded()
        await refreshMigrationPlan()
        await refreshDigest()
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
            lastError = String(describing: error)
        }
    }

    func refreshDigest() async {
        guard let client = daemon.apiClient else {
            return
        }
        do {
            let latestDigest = try await client.digest()
            digest = latestDigest
            lastError = nil
            notifyQueueBacklogIfNeeded(count: latestDigest.queue_counts.total)
            notifyNightlyFailureIfNeeded(run: latestDigest.latest_run)
            await notifyConnectorFailuresIfNeeded(client: client)
        } catch {
            lastError = String(describing: error)
        }
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
            migrationActionMessage = "Shims installed"
            await refreshMigrationPlan()
        } catch {
            lastError = String(describing: error)
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
            lastError = String(describing: error)
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
                self?.notifyDaemonFailureIfNeeded()
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
        UNUserNotificationCenter.current().add(request) { _ in }
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
        UNUserNotificationCenter.current().add(request) { _ in }
    }
}
