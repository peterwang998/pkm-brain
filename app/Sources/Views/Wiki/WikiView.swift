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
        .task {
            await loadPages()
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
                    markdownBody(selectedPage.body ?? "")
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

    private func markdownBody(_ body: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(displayLines(from: body).enumerated()), id: \.offset) { _index, line in
                Text(line.text)
                    .font(line.isHeading ? .headline : .body)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func factsSection(_ facts: [QueueFact]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Facts")
                .font(.title3.weight(.semibold))
            if facts.isEmpty {
                Text("No active facts on this page.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(Array(facts.prefix(20).enumerated()), id: \.offset) { _index, fact in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(fact.statement ?? "Untitled fact")
                            .textSelection(.enabled)
                        MetadataRow(values: [
                            confidenceText(fact.displayConfidence),
                            fact.section_hint,
                            fact.displayID.map(shortID),
                        ])
                    }
                    .padding(.vertical, 6)
                    Divider()
                }
            }
        }
    }

    private func metadataSection(_ page: WikiPageDetail) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Metadata")
                .font(.title3.weight(.semibold))
            MetadataRow(values: page.related?.map { "related \($0)" } ?? [])
            if let contract = page.contract, !contract.isEmpty {
                Text(contract["retrieval_purpose"]?.stringValue ?? contract["page_scope"]?.stringValue ?? "Contract available.")
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            SourceDocumentsView(documents: page.source_documents ?? [])
        }
    }

    private func loadPages() async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let response = try await client.wikiPages()
            pages = response.pages
            errorMessage = nil
            let nextPath = selectedPath ?? pages.first?.relative_path
            if let nextPath {
                await selectPage(nextPath)
            }
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func selectPage(_ path: String) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        selectedPath = path
        do {
            selectedPage = try await client.wikiPage(path: path)
            errorMessage = nil
        } catch {
            errorMessage = String(describing: error)
        }
    }
}

private func displayLines(from markdown: String) -> [(text: String, isHeading: Bool)] {
    markdown
        .split(separator: "\n", omittingEmptySubsequences: false)
        .map { rawLine in
            let line = String(rawLine)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("#") {
                return (text: trimmed.trimmingCharacters(in: CharacterSet(charactersIn: "# ")), isHeading: true)
            }
            return (text: line, isHeading: false)
        }
        .filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
}
