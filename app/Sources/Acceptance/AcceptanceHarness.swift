import Darwin
import Foundation
import PKMBrainKit

@main
struct PKMBrainAcceptance {
    @MainActor
    static func main() async {
        do {
            let result = try await runAcceptance()
            let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        } catch {
            FileHandle.standardError.write(Data("M2 acceptance failed: \(error)\n".utf8))
            exit(1)
        }
    }

    @MainActor
    private static func runAcceptance() async throws -> [String: Any] {
        let args = CommandLine.arguments
        let appBundlePath = try option("--app-bundle", in: args)
        let rootPath = value("--work-root", in: args)
            ?? FileManager.default.temporaryDirectory
                .appendingPathComponent("pkm-brain-m2-acceptance-\(UUID().uuidString)", isDirectory: true)
                .path
        let root = URL(fileURLWithPath: rootPath, isDirectory: true)
        try? FileManager.default.removeItem(at: root)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)

        let sandboxHome = root.appendingPathComponent("home", isDirectory: true)
        let brainHome = sandboxHome.appendingPathComponent("brain", isDirectory: true)
        let appSupport = sandboxHome.appendingPathComponent("Library/Application Support/PKM Brain", isDirectory: true)
        try FileManager.default.createDirectory(at: sandboxHome, withIntermediateDirectories: true)
        setenv("HOME", sandboxHome.path, 1)

        let appBundleURL = URL(fileURLWithPath: appBundlePath)
        guard let appBundle = Bundle(url: appBundleURL) else {
            throw AcceptanceError.invalidBundle(appBundlePath)
        }
        let expectedVersion = appBundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let provisioner = RuntimeProvisioner(appSupportURL: appSupport)
        let brainExecutable = try await provisioner.ensureRuntime(bundle: appBundle)
        let supervisor = DaemonSupervisor(provisioner: provisioner, expectedDaemonVersion: expectedVersion)

        await supervisor.start(homeURL: brainHome)
        guard case .running = supervisor.status else {
            throw AcceptanceError.daemonDidNotStart(supervisor.status.label)
        }
        guard let client = supervisor.apiClient,
              let firstPID = supervisor.handshake?.pid
        else {
            throw AcceptanceError.missingHandshake
        }

        let typedDigest = try await client.digest()
        let groundTruthDigest: Digest = try await client.get("/api/digest")
        guard digestContentMatches(typedDigest, groundTruthDigest) else {
            throw AcceptanceError.digestMismatch
        }

        kill(pid_t(firstPID), SIGKILL)
        try await waitUntil(timeoutSeconds: 16) {
            guard let pid = supervisor.handshake?.pid else {
                return false
            }
            return pid != firstPID && isAlive(pid)
        }
        let restartedPID = try require(supervisor.handshake?.pid, AcceptanceError.missingHandshake)
        await supervisor.stop()
        try await waitUntil(timeoutSeconds: 8) {
            !isAlive(restartedPID)
        }

        return [
            "app_bundle": appBundlePath,
            "app_support": appSupport.path,
            "brain_executable": brainExecutable.path,
            "brain_home": brainHome.path,
            "digest_generated_at": typedDigest.generated_at,
            "digest_queue_total": typedDigest.queue_counts.total,
            "digest_queue_actionable": typedDigest.queue_summary?.actionable_total ?? typedDigest.queue_counts.total,
            "digest_queue_blocked": typedDigest.queue_summary?.blocked_total ?? 0,
            "first_pid": firstPID,
            "restarted_pid": restartedPID,
            "runtime_phase": provisioner.phase,
            "version": expectedVersion ?? ""
        ]
    }

    private static func option(_ name: String, in args: [String]) throws -> String {
        if let value = value(name, in: args) {
            return value
        }
        throw AcceptanceError.missingOption(name)
    }

    private static func value(_ name: String, in args: [String]) -> String? {
        guard let index = args.firstIndex(of: name), args.indices.contains(index + 1) else {
            return nil
        }
        return args[index + 1]
    }

    private static func require<T>(_ value: T?, _ error: AcceptanceError) throws -> T {
        guard let value else {
            throw error
        }
        return value
    }

    private static func digestContentMatches(_ lhs: Digest, _ rhs: Digest) -> Bool {
        lhs.since == rhs.since
            && lhs.pulse == rhs.pulse
            && lhs.latest_run == rhs.latest_run
            && lhs.facts_by_page == rhs.facts_by_page
            && lhs.reverts == rhs.reverts
            && lhs.demotions == rhs.demotions
            && lhs.eval_transitions == rhs.eval_transitions
            && queueSummaryContentMatches(lhs.queue_summary, rhs.queue_summary)
            && lhs.queue_counts == rhs.queue_counts
            && lhs.raw == rhs.raw
    }

    private static func queueSummaryContentMatches(_ lhs: QueueSummary?, _ rhs: QueueSummary?) -> Bool {
        lhs?.server_pid == rhs?.server_pid
            && lhs?.home == rhs?.home
            && lhs?.active_total == rhs?.active_total
            && lhs?.actionable_total == rhs?.actionable_total
            && lhs?.blocked_total == rhs?.blocked_total
            && lhs?.deferred_total == rhs?.deferred_total
            && lhs?.by_kind == rhs?.by_kind
            && lhs?.blocked_by_kind == rhs?.blocked_by_kind
            && lhs?.raw == rhs?.raw
    }

    private static func waitUntil(timeoutSeconds: TimeInterval, condition: @MainActor @escaping () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if await condition() {
                return
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw AcceptanceError.timeout
    }

    private static func isAlive(_ pid: Int) -> Bool {
        kill(pid_t(pid), 0) == 0 || errno == EPERM
    }
}

enum AcceptanceError: Error {
    case daemonDidNotStart(String)
    case digestMismatch
    case invalidBundle(String)
    case missingHandshake
    case missingOption(String)
    case timeout
}
