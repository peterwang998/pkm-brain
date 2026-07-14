import Foundation

public enum APIClientError: Error, Equatable, LocalizedError {
    case invalidResponse
    case httpStatus(Int, String)

    public var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "The PKM Brain service returned an invalid response."
        case let .httpStatus(status, body):
            return Self.responseMessage(body) ?? "PKM Brain request failed (HTTP \(status))."
        }
    }

    private static func responseMessage(_ body: String) -> String? {
        guard let data = body.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : body
        }
        if let message = object["error"] as? String, !message.isEmpty {
            return message
        }
        if let error = object["error"] as? [String: Any],
           let message = error["message"] as? String,
           !message.isEmpty {
            return message
        }
        return nil
    }
}

public final class BrainAPIClient: Sendable {
    public let baseURL: URL
    public let token: String
    private let session: URLSession

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

    public func queue(
        kind: String = "all",
        state: String = "actionable",
        sort: String = "priority",
        limit: Int = 50,
        cursor: Int = 0
    ) async throws -> QueuePage {
        let escapedKind = percentEncodeQueryValue(kind)
        let escapedState = percentEncodeQueryValue(state)
        let escapedSort = percentEncodeQueryValue(sort)
        return try await get(
            "/api/queue?kind=\(escapedKind)&state=\(escapedState)&sort=\(escapedSort)&limit=\(limit)&cursor=\(cursor)"
        )
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

    public func setConnectorEnabled(
        _ connectorID: String,
        enabled: Bool
    ) async throws -> ConnectorPayload {
        let command = enabled ? "enable" : "disable"
        return try await post(
            "/api/connectors/\(percentEncodePathComponent(connectorID))/\(command)",
            payload: EmptyPayload()
        )
    }

    public func runConnector(_ connectorID: String) async throws -> [String: JSONValue] {
        try await post(
            "/api/connectors/\(percentEncodePathComponent(connectorID))/run",
            payload: EmptyPayload()
        )
    }

    public func configureConnectorAuth(
        _ connectorID: String,
        clientID: String,
        clientSecret: String?
    ) async throws -> ConnectorPayload {
        try await put(
            "/api/connectors/\(percentEncodePathComponent(connectorID))/auth/config",
            payload: ConnectorAuthConfigRequest(
                client_id: clientID,
                client_secret: clientSecret
            )
        )
    }

    public func startConnectorAuth(_ connectorID: String) async throws -> ConnectorAuthStartResponse {
        try await post(
            "/api/connectors/\(percentEncodePathComponent(connectorID))/auth/start",
            payload: EmptyPayload()
        )
    }

    public func disconnectConnectorAuth(_ connectorID: String) async throws -> ConnectorPayload {
        try await post(
            "/api/connectors/\(percentEncodePathComponent(connectorID))/auth/disconnect",
            payload: EmptyPayload()
        )
    }

    public func wikiPages(query: String = "") async throws -> WikiPagesResponse {
        if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return try await get("/api/wiki/pages")
        }
        return try await get("/api/wiki/pages?q=\(percentEncodeQueryValue(query))")
    }

    public func routableWikiPages() async throws -> WikiPagesResponse {
        try await get("/api/wiki/pages?routable=1")
    }

    public func wikiPage(path: String) async throws -> WikiPageDetail {
        try await get("/api/wiki/page?path=\(percentEncodeQueryValue(path))")
    }

    public func confirmWikiFact(_ factID: String) async throws -> [String: JSONValue] {
        try await post(
            "/api/wiki/facts/\(percentEncodePathComponent(factID))/confirm",
            payload: EmptyPayload()
        )
    }

    public func flagWikiFact(
        _ factID: String,
        reason: String = "Flagged for review from the native Wiki"
    ) async throws -> [String: JSONValue] {
        try await post(
            "/api/wiki/facts/\(percentEncodePathComponent(factID))/flag",
            payload: ["reason": reason]
        )
    }

    public func entities(
        query: String = "",
        type: String = "",
        includeInactive: Bool = false,
        sort: String = "retrieval"
    ) async throws -> EntitiesResponse {
        var items: [String] = ["sort=\(percentEncodeQueryValue(sort))"]
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

    public func proposeEntityMerge(
        _ candidate: EntityMergeCandidate
    ) async throws -> EntityMergeResponse {
        if candidate.action_type == "entity_merge" {
            return try await post(
                "/api/entities/merge",
                payload: EntityMergeCandidateRequest(candidate: candidate)
            )
        }
        let canonical = candidate.canonicalID ?? ""
        let merged = candidate.sourceIDs.map(JSONValue.string)
        let payload: [String: JSONValue] = [
            "canonical_entity_id": .string(canonical),
            "merged_entity_ids": .array(merged),
            "reason": .string(candidate.reason ?? "manual native merge proposal"),
            "confidence": .number(candidate.score ?? 1.0),
            "risk_tier": .string(candidate.risk_tier ?? "medium"),
        ]
        return try await post("/api/entities/merge", payload: payload)
    }

    public func curationSettings() async throws -> CurationSettingsResponse {
        try await get("/api/settings/curation")
    }

    public func updateCurationSettings(
        strictness: String,
        mergeAggressiveness: Double,
        splitAggressiveness: Double,
        topologyReviewThreshold: Int
    ) async throws -> CurationSettingsResponse {
        let payload: [String: JSONValue] = [
            "strictness": .string(strictness),
            "merge_aggressiveness": .number(mergeAggressiveness),
            "split_aggressiveness": .number(splitAggressiveness),
            "topology_review_threshold": .number(Double(topologyReviewThreshold)),
        ]
        return try await put(
            "/api/settings/curation",
            payload: payload
        )
    }

    public func retrieve(_ request: RetrieveRequest) async throws -> RetrieveResult {
        try await post("/api/retrieve", payload: request)
    }

    public func opsRuns() async throws -> OpsRunsResponse {
        try await get("/api/ops/runs")
    }

    public func storageInventory() async throws -> StorageInventory {
        try await get("/api/ops/storage")
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

    public func put<T: Decodable, P: Encodable>(_ path: String, payload: P) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "PUT"
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
        return try JSONDecoder().decode(T.self, from: data)
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

private struct ConnectorAuthConfigRequest: Encodable {
    let client_id: String
    let client_secret: String?
}

private struct EntityMergeCandidateRequest: Encodable {
    let candidate: EntityMergeCandidate
}
