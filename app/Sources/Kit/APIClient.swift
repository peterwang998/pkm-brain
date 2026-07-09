import Foundation

public enum APIClientError: Error, Equatable {
    case invalidResponse
    case httpStatus(Int, String)
}

public final class BrainAPIClient: Sendable {
    public let baseURL: URL
    public let token: String
    private let session: URLSession
    private let decoder = JSONDecoder()

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    public func health() async throws -> DaemonHealth {
        try await get("/api/health")
    }

    public func scheduler() async throws -> SchedulerState {
        try await get("/api/scheduler")
    }

    public func digest(since: String? = nil) async throws -> Digest {
        var path = "/api/digest"
        if let since, let escaped = since.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
            path += "?since=\(escaped)"
        }
        return try await get(path)
    }

    public func queue(kind: String = "all", limit: Int = 50, cursor: Int = 0) async throws -> QueuePage {
        let escapedKind = percentEncodeQueryValue(kind)
        return try await get("/api/queue?kind=\(escapedKind)&limit=\(limit)&cursor=\(cursor)")
    }

    public func decideQueueItem(
        _ itemID: String,
        decision: String,
        payload: [String: JSONValue] = [:]
    ) async throws -> QueueDecisionResult {
        var body = payload
        body["decision"] = .string(decision)
        return try await post(
            "/api/queue/\(percentEncodePathComponent(itemID))/decision",
            payload: body
        )
    }

    public func undoQueueDecision(_ handle: JSONValue) async throws -> QueueUndoResult {
        try await post("/api/queue/undo", payload: ["undo_handle": handle])
    }

    public func connectors() async throws -> ConnectorsResponse {
        try await get("/api/connectors")
    }

    public func wikiPages(query: String = "") async throws -> WikiPagesResponse {
        if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return try await get("/api/wiki/pages")
        }
        return try await get("/api/wiki/pages?q=\(percentEncodeQueryValue(query))")
    }

    public func wikiPage(path: String) async throws -> WikiPageDetail {
        try await get("/api/wiki/page?path=\(percentEncodeQueryValue(path))")
    }

    public func entities(
        query: String = "",
        type: String = "",
        includeInactive: Bool = false
    ) async throws -> EntitiesResponse {
        var items: [String] = []
        let trimmedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedQuery.isEmpty {
            items.append("q=\(percentEncodeQueryValue(trimmedQuery))")
        }
        if !type.isEmpty {
            items.append("type=\(percentEncodeQueryValue(type))")
        }
        if includeInactive {
            items.append("inactive=1")
        }
        let suffix = items.isEmpty ? "" : "?\(items.joined(separator: "&"))"
        return try await get("/api/entities\(suffix)")
    }

    public func entityDetail(_ entityID: String) async throws -> EntityDetail {
        try await get("/api/entities/\(percentEncodePathComponent(entityID))")
    }

    public func retrieve(_ request: RetrieveRequest) async throws -> RetrieveResult {
        try await post("/api/retrieve", payload: request)
    }

    public func runSchedulerJob(_ jobID: String) async throws -> SchedulerState {
        try await post("/api/scheduler/run", payload: ["job_id": jobID])
    }

    public func pauseScheduler(seconds: Int) async throws -> SchedulerState {
        try await post("/api/scheduler/pause", payload: ["seconds": seconds])
    }

    public func resumeScheduler() async throws -> SchedulerState {
        try await post("/api/scheduler/resume", payload: EmptyPayload())
    }

    public func shutdown() async throws -> [String: JSONValue] {
        try await post("/api/shutdown", payload: EmptyPayload())
    }

    public func migrationPlan() async throws -> MigrationPlan {
        try await get("/api/migration")
    }

    public func installMigrationShims() async throws -> [String: JSONValue] {
        try await post("/api/migration/shims", payload: EmptyPayload())
    }

    public func dryRunLaunchAgentRetirement() async throws -> [String: JSONValue] {
        try await post("/api/migration/launch-agents/retire", payload: ["dry_run": true])
    }

    public func get<T: Decodable>(_ path: String) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "GET"
        return try await decode(request)
    }

    public func post<T: Decodable, P: Encodable>(_ path: String, payload: P) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "POST"
        request.httpBody = try JSONEncoder().encode(payload)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return try await decode(request)
    }

    private func decode<T: Decodable>(_ request: URLRequest) async throws -> T {
        var request = request
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIClientError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIClientError.httpStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return try decoder.decode(T.self, from: data)
    }

    private func url(for path: String) -> URL {
        URL(string: path, relativeTo: baseURL)!.absoluteURL
    }

    private func percentEncodePathComponent(_ value: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    private func percentEncodeQueryValue(_ value: String) -> String {
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "&=")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }
}

private struct EmptyPayload: Encodable {}
