import Foundation
import Darwin
import Testing
@testable import PKMBrainKit

@Suite("PKMBrainKit decoding")
struct PKMBrainKitTests {
    @Test("health fixture decodes")
    func healthFixtureDecodes() throws {
        let health: DaemonHealth = try decodeFixture("health")

        #expect(health.ok)
        #expect(health.port == 54321)
        #expect(health.schema_version == 20)
    }

    @Test("digest fixture decodes")
    func digestFixtureDecodes() throws {
        let digest: Digest = try decodeFixture("digest")

        #expect(digest.pulse.count == 2)
        #expect(digest.queue_counts.total == 8)
        #expect(digest.facts_by_page.first?.page_hint == "projects/pkm-brain.md")
        #expect(digest.latest_run?.status == "success")
        let evals = try #require(digest.pulse.first { $0.key == "evals" })
        #expect(digest.detailText(for: evals)?.contains("22 sampled findings") == true)
        let nightly = try #require(digest.pulse.first { $0.key == "nightly" })
        #expect(digest.detailText(for: nightly)?.contains("Scheduler check status is shown in Ops") == true)
    }

    @Test("scheduler fixture decodes")
    func schedulerFixtureDecodes() throws {
        let scheduler: SchedulerState = try decodeFixture("scheduler")

        #expect(scheduler.jobs.map(\.id).contains("capture_tick"))
        #expect(scheduler.jobs.first?.cadence_s == 600)
        let nightly = try #require(scheduler.jobs.first { $0.id == "nightly" })
        #expect(nightly.displayStatus == "skipped")
        #expect(nightly.statusDetail == "last successful nightly run is less than 20 hours old")
    }

    @Test("queue fixture decodes")
    func queueFixtureDecodes() throws {
        let queue: QueuePage = try decodeFixture("queue")

        #expect(queue.counts.total == 2)
        #expect(queue.items.first?.group == "conflicts")
        #expect(queue.items.first?.candidate?.displayQuote == "Queue cards include candidate and existing evidence.")
        #expect(queue.items.first?.counterparts?.first?.statement == "The old queue hid the existing fact.")
        #expect(queue.items.last?.memory?.content == "Review me from the queue.")
    }

    @Test("connectors fixture decodes")
    func connectorsFixtureDecodes() throws {
        let connectors: ConnectorsResponse = try decodeFixture("connectors")

        #expect(connectors.count == 1)
        #expect(connectors.connectors.first?.manifest.id == "codex")
        #expect(connectors.connectors.first?.state.settings["sessions_dir"]?.stringValue == "~/.codex/sessions")
        #expect(connectors.connectors.first?.health.consecutive_failures == 3)
        #expect(connectors.connectors.first?.health.last_error == "sessions directory unavailable")
    }

    @Test("migration fixture decodes")
    func migrationFixtureDecodes() throws {
        let plan: MigrationPlan = try decodeFixture("migration")

        #expect(plan.needsMigration)
        #expect(plan.detected_launch_agents.map(\.label).contains("com.pkm-brain.capture-secondary"))
        #expect(plan.steps.map(\.id).contains("cli_shims"))
    }

    @Test("handshake builds loopback base URL")
    func handshakeBaseURL() throws {
        let handshake = DaemonHandshake(
            pid: 123,
            port: 9876,
            token: "token",
            version: "0.1.0",
            home: "/tmp/brain",
            started_at: "2026-07-08T08:00:00+00:00",
            host: nil
        )

        #expect(handshake.baseURL.absoluteString == "http://127.0.0.1:9876")
    }

    @MainActor
    @Test("supervisor restarts a killed daemon and shuts it down")
    func supervisorRestartsKilledDaemon() async throws {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("PKMBrainKitTests-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: tempRoot)
        }
        let appSupport = tempRoot.appendingPathComponent("AppSupport", isDirectory: true)
        let home = tempRoot.appendingPathComponent("Brain", isDirectory: true)
        let brain = appSupport.appendingPathComponent("runtime/current/bin/brain")
        let fakeDaemon = try #require(Bundle.module.url(forResource: "fake_brain_daemon", withExtension: "py"))
        try installFakeBrain(at: brain, fakeDaemon: fakeDaemon)

        let provisioner = RuntimeProvisioner(appSupportURL: appSupport)
        let supervisor = DaemonSupervisor(provisioner: provisioner, expectedDaemonVersion: "0.1.0")
        await supervisor.start(homeURL: home)
        let firstPID = try #require(supervisor.handshake?.pid)
        #expect(isAlive(firstPID))

        kill(pid_t(firstPID), SIGKILL)
        try await waitUntil(timeoutSeconds: 12) {
            guard let pid = supervisor.handshake?.pid else {
                return false
            }
            return pid != firstPID && isAlive(pid)
        }
        let secondPID = try #require(supervisor.handshake?.pid)
        await supervisor.stop()
        try await waitUntil(timeoutSeconds: 4) {
            !isAlive(secondPID)
        }
    }

    @MainActor
    @Test("supervisor replaces an adopted daemon with a mismatched version")
    func supervisorReplacesMismatchedDaemon() async throws {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("PKMBrainKitTests-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: tempRoot)
        }
        let appSupport = tempRoot.appendingPathComponent("AppSupport", isDirectory: true)
        let home = tempRoot.appendingPathComponent("Brain", isDirectory: true)
        let fakeDaemon = try #require(Bundle.module.url(forResource: "fake_brain_daemon", withExtension: "py"))

        let oldProcess = Process()
        oldProcess.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        oldProcess.arguments = [fakeDaemon.path, "daemon", "--home", home.path]
        var oldEnvironment = ProcessInfo.processInfo.environment
        oldEnvironment["FAKE_BRAIN_VERSION"] = "0.0.1"
        oldProcess.environment = oldEnvironment
        try oldProcess.run()
        defer {
            if oldProcess.isRunning {
                oldProcess.terminate()
            }
        }
        try await waitUntil(timeoutSeconds: 4) {
            let handshakeURL = home.appendingPathComponent("config/local/daemon.json")
            guard let data = try? Data(contentsOf: handshakeURL),
                  let handshake = try? JSONDecoder().decode(DaemonHandshake.self, from: data)
            else {
                return false
            }
            return handshake.pid == oldProcess.processIdentifier
        }

        let brain = appSupport.appendingPathComponent("runtime/current/bin/brain")
        try installFakeBrain(at: brain, fakeDaemon: fakeDaemon)
        let supervisor = DaemonSupervisor(
            provisioner: RuntimeProvisioner(appSupportURL: appSupport),
            expectedDaemonVersion: "0.1.0"
        )
        await supervisor.start(homeURL: home)
        let newPID = try #require(supervisor.handshake?.pid)

        #expect(newPID != oldProcess.processIdentifier)
        try await waitUntil(timeoutSeconds: 4) {
            !oldProcess.isRunning
        }
        await supervisor.stop()
        try await waitUntil(timeoutSeconds: 4) {
            !isAlive(newPID)
        }
    }

    private func decodeFixture<T: Decodable>(_ name: String) throws -> T {
        let url = try #require(Bundle.module.url(forResource: name, withExtension: "json"))
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func isAlive(_ pid: Int) -> Bool {
        kill(pid_t(pid), 0) == 0 || errno == EPERM
    }

    private func installFakeBrain(at brain: URL, fakeDaemon: URL) throws {
        try FileManager.default.createDirectory(
            at: brain.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let launcher = """
        #!/bin/zsh
        exec /usr/bin/python3 "\(fakeDaemon.path)" "$@"
        """
        try launcher.write(to: brain, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: brain.path)
    }

    private func waitUntil(timeoutSeconds: TimeInterval, condition: @MainActor @escaping () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if await condition() {
                return
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        Issue.record("Timed out waiting for condition")
    }
}
