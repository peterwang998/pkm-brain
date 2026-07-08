import Darwin
import Foundation

public enum DaemonSupervisorError: Error, Equatable {
    case versionMismatch(expected: String, actual: String)
}

@MainActor
public final class DaemonSupervisor: ObservableObject {
    @Published public private(set) var status: DaemonStatus = .idle
    @Published public private(set) var handshake: DaemonHandshake?
    @Published public private(set) var scheduler: SchedulerState?

    public private(set) var apiClient: BrainAPIClient?
    private let provisioner: RuntimeProvisioner
    private let expectedDaemonVersion: String?
    private var process: Process?
    private var healthTask: Task<Void, Never>?
    private var launchConfiguration: LaunchConfiguration?
    private var restartFailures = 0

    public init(
        provisioner: RuntimeProvisioner,
        expectedDaemonVersion: String? = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
    ) {
        self.provisioner = provisioner
        self.expectedDaemonVersion = expectedDaemonVersion
    }

    public func start(homeURL: URL, serveWeb: Bool = false) async {
        status = .starting
        if await adoptExistingDaemon(homeURL: homeURL) {
            return
        }
        do {
            status = .provisioning("Provisioning runtime")
            let brain = try await provisioner.ensureRuntime()
            launchConfiguration = LaunchConfiguration(brain: brain, homeURL: homeURL, serveWeb: serveWeb)
            try launchDaemon(brain: brain, homeURL: homeURL, serveWeb: serveWeb)
            let handshake = try await waitForHandshake(homeURL: homeURL, timeoutSeconds: 10)
            try await adopt(handshake: handshake)
        } catch {
            status = .failed(String(describing: error))
        }
    }

    public func stop() async {
        healthTask?.cancel()
        healthTask = nil
        if let apiClient {
            _ = try? await apiClient.shutdown()
        }
        if let process, process.isRunning {
            if !(await waitUntilProcessStops(process, timeoutSeconds: 5)) {
                process.terminate()
                if !(await waitUntilProcessStops(process, timeoutSeconds: 5)) {
                    kill(process.processIdentifier, SIGKILL)
                }
            }
        } else if let pid = handshake?.pid {
            _ = await waitUntilPIDStops(pid, timeoutSeconds: 5)
        }
        process = nil
        apiClient = nil
        handshake = nil
        launchConfiguration = nil
        status = .idle
    }

    public func runCaptureNow() async {
        do {
            scheduler = try await apiClient?.runSchedulerJob("capture_tick")
        } catch {
            status = .failed(String(describing: error))
        }
    }

    public func pauseOneHour() async {
        do {
            scheduler = try await apiClient?.pauseScheduler(seconds: 3600)
        } catch {
            status = .failed(String(describing: error))
        }
    }

    public func resume() async {
        do {
            scheduler = try await apiClient?.resumeScheduler()
        } catch {
            status = .failed(String(describing: error))
        }
    }

    private func launchDaemon(brain: URL, homeURL: URL, serveWeb: Bool) throws {
        status = .starting
        let process = Process()
        process.executableURL = brain
        var arguments = ["daemon", "--home", homeURL.path]
        if serveWeb {
            arguments.append("--serve-web")
        }
        process.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["SENTENCE_TRANSFORMERS_HOME"] = provisioner.appSupportURL
            .appendingPathComponent("models", isDirectory: true).path
        process.environment = environment
        try process.run()
        self.process = process
    }

    private func adoptExistingDaemon(homeURL: URL) async -> Bool {
        guard let handshake = try? readHandshake(homeURL: homeURL) else {
            return false
        }
        do {
            try await adopt(handshake: handshake)
            return true
        } catch DaemonSupervisorError.versionMismatch {
            await replaceMismatchedDaemon(handshake: handshake, homeURL: homeURL)
            return false
        } catch {
            if !isProcessAlive(pid: handshake.pid) {
                removeHandshake(homeURL: homeURL)
            }
            return false
        }
    }

    private func replaceMismatchedDaemon(handshake: DaemonHandshake, homeURL: URL) async {
        let client = BrainAPIClient(baseURL: handshake.baseURL, token: handshake.token)
        _ = try? await client.shutdown()
        if !(await waitUntilPIDStops(handshake.pid, timeoutSeconds: 5)), isProcessAlive(pid: handshake.pid) {
            kill(pid_t(handshake.pid), SIGTERM)
            if !(await waitUntilPIDStops(handshake.pid, timeoutSeconds: 5)), isProcessAlive(pid: handshake.pid) {
                kill(pid_t(handshake.pid), SIGKILL)
            }
        }
        removeHandshake(homeURL: homeURL)
    }

    private func adopt(handshake: DaemonHandshake) async throws {
        let client = BrainAPIClient(baseURL: handshake.baseURL, token: handshake.token)
        let health = try await client.health()
        if let expectedDaemonVersion, health.version != expectedDaemonVersion {
            throw DaemonSupervisorError.versionMismatch(expected: expectedDaemonVersion, actual: health.version)
        }
        self.handshake = handshake
        self.apiClient = client
        self.scheduler = try? await client.scheduler()
        status = .running(health)
        restartFailures = 0
        startHealthPolling()
    }

    private func startHealthPolling() {
        healthTask?.cancel()
        healthTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                await self?.pollHealth()
            }
        }
    }

    private func pollHealth() async {
        guard let apiClient else {
            return
        }
        do {
            let health = try await apiClient.health()
            scheduler = try? await apiClient.scheduler()
            status = .running(health)
            restartFailures = 0
        } catch {
            restartFailures += 1
            status = .restarting("Daemon unavailable; retry \(restartFailures)")
            await restartDaemon()
        }
    }

    private func restartDaemon() async {
        guard let launchConfiguration else {
            status = .failed("Daemon unavailable and no launch configuration is available")
            return
        }
        if restartFailures > 3 {
            status = .failed("Daemon failed \(restartFailures) consecutive health checks")
            return
        }
        while restartFailures <= 3 && !Task.isCancelled {
            let delay = min(pow(2.0, Double(restartFailures - 1)), 60.0)
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else {
                return
            }
            await terminateTrackedProcess()
            if let handshake, !isProcessAlive(pid: handshake.pid) {
                removeHandshake(homeURL: launchConfiguration.homeURL)
            }
            do {
                try launchDaemon(
                    brain: launchConfiguration.brain,
                    homeURL: launchConfiguration.homeURL,
                    serveWeb: launchConfiguration.serveWeb
                )
                let handshake = try await waitForHandshake(homeURL: launchConfiguration.homeURL, timeoutSeconds: 10)
                try await adopt(handshake: handshake)
                return
            } catch {
                restartFailures += 1
                status = .restarting("Restart failed; retry \(restartFailures)")
            }
        }
        apiClient = nil
        handshake = nil
        status = .failed("Daemon failed \(restartFailures - 1) restart attempts")
    }

    private func terminateTrackedProcess() async {
        guard let process else {
            return
        }
        if process.isRunning {
            process.terminate()
            if !(await waitUntilProcessStops(process, timeoutSeconds: 1)) {
                kill(process.processIdentifier, SIGKILL)
            }
        }
        self.process = nil
    }

    private func waitUntilProcessStops(_ process: Process, timeoutSeconds: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if !process.isRunning {
                return true
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return !process.isRunning
    }

    private func waitUntilPIDStops(_ pid: Int, timeoutSeconds: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if !isProcessAlive(pid: pid) {
                return true
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return !isProcessAlive(pid: pid)
    }

    private func waitForHandshake(homeURL: URL, timeoutSeconds: TimeInterval) async throws -> DaemonHandshake {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if let handshake = try? readHandshake(homeURL: homeURL) {
                return handshake
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw CocoaError(.fileReadNoSuchFile)
    }

    private func readHandshake(homeURL: URL) throws -> DaemonHandshake {
        let url = homeURL.appendingPathComponent("config/local/daemon.json")
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(DaemonHandshake.self, from: data)
    }

    private func removeHandshake(homeURL: URL) {
        let url = homeURL.appendingPathComponent("config/local/daemon.json")
        try? FileManager.default.removeItem(at: url)
    }

    private func isProcessAlive(pid: Int) -> Bool {
        guard pid > 0 else {
            return false
        }
        return kill(pid_t(pid), 0) == 0 || errno == EPERM
    }
}

private struct LaunchConfiguration {
    let brain: URL
    let homeURL: URL
    let serveWeb: Bool
}
