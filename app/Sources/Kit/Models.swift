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
