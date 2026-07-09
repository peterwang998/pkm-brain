import PKMBrainKit
import SwiftUI

struct EntitiesView: View {
    @EnvironmentObject private var appState: AppState
    @State private var response: EntitiesResponse?
    @State private var selectedID: String?
    @State private var detail: EntityDetail?
    @State private var searchText = ""
    @State private var selectedType = ""
    @State private var includeInactive = false
    @State private var isLoading = false
    @State private var errorMessage: String?

    private var entities: [EntitySummary] {
        response?.entities ?? []
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                entityList
                    .frame(width: 360)
                Divider()
                entityDetail
            }
        }
        .task {
            await loadIndex(selectFirst: true)
        }
        .toolbar {
            Button {
                Task { await loadIndex(selectFirst: false) }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("Entities")
                    .font(.largeTitle.weight(.semibold))
                Text("\(response?.count ?? 0) active entities")
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

    private var entityList: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 8) {
                TextField("Search entities", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit {
                        Task { await loadIndex(selectFirst: true) }
                    }
                Toggle("Inactive", isOn: $includeInactive)
                    .onChange(of: includeInactive) { _, _ in
                        Task { await loadIndex(selectFirst: true) }
                    }
                typeFilter
            }
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
                    if entities.isEmpty, !isLoading {
                        Text("No entities found.")
                            .foregroundStyle(.secondary)
                            .padding(12)
                    }
                    ForEach(entities) { entity in
                        Button {
                            Task { await selectEntity(entity.id) }
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(entity.name)
                                    .font(.callout.weight(entity.id == selectedID ? .semibold : .regular))
                                    .lineLimit(1)
                                Text(shortID(entity.id))
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                MetadataRow(values: [
                                    entity.entity_type,
                                    countText(entity.fact_count, "facts"),
                                    countText(entity.alias_count, "aliases"),
                                    entity.last_observed_at,
                                ])
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 8)
                            .background(entity.id == selectedID ? Color.accentColor.opacity(0.13) : Color.clear)
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

    private var typeFilter: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 6) {
                typeButton(label: "all", type: "")
                ForEach(response?.types ?? []) { row in
                    typeButton(label: "\(row.entity_type) \(row.count)", type: row.entity_type)
                }
            }
        }
        .scrollIndicators(.hidden)
    }

    private func typeButton(label: String, type: String) -> some View {
        Button(label) {
            selectedType = type
            Task { await loadIndex(selectFirst: true) }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .tint(selectedType == type ? .accentColor : nil)
    }

    private var entityDetail: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if isLoading, detail == nil {
                    ProgressView("Loading entities...")
                } else if let detail {
                    detailHeader(detail.entity)
                    coMentions(detail.co_mentions)
                    factGroups(detail.facts_by_page)
                    mergeCandidates(detail.merge_candidates ?? [])
                } else {
                    Text("Select an entity.")
                        .foregroundStyle(.secondary)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func detailHeader(_ entity: EntitySummary) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(entity.name)
                .font(.title2.weight(.semibold))
                .textSelection(.enabled)
            Text(entity.id)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            MetadataRow(values: [
                entity.entity_type,
                entity.status,
                countText(entity.fact_count, "facts"),
                entity.last_observed_at,
            ])
            MetadataRow(values: entity.aliases ?? [])
        }
    }

    private func coMentions(_ items: [EntityCoMention]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Co-Mentions")
                .font(.title3.weight(.semibold))
            if items.isEmpty {
                Text("No co-mentions.")
                    .foregroundStyle(.secondary)
            } else {
                MetadataRow(values: items.prefix(20).map { "\($0.name) \($0.count)" })
            }
        }
    }

    private func factGroups(_ groups: [EntityFactGroup]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Facts")
                .font(.title3.weight(.semibold))
            if groups.isEmpty {
                Text("No active facts.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(groups) { group in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(group.page_hint)
                            .font(.headline)
                            .textSelection(.enabled)
                        ForEach(Array(group.facts.prefix(20).enumerated()), id: \.offset) { _index, fact in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(fact.statement ?? "Untitled fact")
                                    .textSelection(.enabled)
                                MetadataRow(values: [
                                    confidenceText(fact.displayConfidence),
                                    fact.section_hint,
                                    fact.displayID.map(shortID),
                                ])
                            }
                            .padding(.vertical, 5)
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func mergeCandidates(_ candidates: [JSONValue]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Merge Candidates")
                .font(.title3.weight(.semibold))
            if candidates.isEmpty {
                Text("No merge candidates.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(Array(candidates.prefix(8).enumerated()), id: \.offset) { _index, candidate in
                    Text(candidate.objectValue?["reason"]?.stringValue ?? "Candidate merge available.")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func loadIndex(selectFirst: Bool) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let result = try await client.entities(
                query: searchText,
                type: selectedType,
                includeInactive: includeInactive
            )
            response = result
            errorMessage = nil
            if selectFirst || selectedID == nil || !result.entities.contains(where: { $0.id == selectedID }) {
                selectedID = result.entities.first?.id
            }
            if let selectedID {
                await selectEntity(selectedID)
            } else {
                detail = nil
            }
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func selectEntity(_ id: String) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        selectedID = id
        do {
            detail = try await client.entityDetail(id)
            errorMessage = nil
        } catch {
            errorMessage = String(describing: error)
        }
    }
}
