import Foundation

public struct DaemonHandshake: Codable, Equatable, Sendable {
    public let pid: Int
    public let port: Int
    public let token: String
    public let version: String
    public let runtime_id: String?
    public let home: String
    public let started_at: String
    public let host: String?

    public var baseURL: URL {
        URL(string: "http://\(host ?? "127.0.0.1"):\(port)")!
    }
}

public struct DaemonHealth: Codable, Equatable, Sendable {
    public let ok: Bool
    public let version: String
    public let runtime_id: String?
    public let home: String
    public let pid: Int
    public let host: String
    public let port: Int
    public let started_at: String?
    public let schema_version: Int
}

public struct SchedulerState: Codable, Equatable, Sendable {
    public let paused_until: String?
    public let jobs: [SchedulerJobState]
}

public struct SchedulerJobState: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let enabled: Bool
    public let cadence_s: Int
    public let last_run_at: String?
    public let last_status: String?
    public let last_result: JSONValue?
    public let last_error: String?
    public let next_due_at: String?
    public let running: Bool
    public let queued: Bool?

    public var displayStatus: String {
        if running {
            return "running"
        }
        if queued == true {
            return "queued"
        }
        return last_status ?? "pending"
    }

    public var statusDetail: String? {
        if let last_error, !last_error.isEmpty {
            return last_error
        }
        if let reason = last_result?.objectValue?["reason"]?.stringValue, !reason.isEmpty {
            return reason
        }
        if displayStatus == "skipped" {
            return "Skipped without a recorded reason."
        }
        return nil
    }
}

public struct PulseChip: Codable, Equatable, Identifiable, Sendable {
    public let key: String
    public let label: String
    public let state: String
    public let value: String?
    public let timestamp: String?
    public let href: String?

    public var id: String { key }
}

public struct QueueCounts: Codable, Equatable, Sendable {
    public let total: Int
    public let by_kind: [String: Int]
    public let raw: [String: Int]
}

public struct QueueSummary: Codable, Equatable, Sendable {
    public let as_of: String
    public let server_pid: Int?
    public let home: String?
    public let active_total: Int
    public let actionable_total: Int
    public let blocked_total: Int
    public let deferred_total: Int
    public let active_limit: Int?
    public let daily_admission_limit: Int?
    public let admitted_today: Int?
    public let by_kind: [String: Int]
    public let blocked_by_kind: [String: Int]?
    public let deferred_by_kind: [String: Int]?
    public let raw: [String: Int]
}

public struct FactPageDelta: Codable, Equatable, Identifiable, Sendable {
    public let page_hint: String
    public let count: Int
    public let latest_at: String?

    public var id: String { page_hint }
}

public struct DigestRun: Codable, Equatable, Sendable {
    public let id: String?
    public let job_name: String?
    public let status: String?
    public let started_at: String?
    public let finished_at: String?
    public let error: String?
    public let summary: [String: JSONValue]?
}

public struct Digest: Codable, Equatable, Sendable {
    public let generated_at: String
    public let since: String?
    public let pulse: [PulseChip]
    public let latest_run: DigestRun?
    public let facts_by_page: [FactPageDelta]
    public let reverts: [JSONValue]
    public let demotions: [JSONValue]
    public let eval_transitions: [JSONValue]
    public let queue_summary: QueueSummary?
    public let queue_counts: QueueCounts
    public let raw: [String: JSONValue]?

    public func detailText(for chip: PulseChip) -> String? {
        switch chip.key.lowercased() {
        case "nightly":
            var lines: [String] = []
            if let latest_run {
                let status = latest_run.status ?? chip.value ?? "unknown"
                let name = latest_run.job_name ?? "automation run"
                if let finished = latest_run.finished_at ?? latest_run.started_at {
                    lines.append("Latest \(name): \(status) at \(finished).")
                } else {
                    lines.append("Latest \(name): \(status).")
                }
                if let error = latest_run.error, !error.isEmpty {
                    lines.append(error)
                }
            } else if let timestamp = chip.timestamp {
                lines.append("Latest status at \(timestamp).")
            }
            lines.append("Scheduler check status is shown in Ops.")
            return joinedDetail(lines)
        case "evals":
            guard let audit = raw?["audit"]?.objectValue else {
                return nil
            }
            let counts = audit["counts"]?.objectValue ?? [:]
            var parts: [String] = []
            if let bad = counts["sampled_bad"]?.intValue {
                parts.append("\(bad) sampled findings")
            }
            if let ok = counts["sampled_ok"]?.intValue {
                parts.append("\(ok) sampled ok")
            }
            if let unaudited = counts["unaudited"]?.intValue {
                parts.append("\(unaudited) unaudited")
            }
            if let note = audit["note"]?.stringValue, !note.isEmpty {
                parts.append(note)
            }
            return joinedDetail(parts)
        case "index":
            guard let index = raw?["index"]?.objectValue else {
                return nil
            }
            if let documents = index["documents"]?.intValue,
               let chunks = index["chunks"]?.intValue {
                return "\(documents) documents, \(chunks) chunks indexed."
            }
            return index["embedding_provider"]?.stringValue
        case "agents":
            guard let jobs = raw?["jobs"]?.objectValue?["jobs"]?.arrayValue else {
                return nil
            }
            let loaded = jobs.filter { $0.objectValue?["loaded"]?.boolValue == true }.count
            return "\(jobs.count) legacy scheduler checks, \(loaded) loaded."
        case "sync":
            guard let sync = raw?["sync"]?.objectValue else {
                return nil
            }
            let role = sync["role"]?.stringValue ?? chip.value ?? "configured"
            let peers = sync["peers"]?.arrayValue?.count ?? 0
            let warnings = sync["warnings"]?.arrayValue?.count ?? 0
            return "\(role) role, \(peers) peers, \(warnings) warnings."
        default:
            return chip.timestamp.map { "Latest status at \($0)." }
        }
    }
}

public struct QueuePage: Codable, Equatable, Sendable {
    public let kind: String
    public let state: String?
    public let sort: String?
    public let queue_summary: QueueSummary?
    public let counts: QueueCounts
    public let total: Int
    public let cursor: Int
    public let next_cursor: Int?
    public let items: [QueueItem]

    public init(
        kind: String,
        state: String? = nil,
        sort: String? = nil,
        queue_summary: QueueSummary? = nil,
        counts: QueueCounts,
        total: Int,
        cursor: Int,
        next_cursor: Int?,
        items: [QueueItem]
    ) {
        self.kind = kind
        self.state = state
        self.sort = sort
        self.queue_summary = queue_summary
        self.counts = counts
        self.total = total
        self.cursor = cursor
        self.next_cursor = next_cursor
        self.items = items
    }
}

public struct QueueItem: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let source_type: String
    public let kind: String
    public let group: String
    public let title: String?
    public let summary: String?
    public let created_at: String?
    public let status: String?
    public let risk_tier: String?
    public let page_hint: String?
    public let entity_key: String?
    public let action_id: String?
    public let candidate: QueueFact?
    public let counterparts: [QueueFact]?
    public let comparison_mode: String?
    public let alternatives: [QueueFact]?
    public let orientation: QueueOrientation?
    public let route_candidates: [QueueRouteCandidate]?
    public let memory: QueueMemory?
    public let anomaly: QueueAnomaly?
    public let audit: QueueAuditFinding?
    public let topology: QueueTopology?
    public let action: [String: JSONValue]?
    public let proposal: JSONValue?
    public let question: [String: JSONValue]?
    public let options: [JSONValue]?
    public let raw: JSONValue?
    public let popularity: QueuePopularity?
    public let approvable: Bool?
    public let blocking_code: String?
    public let blocking_reason: String?

    public var displayTitle: String {
        orientation?.title ?? title ?? summary ?? id
    }

    public var primaryConfidence: Double? {
        candidate?.displayConfidence
            ?? alternatives?.first?.displayConfidence
            ?? memory?.confidence
            ?? orientation?.relation_confidence
    }

    public var isApprovable: Bool {
        approvable ?? true
    }

    public var isAlternativeComparison: Bool {
        comparison_mode == "alternatives"
    }
}

public struct QueueAnomaly: Codable, Equatable, Sendable {
    public let document_id: String?
    public let document_title: String?
    public let reviewed_count: Int
    public let blocked_count: Int
    public let block_rate: Double?
}

public struct QueueAuditFinding: Codable, Equatable, Sendable {
    public let status: String?
    public let rationale: String
    public let provider: String?
    public let model: String?
    public let audited_at: String?
    public let action_type: String?
    public let action_status: String?
    public let affected_fact_count: Int?
    public let affected_page_count: Int?
    public let affected_contract_count: Int?
    public let affected_facts: [QueueFact]?
    public let revertible: Bool?
    public let revert_mode: String?
    public let reviewability_reason: String?
}

public struct QueuePopularity: Codable, Equatable, Sendable {
    public let retrieval_count: Int
    public let last_retrieved_at: String?
    public let fact_retrieval_count: Int?
    public let entity_retrieval_count: Int?
}

public struct QueueTopology: Codable, Equatable, Sendable {
    public let entity_ids: [String]?
    public let entity_labels: [String]?
    public let entity_statuses: [String: String]?
    public let page_hints: [String]?
    public let page_statuses: [String: String]?
    public let target_label: String?
    public let merge_destination_label: String?
    public let merge_source_labels: [String]?
    public let split_preview: QueueSplitPreview?
}

public struct QueueSplitPreview: Codable, Equatable, Sendable {
    public let source_page_hint: String
    public let source_page_retained: Bool
    public let resulting_page_count: Int
    public let movable_fact_count: Int
    public let children: [QueueSplitChild]
    public let approvable: Bool
}

public struct QueueSplitChild: Codable, Equatable, Identifiable, Sendable {
    public let section: String
    public let page_hint: String
    public let fact_count: Int
    public let representative_facts: [QueueSplitFact]

    public var id: String { page_hint }
}

public struct QueueSplitFact: Codable, Equatable, Sendable {
    public let id: String?
    public let statement: String?
    public let evidence_quote: String?
}

public struct QueueOrientation: Codable, Equatable, Sendable {
    public let title: String?
    public let entity_label: String?
    public let entity_key: String?
    public let page_hint: String?
    public let section_hint: String?
    public let candidate_observed_at: String?
    public let existing_observed_at: String?
    public let temporal_scope: String?
    public let existing_temporal_scope: String?
    public let currentness: String?
    public let relation: String?
    public let relation_confidence: Double?
    public let relation_rationale: String?
}

public struct QueueFact: Codable, Equatable, Sendable {
    public let id: String?
    public let fact_id: String?
    public let statement: String?
    public let evidence_quote: String?
    public let quote: String?
    public let truth_confidence: Double?
    public let confidence: Double?
    public let page_hint: String?
    public let section_hint: String?
    public let entity_key: String?
    public let observed_at: String?
    public let source_date: String?
    public let source_date_basis: String?
    public let source_ids: [String]?
    public let source_documents: [QueueSourceDocument]?
    public let retrieval_count: Int?
    public let last_retrieved_at: String?

    public var displayID: String? {
        id ?? fact_id
    }

    public var displayQuote: String? {
        evidence_quote ?? quote
    }

    public var displayConfidence: Double? {
        truth_confidence ?? confidence
    }
}

public struct QueueRouteCandidate: Codable, Equatable, Identifiable, Sendable {
    public let page_hint: String
    public let title: String?
    public let score: Double?
    public let page_type: String?
    public let document_coherence_count: Int?
    public let document_coherence_share: Double?

    public var id: String { page_hint }
}

public struct QueueMemory: Codable, Equatable, Sendable {
    public let id: String?
    public let content: String?
    public let memory_type: String?
    public let scope: String?
    public let confidence: Double?
    public let status: String?
    public let created_at: String?
    public let updated_at: String?
    public let source_ids: [String]?
    public let source_documents: [QueueSourceDocument]?
    public let audit: [String: JSONValue]?
}

public struct QueueSourceDocument: Codable, Equatable, Identifiable, Sendable {
    public let source_id: String
    public let title: String?
    public let source_type: String?
    public let source_path: String?
    public let raw_path: String?
    public let created_at: String?
    public let ingested_at: String?
    public let captured_at: String?
    public let uri: String?
    public let path: String?
    public let origin: String?
    public let source_refs: [String]?

    public var id: String { source_id }
}

public struct QueueDecisionResult: Codable, Equatable, Sendable {
    public let status: String
    public let item_id: String?
    public let result: [String: JSONValue]?
    public let undo_handle: JSONValue?
    public let queue_summary: QueueSummary?
}

public struct QueueUndoResult: Codable, Equatable, Sendable {
    public let status: String
    public let undo_handle: JSONValue?
    public let queue_summary: QueueSummary?
}

public struct ConnectorsResponse: Codable, Equatable, Sendable {
    public let connectors: [ConnectorPayload]
    public let count: Int
}

public struct WikiPagesResponse: Codable, Equatable, Sendable {
    public let pages: [WikiPageSummary]
    public let count: Int
}

public struct WikiPageSummary: Codable, Equatable, Identifiable, Sendable {
    public let title: String?
    public let page_type: String?
    public let status: String?
    public let relative_path: String
    public let source_ids: [String]?
    public let source_count: Int?
    public let updated_at: String?
    public let generated: Bool?
    public let related: [String]?
    public let score: Double?
    public let summary: String?

    public var id: String { relative_path }
    public var displayTitle: String { title ?? relative_path }
}

public struct WikiPageDetail: Codable, Equatable, Sendable {
    public let relative_path: String
    public let frontmatter: [String: JSONValue]?
    public let body: String?
    public let markdown: String?
    public let generated: Bool?
    public let source_ids: [String]?
    public let source_documents: [QueueSourceDocument]?
    public let facts: [QueueFact]?
    public let contract: [String: JSONValue]?
    public let snapshots: [WikiSnapshot]?
    public let related: [String]?

    public var displayTitle: String {
        frontmatter?["title"]?.stringValue ?? relative_path
    }
}

public struct WikiSnapshot: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let page_path: String?
    public let reason: String?
    public let created_at: String?
    public let before_preview: String?
    public let after_preview: String?
}

public struct EntitiesResponse: Codable, Equatable, Sendable {
    public let entities: [EntitySummary]
    public let count: Int
    public let types: [EntityTypeCount]
    public let sort: String?
}

public struct EntityTypeCount: Codable, Equatable, Identifiable, Sendable {
    public let entity_type: String
    public let count: Int

    public var id: String { entity_type }
}

public struct EntitySummary: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let entity_type: String?
    public let aliases: [String]?
    public let alias_count: Int?
    public let status: String?
    public let merged_into: String?
    public let fact_count: Int?
    public let retrieval_count: Int?
    public let last_retrieved_at: String?
    public let last_observed_at: String?
    public let created_at: String?
}

public struct EntityDetail: Codable, Equatable, Sendable {
    public let entity: EntitySummary
    public let facts_by_page: [EntityFactGroup]
    public let co_mentions: [EntityCoMention]
    public let merge_candidates: [EntityMergeCandidate]?
}

public struct EntityMergeCandidate: Codable, Equatable, Identifiable, Sendable {
    public let candidate_key: String?
    public let action_type: String?
    public let entity_ids: [String]
    public let canonical_entity_id: String?
    public let merged_entity_ids: [String]?
    public let entity_names: [String: String]?
    public let entity_types: [String: String]?
    public let page_hints: [String]?
    public let reason: String?
    public let merge_signal: String?
    public let score: Double?
    public let similarity: Double?
    public let affected_fact_count: Int?
    public let risk_tier: String?

    public var id: String {
        candidate_key ?? "entity_merge:\(entity_ids.sorted().joined(separator: ","))"
    }

    public var canonicalID: String? {
        canonical_entity_id ?? entity_ids.first
    }

    public var sourceIDs: [String] {
        if let merged_entity_ids, !merged_entity_ids.isEmpty {
            return merged_entity_ids
        }
        guard let canonicalID else {
            return []
        }
        return entity_ids.filter { $0 != canonicalID }
    }

    public func name(for entityID: String) -> String {
        entity_names?[entityID] ?? entityID
    }
}

public struct EntityMergeResponse: Codable, Equatable, Sendable {
    public let action: [String: JSONValue]
}

public struct EntityFactGroup: Codable, Equatable, Identifiable, Sendable {
    public let page_hint: String
    public let facts: [QueueFact]

    public var id: String { page_hint }
}

public struct EntityCoMention: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let entity_type: String?
    public let count: Int
}

public struct CurationSettingsResponse: Codable, Equatable, Sendable {
    public let strictness: String
    public let label: String
    public let minimum_auto_confidence: Double
    public let merge_aggressiveness: Double
    public let split_aggressiveness: Double
    public let topology_review_threshold: Int
    public let policy_version: Int?
    public let updated_at: String?
    public let configured: Bool
    public let changed: Bool
    public let applies_to: String
    public let existing_queue_unchanged: Bool
    public let topology_applies_to: String
    public let profiles: [CurationProfile]
    public let hard_review_boundaries: [String]
}

public struct CurationProfile: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let label: String
    public let minimum_auto_confidence: Double

    public init(id: String, label: String, minimum_auto_confidence: Double) {
        self.id = id
        self.label = label
        self.minimum_auto_confidence = minimum_auto_confidence
    }
}

public struct RetrieveRequest: Encodable, Sendable {
    public let task: String
    public let mode: String
    public let debug: Bool

    public init(task: String, mode: String = "default", debug: Bool = false) {
        self.task = task
        self.mode = mode
        self.debug = debug
    }
}

public struct RetrieveResult: Codable, Equatable, Sendable {
    public let task: String?
    public let project: String?
    public let budget: Int?
    public let retrieval_mode: String?
    public let retrieval_verdict: String?
    public let retrieval_confidence: Double?
    public let retrieval_reasons: [String]?
    public let relevant_facts: [RetrieveFact]?
    public let relevant_wiki_pages: [RetrieveWikiPage]?
    public let supporting_chunks: [RetrieveChunk]?
    public let active_memories: [RetrieveMemory]?
    public let candidate_memories: [RetrieveMemory]?
    public let retrieval_debug: [String: JSONValue]?
    public let raw: [String: JSONValue]?
}

public struct RetrieveFact: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let statement: String?
    public let page_hint: String?
    public let entity_id: String?
    public let retrieval_score: Double?
    public let score: Double?
    public let selection_reasons: [String]?
    public let fact_relevance_reasons: [String]?
}

public struct RetrieveWikiPage: Codable, Equatable, Identifiable, Sendable {
    public let relative_path: String
    public let title: String?
    public let score: Double?
    public let selection_reasons: [String]?

    public var id: String { relative_path }
}

public struct RetrieveChunk: Codable, Equatable, Identifiable, Sendable {
    public let chunk_id: String?
    public let id: String?
    public let document_id: String?
    public let title: String?
    public let source_type: String?
    public let text: String?
    public let snippet: String?
    public let score: Double?
    public let rerank_score: Double?
    public let selection_reasons: [String]?
    public let reasons: [String]?
    public let raw_context: [String: JSONValue]?

    public var stableID: String { chunk_id ?? id ?? document_id ?? title ?? snippet ?? text ?? "chunk" }
}

public struct RetrieveMemory: Codable, Equatable, Identifiable, Sendable {
    public let id: String?
    public let content: String?
    public let memory_type: String?
    public let scope: String?
    public let memory_relevance_score: Double?

    public var stableID: String { id ?? content ?? memory_type ?? scope ?? "memory" }
}

public struct ConnectorPayload: Codable, Equatable, Identifiable, Sendable {
    public let manifest: ConnectorManifestSummary
    public let state: ConnectorStateSummary
    public let health: ConnectorHealth

    public var id: String { manifest.id }
}

public struct ConnectorManifestSummary: Codable, Equatable, Sendable {
    public let id: String
    public let display_name: String
    public let description: String
    public let source_type: String
    public let default_enabled: Bool
    public let default_cadence_s: Int
    public let settings_schema: [ConnectorSettingField]
    public let permissions_note: String
    public let lifecycle: String?
    public let capture_available: Bool?
    public let auth: ConnectorAuthManifest?
    public let activation_note: String?

    public var canCapture: Bool { capture_available ?? true }
    public var lifecycleStatus: String { lifecycle ?? "active" }
}

public struct ConnectorAuthManifest: Codable, Equatable, Sendable {
    public let kind: String
    public let provider: String
    public let phase: String
    public let requested_scopes: [String]
    public let client_secret_required: Bool
    public let redirect_uri: String
    public let setup_url: String

    public func accessSummary(for connectorID: String) -> String {
        guard phase == "read_only" else {
            return "Identity only"
        }
        switch connectorID {
        case "gmail":
            return "Read Gmail messages and threads only"
        case "calendar":
            return "Read events on your owned primary calendar only"
        default:
            return "Operational read-only access"
        }
    }
}

public struct ConnectorSettingField: Codable, Equatable, Identifiable, Sendable {
    public let key: String
    public let label: String
    public let kind: String
    public let `default`: JSONValue?
    public let help: String
    public let choices: [String]

    public var id: String { key }
}

public struct ConnectorStateSummary: Codable, Equatable, Sendable {
    public let enabled: Bool
    public let cadence_s: Int
    public let settings: [String: JSONValue]
    public let auth: ConnectorAuthState?
}

public struct ConnectorAuthState: Codable, Equatable, Sendable {
    public let kind: String
    public let provider: String
    public let phase: String
    public let status: String
    public let client_id: String?
    public let client_secret_configured: Bool
    public let connected_at: String?
    public let account_label: String?
    public let granted_scopes: [String]
    public let requested_scopes: [String]
    public let redirect_uri: String
    public let setup_url: String
    public let can_authorize: Bool
    public let can_disconnect: Bool
    public let last_error: String?
}

public struct ConnectorAuthStartResponse: Codable, Equatable, Sendable {
    public let authorization_url: String
    public let redirect_uri: String
    public let expires_at: String
    public let connector: ConnectorPayload
}

public struct ConnectorHealth: Codable, Equatable, Sendable {
    public let status: String
    public let consecutive_failures: Int
    public let last_run_at: String?
    public let last_error: String?
    public let last_result: JSONValue?
}

public struct OpsRunsResponse: Codable, Equatable, Sendable {
    public let automation_runs: [OpsRun]
    public let ingestion_runs: [OpsRun]
}

public struct OpsRun: Codable, Equatable, Identifiable, Sendable {
    public let run_id: String?
    public let job_name: String?
    public let source_type: String?
    public let status: String?
    public let started_at: String?
    public let finished_at: String?
    public let error: String?

    public var id: String {
        run_id ?? "\(job_name ?? source_type ?? "run"):\(started_at ?? finished_at ?? "")"
    }

    private enum CodingKeys: String, CodingKey {
        case run_id = "id"
        case job_name
        case source_type
        case status
        case started_at
        case finished_at
        case error
    }
}

public struct StorageInventory: Codable, Equatable, Sendable {
    public let generated_at: String
    public let roots: [StorageEntry]
    public let details: [StorageEntry]
    public let managed_root_bytes: Int64
}

public struct StorageEntry: Codable, Equatable, Identifiable, Sendable {
    public let key: String
    public let path: String
    public let exists: Bool
    public let bytes: Int64
    public let policy: String
    public let item_count: Int?

    public var id: String { key }
}

public struct MigrationPlan: Codable, Equatable, Sendable {
    public let state: String
    public let home: String
    public let home_exists: Bool
    public let app_support: String
    public let launch_agents_dir: String
    public let detected_launch_agents: [DetectedLaunchAgent]
    public let rollback_script: String
    public let shim_dir: String
    public let steps: [MigrationStep]

    public var needsMigration: Bool {
        state == "migrate"
    }

    public var shouldShowAssistant: Bool {
        needsMigration || !detected_launch_agents.isEmpty
    }
}

public struct DetectedLaunchAgent: Codable, Equatable, Identifiable, Sendable {
    public let label: String
    public let plist_path: String
    public let role_set: String
    public let start_interval: Int?

    public var id: String { label }
}

public struct MigrationStep: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let label: String
    public let required: Bool
}

public struct TodayBriefing: Codable, Equatable, Sendable {
    public let schema_version: Int
    public let status: String
    public let availability_reason: String?
    public let briefing_id: String?
    public let generated_at: String
    public let as_of: String?
    public let timezone: String?
    public let freshness: TodayFreshness
    public let coverage: [TodayCoverage]
    public let focus: [TodayItem]
    public let urgent_overflow: TodayUrgentOverflow
    public let calendar: TodayCalendar
    public let due_overdue: [TodayItem]
    public let waiting: [TodayItem]
    public let attention: [TodayItem]
    public let awareness: [TodayItem]
    public let uncertain: [TodayItem]
    public let ignored_suppressed_count: Int?
    public let ignored_suppressed: [TodayItem]
    public let feedback: TodayFeedbackCapabilities

    public var isAvailable: Bool {
        status == "available" || status == "partial"
    }

    public var visibleFocus: [TodayItem] {
        Array(focus.prefix(5))
    }

    public var hasCoverageWarning: Bool {
        status == "partial" || coverage.contains { $0.state != "complete" }
    }

    public var ignoredSuppressedTotal: Int {
        max(ignored_suppressed_count ?? ignored_suppressed.count, ignored_suppressed.count)
    }
}

public struct TodayFreshness: Codable, Equatable, Sendable {
    public let state: String
    public let as_of: String?
    public let age_seconds: Int?
    public let stale_after_seconds: Int?
}

public struct TodayCoverage: Codable, Equatable, Identifiable, Sendable {
    public let source_id: String
    public let label: String
    public let state: String
    public let last_success_at: String?
    public let detail: String?
    public let deferred_count: Int

    public var id: String { source_id }
}

public struct TodayUrgentOverflow: Codable, Equatable, Sendable {
    public let count: Int
    public let items: [TodayItem]
}

public struct TodayCalendar: Codable, Equatable, Sendable {
    public let now: [TodayItem]
    public let next: [TodayItem]
}

public struct TodayFeedbackCapabilities: Codable, Equatable, Sendable {
    public let enabled: Bool
    public let actions: [String]
    public let unavailable_reason: String?

    public func allows(_ action: String) -> Bool {
        enabled && actions.contains(action)
    }
}

public struct TodayItem: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let section: String
    public let title: String
    public let summary: String?
    public let kind: String
    public let state: String
    public let priority: String?
    public let starts_at: String?
    public let due_at: String?
    public let ends_at: String?
    public let owner: String?
    public let counterparty: String?
    public let confidence: Double?
    public let handled_verdict: String
    public let handled_reason: String?
    public let reason_codes: [String]
    public let evidence: [TodayEvidenceLink]
    public let feedback_actions: [String]

    public var handledLabel: String {
        switch handled_verdict {
        case "needs_action": return "Needs action"
        case "responded_waiting": return "Responded · waiting"
        case "being_handled": return "Being handled"
        case "fulfilled": return "Fulfilled"
        default: return "Handling unknown"
        }
    }

    public var timingText: String? {
        if let due_at { return "Due \(due_at)" }
        if let starts_at { return "Starts \(starts_at)" }
        return ends_at.map { "Ends \($0)" }
    }

    public var supportsMeetingPreparation: Bool {
        kind == "event" && BrainAPIClient.canPrepareMeeting(itemID: id)
    }
}

public struct TodayEvidenceLink: Codable, Equatable, Identifiable, Sendable {
    public let source_type: String
    public let label: String
    public let reference: String
    public let brain_route: String?
    public let provider_url: String?

    public var id: String { "\(source_type):\(reference):\(label)" }
}

public struct TodayRetainedEvidence: Codable, Equatable, Sendable {
    public let schema_version: Int
    public let source_type: String
    public let account_key: String
    public let source_ref: String
    public let source_revision: String?
    public let retention_days: Int
    public let evidence: JSONValue

    public var sourceLabel: String {
        switch source_type {
        case "calendar": return "Google Calendar"
        case "gmail": return "Gmail"
        default: return "Local source"
        }
    }

    public var displayTitle: String {
        guard let object = evidence.objectValue else {
            return "Retained evidence"
        }
        return object["subject"]?.stringValue
            ?? object["title"]?.stringValue
            ?? "Retained evidence"
    }
}

public struct TodayMeetingPacket: Codable, Equatable, Sendable {
    public let schema_version: Int
    public let item_id: String
    public let generated_at: String
    public let title: String
    public let event_claims: [TodayMeetingClaim]
    public let knowledge_claims: [TodayMeetingClaim]
    public let wiki_context: [TodayMeetingWikiContext]
    public let suggestions: [TodayMeetingSuggestion]
    public let coverage: [String: JSONValue]
    public let retrieval_reasons: [String]
}

public struct TodayMeetingClaim: Codable, Equatable, Identifiable, Sendable {
    public let claim: String
    public let claim_type: String
    public let evidence_refs: [JSONValue]
    public let fact_id: String?
    public let confidence: Double?

    public var id: String {
        "\(claim_type):\(fact_id ?? claim)"
    }
}

public struct TodayMeetingWikiContext: Codable, Equatable, Identifiable, Sendable {
    public let title: String
    public let path: String?
    public let summary: String
    public let source_ids: [String]

    public var id: String { path ?? title }
}

public struct TodayMeetingSuggestion: Codable, Equatable, Identifiable, Sendable {
    public let suggestion: String
    public let is_factual_claim: Bool

    public var id: String { suggestion }
}

public struct TodayFeedbackResponse: Codable, Equatable, Sendable {
    public let schema_version: Int
    public let status: String
    public let item_id: String?
    public let action: String
    public let recorded_at: String?
    public let message: String
}

public struct TodayShadowRunStatus: Codable, Equatable, Sendable {
    public let schema_version: Int
    public let status: String
    public let message: String
    public let run_id: String?
    public let started_at: String?
    public let finished_at: String?
    public let counts: [String: JSONValue]?
    public let coverage: [String: JSONValue]?
    public let usage: [String: JSONValue]?

    public var succeeded: Bool {
        ["complete", "partial"].contains(status.lowercased())
    }

    public var isInProgress: Bool {
        ["accepted", "running"].contains(status.lowercased())
    }

    public var isTerminal: Bool {
        ["complete", "partial", "failed"].contains(status.lowercased())
    }

    public var displayKind: TodayShadowRunDisplayKind {
        switch status.lowercased() {
        case "accepted", "running":
            return .progress
        case "complete":
            return .complete
        case "partial":
            return .partial
        default:
            return .failed
        }
    }

    /// A non-progress response may arrive after the runner has already retained
    /// useful operational state, including when its final briefing projection fails.
    public var shouldRefreshBriefing: Bool {
        displayKind != .progress
    }
}

public enum TodayShadowRunDisplayKind: Equatable, Sendable {
    case progress
    case complete
    case partial
    case failed
}

public enum DaemonStatus: Equatable, Sendable {
    case idle
    case provisioning(String)
    case starting
    case running(DaemonHealth)
    case restarting(String)
    case failed(String)

    public var label: String {
        switch self {
        case .idle:
            return "Idle"
        case .provisioning(let message):
            return message
        case .starting:
            return "Starting"
        case .running:
            return "Running"
        case .restarting(let message):
            return message
        case .failed(let message):
            return message
        }
    }
}

private func joinedDetail(_ parts: [String]) -> String? {
    let nonEmpty = parts
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
    if nonEmpty.isEmpty {
        return nil
    }
    return nonEmpty.joined(separator: " ")
}
