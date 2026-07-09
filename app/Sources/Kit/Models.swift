import Foundation

public struct DaemonHandshake: Codable, Equatable, Sendable {
    public let pid: Int
    public let port: Int
    public let token: String
    public let version: String
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
    public let last_error: String?
    public let next_due_at: String?
    public let running: Bool
    public let queued: Bool?
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
    public let queue_counts: QueueCounts
    public let raw: [String: JSONValue]?
}

public struct QueuePage: Codable, Equatable, Sendable {
    public let kind: String
    public let counts: QueueCounts
    public let total: Int
    public let cursor: Int
    public let next_cursor: Int?
    public let items: [QueueItem]

    public init(
        kind: String,
        counts: QueueCounts,
        total: Int,
        cursor: Int,
        next_cursor: Int?,
        items: [QueueItem]
    ) {
        self.kind = kind
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
    public let route_candidates: [QueueRouteCandidate]?
    public let memory: QueueMemory?
    public let action: [String: JSONValue]?
    public let proposal: JSONValue?
    public let question: [String: JSONValue]?
    public let options: [JSONValue]?
    public let raw: JSONValue?

    public var displayTitle: String {
        title ?? summary ?? id
    }
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
    public let source_ids: [String]?
    public let source_documents: [QueueSourceDocument]?

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
    public let ingested_at: String?
    public let captured_at: String?
    public let uri: String?
    public let path: String?
    public let origin: String?

    public var id: String { source_id }
}

public struct QueueDecisionResult: Codable, Equatable, Sendable {
    public let status: String
    public let item_id: String?
    public let result: [String: JSONValue]?
    public let undo_handle: JSONValue?
}

public struct QueueUndoResult: Codable, Equatable, Sendable {
    public let status: String
    public let undo_handle: JSONValue?
}

public struct ConnectorsResponse: Codable, Equatable, Sendable {
    public let connectors: [ConnectorPayload]
    public let count: Int
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
}

public struct ConnectorHealth: Codable, Equatable, Sendable {
    public let status: String
    public let consecutive_failures: Int
    public let last_run_at: String?
    public let last_error: String?
    public let last_result: JSONValue?
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
