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
        #expect(health.schema_version == 21)
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
        #expect(queue.state == "actionable")
        #expect(queue.sort == "retrieval")
        #expect(queue.queue_summary?.actionable_total == 2)
        #expect(queue.queue_summary?.blocked_total == 0)
        #expect(queue.items.first?.group == "conflicts")
        #expect(queue.items.first?.isApprovable == true)
        #expect(queue.items.first?.displayTitle == "Pkm Brain / Summary")
        #expect(queue.items.first?.orientation?.relation == "updates")
        #expect(queue.items.first?.orientation?.temporal_scope == "current_state")
        #expect(queue.items.first?.orientation?.currentness == "candidate reads as current state")
        #expect(queue.items.first?.candidate?.displayQuote == "Queue cards include candidate and existing evidence.")
        #expect(queue.items.first?.candidate?.source_date == "2026-07-08T18:00:00+00:00")
        #expect(queue.items.first?.candidate?.source_date_basis == "source_captured_at")
        #expect(queue.items.first?.counterparts?.first?.statement == "The old queue hid the existing fact.")
        #expect(queue.items.last?.memory?.content == "Review me from the queue.")
        #expect(queue.items.first?.popularity?.retrieval_count == 14)
    }

    @Test("alternative conflict payload decodes")
    func alternativeConflictPayloadDecodes() throws {
        let data = Data(
            #"{"id":"question_legacy","source_type":"question","kind":"conflict","group":"conflicts","comparison_mode":"alternatives","alternatives":[{"id":"fact_latest","statement":"Latest fact","confidence":0.9}]}"#.utf8
        )
        let item = try JSONDecoder().decode(QueueItem.self, from: data)

        #expect(item.isAlternativeComparison)
        #expect(item.alternatives?.first?.displayID == "fact_latest")
        #expect(item.primaryConfidence == 0.9)
    }

    @Test("API errors expose the server message without enum debug text")
    func apiErrorDescription() {
        let error = APIClientError.httpStatus(409, #"{"error":"This merge was already completed."}"#)

        #expect(error.errorDescription == "This merge was already completed.")
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

    @Test("wiki fixtures decode")
    func wikiFixturesDecode() throws {
        let pages: WikiPagesResponse = try decodeFixture("wiki_pages")
        let page: WikiPageDetail = try decodeFixture("wiki_page")

        #expect(pages.count == 1)
        #expect(pages.pages.first?.displayTitle == "PKM Brain")
        #expect(page.displayTitle == "PKM Brain")
        #expect(page.facts?.first?.statement == "PKM Brain has a native macOS app shell.")
        #expect(page.source_documents?.first?.source_id == "doc_1")
    }

    @Test("entity fixtures decode")
    func entityFixturesDecode() throws {
        let index: EntitiesResponse = try decodeFixture("entities")
        let detail: EntityDetail = try decodeFixture("entity_detail")

        #expect(index.count == 1)
        #expect(index.types.first?.entity_type == "project")
        #expect(index.sort == "retrieval")
        #expect(index.entities.first?.retrieval_count == 14)
        #expect(detail.entity.name == "PKM Brain")
        #expect(detail.facts_by_page.first?.facts.first?.statement == "PKM Brain has an entity detail view.")
        #expect(detail.co_mentions.first?.name == "Codex")
        #expect(detail.facts_by_page.first?.facts.first?.retrieval_count == 14)
    }

    @Test("curation settings fixture decodes")
    func curationSettingsFixtureDecodes() throws {
        let settings: CurationSettingsResponse = try decodeFixture("curation_settings")

        #expect(settings.strictness == "balanced")
        #expect(settings.minimum_auto_confidence == 0.8)
        #expect(settings.merge_aggressiveness == 0.7)
        #expect(settings.split_aggressiveness == 0.2)
        #expect(settings.applies_to == "future_actions_only")
        #expect(settings.topology_applies_to == "future_gardener_runs_only")
        #expect(settings.profiles.map(\.id) == ["strict", "balanced", "lenient"])
    }

    @Test("retrieve fixture decodes")
    func retrieveFixtureDecodes() throws {
        let result: RetrieveResult = try decodeFixture("retrieve")

        #expect(result.retrieval_verdict == "found")
        #expect(result.relevant_facts?.first?.statement == "The native app has queue review shortcuts.")
        #expect(result.relevant_wiki_pages?.first?.relative_path == "projects/pkm-brain.md")
        #expect(result.supporting_chunks?.first?.stableID == "chunk_1")
        #expect(result.active_memories?.first?.content == "Alex prefers numeric queue shortcuts.")
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
            runtime_id: "test-runtime",
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

    @MainActor
    @Test("supervisor replaces a same-version daemon with a mismatched runtime")
    func supervisorReplacesMismatchedRuntime() async throws {
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
        oldEnvironment["FAKE_BRAIN_VERSION"] = "0.1.0"
        oldEnvironment["PKM_BRAIN_RUNTIME_ID"] = "old-runtime"
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
            expectedDaemonVersion: "0.1.0",
            expectedRuntimeID: "new-runtime"
        )
        await supervisor.start(homeURL: home)
        let newPID = try #require(supervisor.handshake?.pid)

        #expect(newPID != oldProcess.processIdentifier)
        #expect(supervisor.handshake?.runtime_id == "new-runtime")
        try await waitUntil(timeoutSeconds: 4) {
            !oldProcess.isRunning
        }
        await supervisor.stop()
        try await waitUntil(timeoutSeconds: 4) {
            !isAlive(newPID)
        }
    }

    @Test("runtime retention pins current, rollback, and live process runtimes")
    func runtimeRetentionPinsProtectedRuntimes() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("PKMBrainRuntimeRetention-\(UUID().uuidString)", isDirectory: true)
        defer {
            try? FileManager.default.removeItem(at: root)
        }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let now = Date()
        for (index, id) in ["runtime-current", "runtime-rollback", "runtime-active", "runtime-stale"].enumerated() {
            let directory = root.appendingPathComponent(id, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try Data(repeating: UInt8(index), count: 32).write(to: directory.appendingPathComponent("payload"))
            try FileManager.default.setAttributes(
                [.modificationDate: now.addingTimeInterval(Double(-index))],
                ofItemAtPath: directory.path
            )
        }
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent("current"),
            withDestinationURL: root.appendingPathComponent("runtime-current", isDirectory: true)
        )
        let commands = [
            "\(root.path)/runtime-active/bin/python \(root.path)/current/bin/brain-mcp"
        ]
        let manager = RuntimeRetentionManager(runtimeRoot: root)

        let plan = try manager.plan(
            currentRuntimeID: "runtime-current",
            keepRollbacks: 1,
            processCommands: commands
        )

        #expect(plan.activeRuntimeIDs == ["runtime-active"])
        #expect(plan.rollbackRuntimeIDs == ["runtime-rollback"])
        #expect(plan.removableRuntimeIDs == ["runtime-stale"])
        #expect(plan.reclaimableBytes > 0)

        _ = try manager.prune(
            currentRuntimeID: "runtime-current",
            keepRollbacks: 1,
            processCommands: commands
        )
        #expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent("runtime-stale").path))
        #expect(FileManager.default.fileExists(atPath: root.appendingPathComponent("runtime-active").path))
        #expect(FileManager.default.fileExists(atPath: root.appendingPathComponent("runtime-rollback").path))
        #expect(FileManager.default.fileExists(atPath: root.appendingPathComponent("runtime-current").path))
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
