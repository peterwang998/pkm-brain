import AppKit
import PKMBrainKit
import SwiftUI

struct WikiView: View {
    @EnvironmentObject private var appState: AppState
    @State private var pages: [WikiPageSummary] = []
    @State private var selectedPath: String?
    @State private var selectedPage: WikiPageDetail?
    @State private var searchText = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var visibleFactCount = 20
    @State private var factActionID: String?
    @State private var factActionMessage: String?

    private var filteredPages: [WikiPageSummary] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else {
            return pages
        }
        return pages.filter { page in
            "\(page.displayTitle) \(page.relative_path) \(page.page_type ?? "")"
                .lowercased()
                .contains(query)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                pageList
                    .frame(width: 320)
                Divider()
                pageDetail
            }
        }
        .task(id: appState.daemon.handshake?.pid) {
            await loadPages()
        }
        .onChange(of: appState.requestedWikiPath) { _, requested in
            guard let requested else {
                return
            }
            Task {
                await selectPage(requested)
                appState.requestedWikiPath = nil
            }
        }
        .toolbar {
            Button {
                Task { await loadPages() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("Wiki")
                    .font(.largeTitle.weight(.semibold))
                Text("\(pages.count) pages")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if isLoading {
                ProgressView()
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 16)
    }

    private var pageList: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField("Search pages", text: $searchText)
                .textFieldStyle(.roundedBorder)
                .padding(.horizontal, 12)
                .padding(.top, 12)
            if let errorMessage {
                Text(errorMessage)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 12)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    if filteredPages.isEmpty, !isLoading {
                        Text("No wiki pages found.")
                            .foregroundStyle(.secondary)
                            .padding(12)
                    }
                    ForEach(filteredPages) { page in
                        Button {
                            Task { await selectPage(page.relative_path) }
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(page.displayTitle)
                                    .font(.callout.weight(page.relative_path == selectedPath ? .semibold : .regular))
                                    .lineLimit(2)
                                Text(page.relative_path)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                MetadataRow(values: [page.page_type, page.status, countText(page.source_count, "sources")])
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .background(page.relative_path == selectedPath ? Color.accentColor.opacity(0.13) : Color.clear)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 12)
            }
        }
    }

    private var pageDetail: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if isLoading, selectedPage == nil {
                    ProgressView("Loading wiki...")
                } else if let selectedPage {
                    pageHeader(selectedPage)
                    MarkdownDocumentView(markdown: selectedPage.body ?? "")
                    factsSection(selectedPage.facts ?? [])
                    metadataSection(selectedPage)
                } else {
                    Text("Select a page.")
                        .foregroundStyle(.secondary)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func pageHeader(_ page: WikiPageDetail) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(page.displayTitle)
                .font(.title2.weight(.semibold))
                .textSelection(.enabled)
            Text(page.relative_path)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            MetadataRow(values: [
                page.generated == true ? "generated" : "editable",
                countText(page.facts?.count, "facts"),
                countText(page.source_ids?.count, "sources"),
            ])
        }
    }

    private func factsSection(_ facts: [QueueFact]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Facts")
                    .font(.title3.weight(.semibold))
                Spacer()
                if !facts.isEmpty {
                    Text("Showing \(min(visibleFactCount, facts.count)) of \(facts.count)")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            if let factActionMessage {
                Text(factActionMessage)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            if facts.isEmpty {
                Text("No active facts on this page.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(Array(facts.prefix(visibleFactCount).enumerated()), id: \.offset) { _index, fact in
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: 8) {
                            if let quote = fact.displayQuote, !quote.isEmpty {
                                Text(quote)
                                    .font(.callout.monospaced())
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            } else {
                                Text("No verbatim quote is stored for this fact.")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                            }
                            MetadataRow(values: [
                                fact.source_date.map { "source \($0)" },
                                fact.source_date_basis,
                                fact.entity_key,
                            ])
                            factSources(fact.source_documents ?? [])
                            if let factID = fact.displayID {
                                HStack(spacing: 8) {
                                    Button {
                                        Task { await actOnFact(factID, confirm: true) }
                                    } label: {
                                        Label("Confirm", systemImage: "checkmark.circle")
                                    }
                                    .disabled(factActionID != nil)
                                    Button {
                                        Task { await actOnFact(factID, confirm: false) }
                                    } label: {
                                        Label("Flag", systemImage: "flag")
                                    }
                                    .disabled(factActionID != nil)
                                    if factActionID == factID {
                                        ProgressView()
                                            .controlSize(.small)
                                    }
                                }
                            }
                        }
                        .padding(.top, 6)
                    } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(fact.statement ?? "Untitled fact")
                                .textSelection(.enabled)
                            HStack(spacing: 8) {
                                ConfidenceBadge(value: fact.displayConfidence)
                                MetadataRow(values: [
                                    fact.section_hint,
                                    fact.displayID.map(shortID),
                                ])
                            }
                        }
                    }
                    .padding(.vertical, 6)
                    Divider()
                }
                if visibleFactCount < facts.count {
                    Button {
                        visibleFactCount = min(facts.count, visibleFactCount + 20)
                    } label: {
                        Label(
                            "Show \(min(20, facts.count - visibleFactCount)) more",
                            systemImage: "chevron.down"
                        )
                    }
                }
            }
        }
    }

    private func metadataSection(_ page: WikiPageDetail) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Metadata")
                .font(.title3.weight(.semibold))
            if let contract = page.contract, !contract.isEmpty {
                DisclosureGroup("Page Contract") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(contract.keys.sorted(), id: \.self) { key in
                            LabeledContent(key.replacingOccurrences(of: "_", with: " ")) {
                                Text(jsonText(contract[key]))
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    .padding(.top, 6)
                }
            }
            if let snapshots = page.snapshots, !snapshots.isEmpty {
                DisclosureGroup("Recent Changes (\(snapshots.count))") {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(snapshots) { snapshot in
                            VStack(alignment: .leading, spacing: 4) {
                                MetadataRow(values: [snapshot.created_at, snapshot.reason])
                                if let before = snapshot.before_preview, !before.isEmpty {
                                    Text("Before: \(before)")
                                        .foregroundStyle(.secondary)
                                }
                                if let after = snapshot.after_preview, !after.isEmpty {
                                    Text("After: \(after)")
                                }
                            }
                            Divider()
                        }
                    }
                    .padding(.top, 6)
                }
            }
            if let related = page.related, !related.isEmpty {
                MetadataRow(values: related.map { "related \($0)" })
            }
            SourceDocumentsView(documents: page.source_documents ?? [])
        }
    }

    private func loadPages() async {
        isLoading = true
        defer { isLoading = false }
        guard let client = await appState.waitForAPIClient() else {
            if !Task.isCancelled {
                errorMessage = "Daemon API is unavailable."
            }
            return
        }
        do {
            let response = try await client.wikiPages()
            pages = response.pages
            errorMessage = nil
            let nextPath = appState.requestedWikiPath ?? selectedPath ?? pages.first?.relative_path
            if let nextPath {
                await selectPage(nextPath)
                if appState.requestedWikiPath == nextPath {
                    appState.requestedWikiPath = nil
                }
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func selectPage(_ path: String) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        selectedPath = path
        visibleFactCount = 20
        factActionMessage = nil
        do {
            selectedPage = try await client.wikiPage(path: path)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func factSources(_ documents: [QueueSourceDocument]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(documents.prefix(8)) { document in
                HStack(spacing: 8) {
                    Image(systemName: "doc.text")
                        .foregroundStyle(.secondary)
                    Text(document.title ?? document.source_id)
                        .lineLimit(2)
                    Spacer()
                    Button {
                        openSource(document)
                    } label: {
                        Image(systemName: "folder")
                    }
                    .buttonStyle(.borderless)
                    .help("Reveal source")
                    .disabled(sourcePath(document) == nil)
                }
            }
        }
    }

    private func actOnFact(_ factID: String, confirm: Bool) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        factActionID = factID
        defer { factActionID = nil }
        do {
            if confirm {
                _ = try await client.confirmWikiFact(factID)
                factActionMessage = "Fact confirmed."
            } else {
                _ = try await client.flagWikiFact(factID)
                factActionMessage = "Fact added to review."
            }
            if let selectedPath {
                selectedPage = try await client.wikiPage(path: selectedPath)
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func sourcePath(_ document: QueueSourceDocument) -> String? {
        [document.raw_path, document.source_path, document.path]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty }
    }

    private func openSource(_ document: QueueSourceDocument) {
        guard let path = sourcePath(document) else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    private func jsonText(_ value: JSONValue?) -> String {
        guard let value else {
            return "-"
        }
        switch value {
        case .string(let text): return text
        case .number(let number): return number.formatted()
        case .bool(let enabled): return enabled ? "yes" : "no"
        case .array(let values): return values.map { jsonText($0) }.joined(separator: ", ")
        case .object: return "Structured value"
        case .null: return "-"
        }
    }
}
