import Foundation
import Darwin
import Testing
@testable import PKMBrainKit

@Suite("PKMBrainKit decoding")
struct PKMBrainKitTests {
    @Test("Connector authorization explains each read-only boundary")
    func connectorAuthorizationAccessSummary() {
        let gmail = ConnectorAuthManifest(
            kind: "oauth2",
            provider: "gmail",
            phase: "read_only",
            requested_scopes: ["https://www.googleapis.com/auth/gmail.readonly"],
            client_secret_required: false,
            redirect_uri: "http://127.0.0.1/oauth/callback/gmail",
            setup_url: "https://console.cloud.google.com/apis/credentials"
        )
        let calendar = ConnectorAuthManifest(
            kind: "oauth2",
            provider: "calendar",
            phase: "read_only",
            requested_scopes: ["https://www.googleapis.com/auth/calendar.events.owned.readonly"],
            client_secret_required: false,
            redirect_uri: "http://127.0.0.1/oauth/callback/calendar",
            setup_url: "https://console.cloud.google.com/apis/credentials"
        )
        let identity = ConnectorAuthManifest(
            kind: "oauth2",
            provider: "slack",
            phase: "identity_only",
            requested_scopes: ["openid"],
            client_secret_required: true,
            redirect_uri: "http://127.0.0.1/oauth/callback/slack",
            setup_url: "https://api.slack.com/apps"
        )

        #expect(gmail.accessSummary(for: "gmail") == "Read Gmail messages and threads only")
        #expect(
            calendar.accessSummary(for: "calendar")
                == "Read events on your owned primary calendar only"
        )
        #expect(identity.accessSummary(for: "slack") == "Identity only")
    }

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

    @Test("Today briefing fixture decodes every presentation section")
    func todayFixtureDecodes() throws {
        let briefing: TodayBriefing = try decodeFixture("today")

        #expect(briefing.schema_version == 1)
        #expect(briefing.status == "partial")
        #expect(briefing.isAvailable)
        #expect(briefing.hasCoverageWarning)
        #expect(briefing.visibleFocus.count == 1)
        #expect(briefing.visibleFocus.first?.handledLabel == "Needs action")
        #expect(briefing.visibleFocus.first?.feedback_actions.contains("confirm") == true)
        #expect(briefing.visibleFocus.first?.evidence.first?.brain_route?.contains("/wiki") == true)
        #expect(briefing.urgent_overflow.count == 2)
        #expect(briefing.calendar.next.first?.title == "Project review")
        #expect(briefing.uncertain.first?.reason_codes == ["authoritative_source_unavailable"])
        #expect(briefing.ignored_suppressed.first?.handled_verdict == "fulfilled")
        #expect(briefing.ignoredSuppressedTotal == 1)
        #expect(briefing.feedback.allows("report_missing"))
        #expect(briefing.feedback.allows("confirm"))
    }

    @Test("Today shadow-run fixture decodes a partial usable result")
    func todayShadowRunFixtureDecodes() throws {
        let result: TodayShadowRunStatus = try decodeFixture("today_shadow_run")

        #expect(result.run_id == "opsshadow_example")
        #expect(result.status == "partial")
        #expect(result.succeeded)
        #expect(result.isTerminal)
        #expect(!result.isInProgress)
        #expect(result.displayKind == .partial)
        #expect(result.shouldRefreshBriefing)
        #expect(result.counts?["surfaced"]?.intValue == 3)
        #expect(result.message == "Shadow run finished with partial coverage.")
    }

    @Test("Today shadow-run display state refreshes retained results after every terminal outcome")
    func todayShadowRunDisplayState() throws {
        let expected: [(String, TodayShadowRunDisplayKind, Bool)] = [
            ("accepted", .progress, false),
            ("running", .progress, false),
            ("complete", .complete, true),
            ("partial", .partial, true),
            ("failed", .failed, true),
            ("cancelled", .failed, true),
        ]

        for (status, displayKind, shouldRefresh) in expected {
            let data = Data(
                #"{"schema_version":1,"status":"\#(status)","message":"Shadow status","run_id":"opsshadow_state","coverage":{},"usage":{},"counts":{}}"#.utf8
            )
            let result = try JSONDecoder().decode(TodayShadowRunStatus.self, from: data)

            #expect(result.displayKind == displayKind)
            #expect(result.shouldRefreshBriefing == shouldRefresh)
        }
    }

    @Test("Today shadow-run client posts the manual source and timezone selection")
    func todayShadowRunRequest() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ShadowRunURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            ShadowRunURLProtocol.handler = nil
            session.invalidateAndCancel()
        }
        ShadowRunURLProtocol.handler = { request in
            #expect(request.url?.path == "/api/v1/today/run")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
            if request.httpMethod == "POST" {
                #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
                let body = try requestBody(request)
                let object = try #require(
                    JSONSerialization.jsonObject(with: body) as? [String: Any]
                )
                #expect(object["timezone_name"] as? String == TimeZone.current.identifier)
                #expect(object["sources"] as? [String] == ["calendar", "gmail"])
                #expect(Set(object.keys) == Set(["timezone_name", "sources"]))
            } else {
                #expect(request.httpMethod == "GET")
            }

            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"]
                )
            )
            let status = request.httpMethod == "POST" ? "accepted" : "complete"
            let payload = Data(
                #"{"schema_version":1,"status":"\#(status)","message":"Shadow status: \#(status).","run_id":"opsshadow_request","coverage":{},"usage":{},"counts":{}}"#.utf8
            )
            return (response, payload)
        }

        let client = BrainAPIClient(
            baseURL: URL(string: "http://127.0.0.1:54321")!,
            token: "test-token",
            session: session
        )
        let accepted = try await client.runTodayShadow()
        let result = try await client.todayShadowRunStatus()

        #expect(accepted.isInProgress)
        #expect(result.run_id == "opsshadow_request")
        #expect(result.succeeded)
    }

    @Test("Today retained evidence only loads the local evidence route")
    func todayRetainedEvidenceRequest() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RetainedEvidenceURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            RetainedEvidenceURLProtocol.handler = nil
            session.invalidateAndCancel()
        }
        RetainedEvidenceURLProtocol.handler = { request in
            #expect(request.url?.path == "/api/ops/evidence")
            #expect(request.url?.query?.contains("source_type=gmail") == true)
            #expect(request.url?.query?.contains("account_key=gmail.personal") == true)
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
            #expect(request.httpMethod == "GET")
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"]
                )
            )
            let payload = Data(
                #"{"schema_version":1,"source_type":"gmail","account_key":"gmail.personal","source_ref":"gmail.personal:thread-1","source_revision":"history-42","retention_days":30,"evidence":{"thread_id":"thread-1","subject":"Send the board deck","messages":[{"body":"Can you send it today?"}]}}"#.utf8
            )
            return (response, payload)
        }

        let client = BrainAPIClient(
            baseURL: URL(string: "http://127.0.0.1:54321")!,
            token: "test-token",
            session: session
        )
        let route = "/api/ops/evidence?source_type=gmail&account_key=gmail.personal&source_ref=gmail.personal%3Athread-1&source_revision=history-42"
        let evidence = try await client.todayRetainedEvidence(at: route)

        #expect(evidence.sourceLabel == "Gmail")
        #expect(evidence.displayTitle == "Send the board deck")
        #expect(evidence.source_revision == "history-42")
        #expect(evidence.evidence.objectValue?["messages"]?.arrayValue?.count == 1)
        #expect(BrainAPIClient.canLoadTodayEvidence(at: route))
    }

    @Test("Today retained evidence rejects remote, malformed, and expanded routes")
    func todayRetainedEvidenceRejectsUnsafeRoutes() async {
        let client = BrainAPIClient(
            baseURL: URL(string: "http://127.0.0.1:54321")!,
            token: "test-token"
        )
        let unsafeRoutes = [
            "https://example.com/api/ops/evidence?source_type=gmail&account_key=a&source_ref=b",
            "/api/ops/evidence?source_type=gmail&account_key=a&source_ref=b&redirect=https://example.com",
            "/api/ops/evidence?source_type=gmail&account_key=a&source_ref=b#fragment",
            "/api/ops/evidence?source_type=gmail&account_key=a&source_ref=b&source_ref=c",
            "/api/ops/evidence?source_type=drive&account_key=a&source_ref=b",
            "/api/ops/storage?source_type=gmail&account_key=a&source_ref=b",
        ]

        for route in unsafeRoutes {
            #expect(!BrainAPIClient.canLoadTodayEvidence(at: route))
            do {
                let _: TodayRetainedEvidence = try await client.todayRetainedEvidence(at: route)
                Issue.record("Unsafe evidence route was accepted: \(route)")
            } catch let error as APIClientError {
                #expect(error == .unsafeTodayEvidenceRoute)
            } catch {
                Issue.record("Unexpected error for unsafe route: \(error)")
            }
        }
    }

    @Test("Meeting preparation fixture decodes evidence, coverage, and non-factual suggestions")
    func meetingPreparationFixtureDecodes() throws {
        let packet: TodayMeetingPacket = try decodeFixture("meeting_packet")

        #expect(packet.schema_version == 1)
        #expect(packet.title == "Project review")
        #expect(packet.event_claims.first?.claim_type == "calendar_observation")
        #expect(packet.event_claims.first?.evidence_refs.count == 1)
        #expect(packet.knowledge_claims.first?.fact_id == "fact-17")
        #expect(packet.knowledge_claims.first?.confidence == 0.91)
        #expect(packet.knowledge_claims.first?.evidence_refs.count == 1)
        #expect(packet.wiki_context.first?.source_ids == ["document-17", "document-22"])
        #expect(packet.coverage["brain_retrieval"]?.objectValue?["stale"]?.boolValue == true)
        #expect(packet.retrieval_reasons.count == 1)
        #expect(packet.suggestions.first?.is_factual_claim == false)

        let briefing: TodayBriefing = try decodeFixture("today")
        #expect(briefing.calendar.next.first?.supportsMeetingPreparation == true)
        #expect(briefing.urgent_overflow.items.first?.supportsMeetingPreparation == false)
    }

    @Test("Meeting preparation GET percent-encodes one bounded item ID")
    func meetingPreparationRequest() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MeetingPacketURLProtocol.self]
        let session = URLSession(configuration: configuration)
        defer {
            MeetingPacketURLProtocol.handler = nil
            session.invalidateAndCancel()
        }
        let itemID = "event/project review?#%"
        MeetingPacketURLProtocol.handler = { request in
            #expect(request.httpMethod == "GET")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
            #expect(
                request.url?.absoluteString.contains(
                    "/api/ops/items/event%2Fproject%20review%3F%23%25/meeting-packet"
                ) == true
            )
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"]
                )
            )
            let payload = try JSONSerialization.data(withJSONObject: [
                "schema_version": 1,
                "item_id": itemID,
                "generated_at": "2026-07-14T14:05:00+00:00",
                "title": "Project review",
                "event_claims": [],
                "knowledge_claims": [],
                "wiki_context": [],
                "suggestions": [],
                "coverage": ["calendar": "complete"],
                "retrieval_reasons": [],
            ])
            return (response, payload)
        }

        let client = BrainAPIClient(
            baseURL: URL(string: "http://127.0.0.1:54321")!,
            token: "test-token",
            session: session
        )
        let packet = try await client.todayMeetingPacket(itemID: itemID)

        #expect(packet.item_id == itemID)
        #expect(packet.title == "Project review")
        #expect(BrainAPIClient.canPrepareMeeting(itemID: itemID))
    }

    @Test("Meeting preparation rejects empty, control-character, traversal, and oversized IDs")
    func meetingPreparationRejectsUnsafeIDs() async {
        let client = BrainAPIClient(
            baseURL: URL(string: "http://127.0.0.1:54321")!,
            token: "test-token"
        )
        let invalidIDs = [
            "",
            "   ",
            ".",
            "..",
            " event-42 ",
            "event\nid",
            String(repeating: "x", count: 257),
        ]

        for itemID in invalidIDs {
            #expect(!BrainAPIClient.canPrepareMeeting(itemID: itemID))
            do {
                let _: TodayMeetingPacket = try await client.todayMeetingPacket(itemID: itemID)
                Issue.record("Unsafe meeting item ID was accepted: \(itemID)")
            } catch let error as APIClientError {
                #expect(error == .invalidMeetingItemID)
            } catch {
                Issue.record("Unexpected error for unsafe item ID: \(error)")
            }
        }
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

        #expect(connectors.count == 3)
        #expect(connectors.connectors.first?.manifest.id == "codex")
        #expect(connectors.connectors.first?.state.settings["sessions_dir"]?.stringValue == "~/.codex/sessions")
        #expect(connectors.connectors.first?.health.consecutive_failures == 3)
        #expect(connectors.connectors.first?.health.last_error == "sessions directory unavailable")
        let calendar = connectors.connectors.first(where: { $0.manifest.id == "calendar" })
        #expect(calendar?.manifest.lifecycleStatus == "auth_only")
        #expect(calendar?.manifest.canCapture == false)
        #expect(calendar?.manifest.auth?.phase == "read_only")
        #expect(calendar?.manifest.auth?.requested_scopes == [
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/calendar.events.owned.readonly",
        ])
        #expect(calendar?.state.auth?.provider == "calendar")
        #expect(calendar?.state.auth?.status == "connected")
        #expect(calendar?.state.auth?.granted_scopes == calendar?.state.auth?.requested_scopes)
        #expect(calendar?.state.auth?.redirect_uri.hasSuffix("/oauth/callback/calendar") == true)
        let gmail = connectors.connectors.first(where: { $0.manifest.id == "gmail" })
        #expect(gmail?.manifest.lifecycleStatus == "auth_only")
        #expect(gmail?.manifest.canCapture == false)
        #expect(gmail?.manifest.auth?.phase == "read_only")
        #expect(gmail?.manifest.auth?.requested_scopes == [
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/gmail.readonly",
        ])
        #expect(gmail?.state.auth?.provider == "gmail")
        #expect(gmail?.state.auth?.status == "ready")
        #expect(gmail?.state.auth?.client_secret_configured == true)
        #expect(gmail?.state.auth?.redirect_uri.hasSuffix("/oauth/callback/gmail") == true)
        #expect(calendar?.state.auth?.redirect_uri != gmail?.state.auth?.redirect_uri)
    }

    @Test("wiki fixtures decode")
    func wikiFixturesDecode() throws {
        let pages: WikiPagesResponse = try decodeFixture("wiki_pages")
        let page: WikiPageDetail = try decodeFixture("wiki_page")

        #expect(pages.count == 3)
        #expect(pages.pages.first?.displayTitle == "PKM Brain")
        #expect(page.displayTitle == "PKM Brain")
        #expect(page.facts?.first?.statement == "PKM Brain has a native macOS app shell.")
        #expect(page.source_documents?.first?.source_id == "doc_1")
        let routeMatches = RoutePathMatcher.suggestions(
            in: pages.pages,
            matching: "severance"
        )
        #expect(routeMatches.map(\.relative_path) == [
            "career/databricks-severance-discussions.md"
        ])
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
        #expect(settings.topology_review_threshold == 32)
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

private final class ShadowRunURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: APIClientError.invalidResponse)
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private final class RetainedEvidenceURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: APIClientError.invalidResponse)
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private final class MeetingPacketURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: APIClientError.invalidResponse)
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private func requestBody(_ request: URLRequest) throws -> Data {
    if let body = request.httpBody {
        return body
    }
    let stream = try #require(request.httpBodyStream)
    stream.open()
    defer { stream.close() }

    var body = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
        let count = buffer.withUnsafeMutableBufferPointer { pointer in
            stream.read(pointer.baseAddress!, maxLength: pointer.count)
        }
        if count < 0 {
            throw stream.streamError ?? APIClientError.invalidResponse
        }
        if count == 0 {
            return body
        }
        body.append(contentsOf: buffer.prefix(count))
    }
}
