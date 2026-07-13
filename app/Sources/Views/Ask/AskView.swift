import AppKit
import PKMBrainKit
import SwiftUI

struct AskView: View {
    @EnvironmentObject private var appState: AppState
    @State private var task = ""
    @State private var mode = "default"
    @State private var debug = false
    @State private var result: RetrieveResult?
    @State private var history: [AskHistoryItem] = []
    @State private var isRunning = false
    @State private var errorMessage: String?

    private let modes = ["default", "compact", "broad", "inspect"]

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                resultPane
                Divider()
                historyPane
                    .frame(width: 260)
            }
        }
        .task {
            history = AskHistoryItem.load()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Ask")
                        .font(.largeTitle.weight(.semibold))
                    Text("Retrieve a context packet from Brain.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if isRunning {
                    ProgressView()
                        .controlSize(.small)
                }
            }
            HStack(alignment: .top, spacing: 10) {
                TextField("Retrieve context for...", text: $task, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...3)
                    .onSubmit {
                        Task { await runAsk() }
                    }
                Picker("Mode", selection: $mode) {
                    ForEach(modes, id: \.self) { mode in
                        Text(mode).tag(mode)
                    }
                }
                .pickerStyle(.menu)
                .frame(width: 120)
                Toggle("Debug", isOn: $debug)
                    .toggleStyle(.checkbox)
                Button {
                    Task { await runAsk() }
                } label: {
                    Label("Run", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
                .disabled(task.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isRunning)
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 16)
    }

    private var resultPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let errorMessage {
                    Text(errorMessage)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                }
                if isRunning, result == nil {
                    ProgressView("Retrieving context...")
                } else if let result {
                    verdictBanner(result)
                    retrievalTrace(result.retrieval_debug)
                    factsSection(result.relevant_facts ?? [])
                    pagesSection(result.relevant_wiki_pages ?? [])
                    chunksSection(result.supporting_chunks ?? [])
                    memoriesSection(
                        title: "Active Memories",
                        memories: result.active_memories ?? []
                    )
                    memoriesSection(
                        title: "Candidate Memories",
                        memories: result.candidate_memories ?? []
                    )
                } else {
                    Text("Run a retrieval to inspect the packet.")
                        .foregroundStyle(.secondary)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var historyPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("History")
                .font(.title3.weight(.semibold))
            if history.isEmpty {
                Text("No recent asks.")
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(history) { item in
                            Button {
                                task = item.task
                                mode = item.mode
                            } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(item.task)
                                        .lineLimit(3)
                                    Text(item.mode)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(8)
                            }
                            .buttonStyle(.plain)
                            .background(Color.secondary.opacity(0.08))
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }
            }
        }
        .padding(16)
        .frame(maxHeight: .infinity, alignment: .topLeading)
    }

    private func verdictBanner(_ result: RetrieveResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            MetadataRow(values: [
                result.retrieval_verdict ?? "unknown",
                scoreText(result.retrieval_confidence),
                result.retrieval_mode,
            ])
            if let reasons = result.retrieval_reasons, !reasons.isEmpty {
                Text(reasons.joined(separator: " "))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(verdictColor(result.retrieval_verdict).opacity(0.13))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func factsSection(_ facts: [RetrieveFact]) -> some View {
        resultSection(title: "Facts", isEmpty: facts.isEmpty, emptyText: "No facts returned.") {
            ForEach(facts) { fact in
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(fact.statement ?? "Untitled fact")
                            .textSelection(.enabled)
                        Spacer()
                        if let page = fact.page_hint {
                            Button {
                                appState.showWiki(path: page)
                            } label: {
                                Image(systemName: "doc.text")
                            }
                            .buttonStyle(.borderless)
                            .help("Open owning Wiki page")
                        }
                        if let entityID = fact.entity_id {
                            Button {
                                appState.showEntity(id: entityID)
                            } label: {
                                Image(systemName: "person.crop.circle")
                            }
                            .buttonStyle(.borderless)
                            .help("Open owning entity")
                        }
                    }
                    MetadataRow(values: [
                        scoreText(fact.retrieval_score ?? fact.score),
                        fact.page_hint,
                        shortID(fact.id),
                    ])
                    reasonsText(fact.selection_reasons ?? fact.fact_relevance_reasons)
                }
                .padding(.vertical, 5)
                Divider()
            }
        }
    }

    private func pagesSection(_ pages: [RetrieveWikiPage]) -> some View {
        resultSection(title: "Wiki Pages", isEmpty: pages.isEmpty, emptyText: "No wiki pages returned.") {
            ForEach(pages) { page in
                VStack(alignment: .leading, spacing: 5) {
                    Button {
                        appState.showWiki(path: page.relative_path)
                    } label: {
                        Label(
                            page.title ?? page.relative_path,
                            systemImage: "doc.text"
                        )
                        .font(.callout.weight(.medium))
                    }
                    .buttonStyle(.plain)
                    MetadataRow(values: [
                        scoreText(page.score),
                        page.relative_path,
                    ])
                    reasonsText(page.selection_reasons)
                }
                .padding(.vertical, 5)
                Divider()
            }
        }
    }

    private func chunksSection(_ chunks: [RetrieveChunk]) -> some View {
        resultSection(title: "Chunks", isEmpty: chunks.isEmpty, emptyText: "No chunks returned.") {
            ForEach(chunks, id: \.stableID) { chunk in
                VStack(alignment: .leading, spacing: 5) {
                    Text(chunk.text ?? chunk.snippet ?? "")
                        .lineLimit(8)
                        .textSelection(.enabled)
                    MetadataRow(values: [
                        scoreText(chunk.score ?? chunk.rerank_score),
                        chunk.title,
                        chunk.source_type,
                        chunk.document_id.map(shortID),
                    ])
                    reasonsText(chunk.selection_reasons ?? chunk.reasons)
                    if chunkSourcePath(chunk) != nil {
                        Button {
                            revealChunkSource(chunk)
                        } label: {
                            Label("Reveal Source", systemImage: "folder")
                        }
                        .buttonStyle(.borderless)
                    }
                }
                .padding(.vertical, 5)
                Divider()
            }
        }
    }

    private func memoriesSection(title: String, memories: [RetrieveMemory]) -> some View {
        resultSection(title: title, isEmpty: memories.isEmpty, emptyText: "None returned.") {
            ForEach(memories, id: \.stableID) { memory in
                VStack(alignment: .leading, spacing: 5) {
                    Text(memory.content ?? "")
                        .textSelection(.enabled)
                    MetadataRow(values: [
                        scoreText(memory.memory_relevance_score),
                        memory.memory_type,
                        memory.scope,
                        memory.id.map(shortID),
                    ])
                }
                .padding(.vertical, 5)
                Divider()
            }
        }
    }

    private func resultSection<Content: View>(
        title: String,
        isEmpty: Bool,
        emptyText: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.title3.weight(.semibold))
            if isEmpty {
                Text(emptyText)
                    .foregroundStyle(.secondary)
            } else {
                content()
            }
        }
        .opacity(isEmpty ? 0.75 : 1)
    }

    private func reasonsText(_ reasons: [String]?) -> some View {
        Group {
            if let reasons, !reasons.isEmpty {
                Text(reasons.joined(separator: " "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private func verdictColor(_ verdict: String?) -> Color {
        switch verdict {
        case "found":
            return .green
        case "no_strong_match":
            return .red
        case "partial":
            return .yellow
        default:
            return .secondary
        }
    }

    private func retrievalTrace(_ trace: [String: JSONValue]?) -> some View {
        Group {
            if let trace, !trace.isEmpty {
                DisclosureGroup("Retrieval Trace") {
                    Text(prettyJSON(.object(trace)))
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                }
            }
        }
    }

    private func chunkSourcePath(_ chunk: RetrieveChunk) -> String? {
        guard let context = chunk.raw_context else {
            return nil
        }
        return ["raw_path", "source_path", "path"]
            .compactMap { context[$0]?.stringValue }
            .first { !$0.isEmpty }
    }

    private func revealChunkSource(_ chunk: RetrieveChunk) {
        guard let path = chunkSourcePath(chunk) else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    private func prettyJSON(_ value: JSONValue) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        guard let data = try? encoder.encode(value) else {
            return "Debug detail unavailable."
        }
        return String(decoding: data, as: UTF8.self)
    }

    private func runAsk() async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        let trimmedTask = task.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTask.isEmpty else {
            return
        }
        isRunning = true
        defer { isRunning = false }
        do {
            result = try await client.retrieve(RetrieveRequest(task: trimmedTask, mode: mode, debug: debug))
            errorMessage = nil
            history = AskHistoryItem.push(.init(task: trimmedTask, mode: mode), into: history)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct AskHistoryItem: Codable, Identifiable, Equatable {
    let task: String
    let mode: String

    var id: String { "\(mode):\(task)" }

    static func load() -> [AskHistoryItem] {
        guard let data = UserDefaults.standard.data(forKey: "PKMBrain.askHistory") else {
            return []
        }
        return (try? JSONDecoder().decode([AskHistoryItem].self, from: data)) ?? []
    }

    static func push(_ item: AskHistoryItem, into existing: [AskHistoryItem]) -> [AskHistoryItem] {
        let next = ([item] + existing.filter { $0.task != item.task }).prefix(10)
        let items = Array(next)
        if let data = try? JSONEncoder().encode(items) {
            UserDefaults.standard.set(data, forKey: "PKMBrain.askHistory")
        }
        return items
    }
}
