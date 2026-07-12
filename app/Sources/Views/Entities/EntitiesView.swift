import PKMBrainKit
import SwiftUI

struct EntitiesView: View {
    @EnvironmentObject private var appState: AppState
    @State private var response: EntitiesResponse?
    @State private var selectedID: String?
    @State private var detail: EntityDetail?
    @State private var searchText = ""
    @State private var selectedType = ""
    @State private var entitySort = "retrieval"
    @State private var factSort = "retrieval"
    @State private var includeInactive = false
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var proposingCandidateID: String?
    @State private var proposalStatus: String?

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
        .task(id: appState.daemon.handshake?.pid) {
            await loadIndex(selectFirst: appState.requestedEntityID == nil)
            if let requested = appState.requestedEntityID {
                await selectEntity(requested)
                appState.requestedEntityID = nil
            }
        }
        .onChange(of: appState.requestedEntityID) { _, requested in
            guard let requested else {
                return
            }
            Task {
                await selectEntity(requested)
                appState.requestedEntityID = nil
            }
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
                Picker("Sort entities", selection: $entitySort) {
                    Label("Most Retrieved", systemImage: "chart.bar.xaxis").tag("retrieval")
                    Label("Most Facts", systemImage: "number").tag("facts")
                    Label("Recently Observed", systemImage: "clock").tag("recent")
                    Label("Name", systemImage: "textformat").tag("name")
                }
                .pickerStyle(.menu)
                .onChange(of: entitySort) { _, _ in
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
                                RetrievalBadge(
                                    count: entity.retrieval_count,
                                    lastRetrievedAt: entity.last_retrieved_at,
                                    compact: true
                                )
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
            RetrievalBadge(
                count: entity.retrieval_count,
                lastRetrievedAt: entity.last_retrieved_at
            )
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
                ForEach(items.prefix(20)) { item in
                    Button {
                        Task { await selectEntity(item.id) }
                    } label: {
                        HStack {
                            Text(item.name)
                            Spacer()
                            Text("\(item.count)")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func factGroups(_ groups: [EntityFactGroup]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Facts")
                    .font(.title3.weight(.semibold))
                Spacer()
                Text("Sort facts")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Picker("Sort facts", selection: $factSort) {
                    Label("Most Retrieved", systemImage: "chart.bar.xaxis").tag("retrieval")
                    Label("Confidence", systemImage: "gauge").tag("confidence")
                    Label("Recent", systemImage: "clock").tag("recent")
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .frame(width: 155)
            }
            if groups.isEmpty {
                Text("No active facts.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(sortedFactGroups(groups)) { group in
                    VStack(alignment: .leading, spacing: 8) {
                        Button {
                            appState.showWiki(path: group.page_hint)
                        } label: {
                            Label(group.page_hint, systemImage: "doc.text")
                                .font(.headline)
                        }
                        .buttonStyle(.plain)
                        ForEach(Array(sortedFacts(group.facts).prefix(20).enumerated()), id: \.offset) { _index, fact in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(fact.statement ?? "Untitled fact")
                                    .textSelection(.enabled)
                                MetadataRow(values: [
                                    fact.section_hint,
                                    fact.displayID.map(shortID),
                                ])
                                HStack(spacing: 8) {
                                    ConfidenceBadge(value: fact.displayConfidence)
                                    RetrievalBadge(
                                        count: fact.retrieval_count,
                                        lastRetrievedAt: fact.last_retrieved_at
                                    )
                                }
                            }
                            .padding(.vertical, 5)
                            Divider()
                        }
                    }
                }
            }
        }
    }

    private func mergeCandidates(_ candidates: [EntityMergeCandidate]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Merge Candidates")
                .font(.title3.weight(.semibold))
            if let proposalStatus {
                Text(proposalStatus)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            if candidates.isEmpty {
                Text("No merge candidates.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(candidates.prefix(8)) { candidate in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(mergeDirection(candidate))
                            .font(.callout.weight(.semibold))
                            .fixedSize(horizontal: false, vertical: true)
                        MetadataRow(values: [
                            candidate.risk_tier.map { "\($0) risk" },
                            candidate.affected_fact_count.map { "\($0) facts" },
                            candidate.score.map { "score \(Int(($0 * 100).rounded()))%" },
                            candidate.merge_signal,
                        ])
                        if let reason = candidate.reason, !reason.isEmpty {
                            Text(reason)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        Button {
                            Task { await proposeMerge(candidate) }
                        } label: {
                            Label("Propose Merge", systemImage: "arrow.triangle.merge")
                        }
                        .disabled(
                            proposingCandidateID != nil
                                || candidate.canonicalID == nil
                                || candidate.sourceIDs.isEmpty
                        )
                        if proposingCandidateID == candidate.id {
                            ProgressView()
                                .controlSize(.small)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.secondary.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
            }
        }
    }

    private func loadIndex(selectFirst: Bool) async {
        isLoading = true
        defer { isLoading = false }
        guard let client = await appState.waitForAPIClient() else {
            if !Task.isCancelled {
                errorMessage = "Daemon API is unavailable."
            }
            return
        }
        do {
            let result = try await client.entities(
                query: searchText,
                type: selectedType,
                includeInactive: includeInactive,
                sort: entitySort
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
            errorMessage = error.localizedDescription
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
            errorMessage = error.localizedDescription
        }
    }

    private func mergeDirection(_ candidate: EntityMergeCandidate) -> String {
        guard let canonicalID = candidate.canonicalID else {
            return "Merge direction unavailable"
        }
        let destination = candidate.name(for: canonicalID)
        let sources = candidate.sourceIDs.map { candidate.name(for: $0) }.joined(separator: ", ")
        return "Merge \(sources) into \(destination)"
    }

    private func proposeMerge(_ candidate: EntityMergeCandidate) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        proposingCandidateID = candidate.id
        defer { proposingCandidateID = nil }
        do {
            let response = try await client.proposeEntityMerge(candidate)
            let status = response.action["status"]?.stringValue ?? "proposed"
            proposalStatus = status == "applied"
                ? "Merge applied under the active policy."
                : "Merge sent to the review queue (\(status))."
            await appState.refreshDigest()
            if let selectedID {
                detail = try await client.entityDetail(selectedID)
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func sortedFactGroups(_ groups: [EntityFactGroup]) -> [EntityFactGroup] {
        groups.sorted { left, right in
            let leftValue = groupSortValue(left)
            let rightValue = groupSortValue(right)
            if leftValue == rightValue {
                return left.page_hint.localizedCaseInsensitiveCompare(right.page_hint) == .orderedAscending
            }
            return leftValue > rightValue
        }
    }

    private func sortedFacts(_ facts: [QueueFact]) -> [QueueFact] {
        facts.sorted { left, right in
            let leftValue = factSortValue(left)
            let rightValue = factSortValue(right)
            if leftValue == rightValue {
                return (left.statement ?? "").localizedCaseInsensitiveCompare(right.statement ?? "") == .orderedAscending
            }
            return leftValue > rightValue
        }
    }

    private func groupSortValue(_ group: EntityFactGroup) -> String {
        group.facts.map(factSortValue).max() ?? ""
    }

    private func factSortValue(_ fact: QueueFact) -> String {
        switch factSort {
        case "confidence":
            return String(format: "%020.8f", fact.displayConfidence ?? -1)
        case "recent":
            return fact.observed_at ?? fact.last_retrieved_at ?? ""
        default:
            return String(format: "%020d", fact.retrieval_count ?? 0)
        }
    }
}
