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
}

private struct EmptyPayload: Encodable {}
