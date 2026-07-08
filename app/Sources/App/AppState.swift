import Foundation
import PKMBrainKit
import SwiftUI

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
        await refreshMigrationPlan()
        await refreshDigest()
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
            digest = try await client.digest()
            lastError = nil
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
        await daemon.stop()
        didStart = false
    }
}
