import AppKit
import PKMBrainKit
import SwiftUI

struct QueueView: View {
    @EnvironmentObject private var appState: AppState
    @State private var page: QueuePage?
    @State private var selectedID: String?
    @State private var filter = "all"
    @State private var selectedIDs: Set<String> = []
    @State private var tally = QueueSessionTally()
    @State private var undoToast: QueueUndoToast?
    @State private var routePageHint = ""
    @State private var isLoading = false
    @State private var isLoadingMore = false
    @State private var errorMessage: String?
    private let queuePageLimit = 50

    private var items: [QueueItem] {
        page?.items ?? []
    }

    private var selectedItem: QueueItem? {
        if let selectedID, let item = items.first(where: { $0.id == selectedID }) {
            return item
        }
        return items.first
    }

    private var selectedIndex: Int {
        guard let selectedItem else {
            return 0
        }
        return items.firstIndex(where: { $0.id == selectedItem.id }) ?? 0
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                listPane
                    .frame(width: 340)
                Divider()
                detailPane
            }
        }
        .background(
            QueueKeyCaptureView { event in
                handleKey(event)
            }
            .frame(width: 0, height: 0)
        )
        .task {
            await loadQueue()
        }
        .toolbar {
            Button {
                Task { await loadQueue() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Queue")
                    .font(.largeTitle.weight(.semibold))
                Text(progressText)
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

    private var listPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            filterBar
            if !selectedIDs.isEmpty {
                batchBar
            }
            if let errorMessage {
                Text(errorMessage)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .lineLimit(3)
            }
            ScrollView {
                LazyVStack(spacing: 2) {
                    if page == nil, isLoading {
                        ProgressView("Loading review queue...")
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                    } else if items.isEmpty, !isLoading {
                        Text("Nothing needs you. Nightly runs will add items here.")
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                    } else {
                        ForEach(items) { item in
                            QueueRow(
                                item: item,
                                isSelected: item.id == selectedItem?.id,
                                isChecked: selectedIDs.contains(item.id)
                            ) {
                                toggleSelected(item.id)
                            }
                            .contentShape(Rectangle())
                            .onTapGesture {
                                selectedID = item.id
                            }
                        }
                        if page?.next_cursor != nil {
                            Button {
                                Task { await loadMoreQueueItems() }
                            } label: {
                                if isLoadingMore {
                                    Label("Loading More", systemImage: "arrow.clockwise")
                                } else {
                                    Label("Load More", systemImage: "plus.circle")
                                }
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .disabled(isLoadingMore)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                        }
                    }
                }
                .padding(.bottom, 16)
            }
        }
        .padding(14)
        .frame(maxHeight: .infinity, alignment: .topLeading)
    }

    private var filterBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(filterEntries, id: \.kind) { entry in
                    Button {
                        filter = entry.kind
                        selectedID = nil
                        selectedIDs.removeAll()
                        Task { await loadQueue() }
                    } label: {
                        Text("\(entry.label) \(entry.count)")
                            .font(.callout)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .tint(entry.kind == filter ? .accentColor : .secondary)
                }
            }
        }
    }

    private var batchBar: some View {
        HStack(spacing: 8) {
            Label("\(selectedIDs.count)", systemImage: "checkmark.square")
                .font(.callout.weight(.medium))
            Button {
                Task { await batchApproveSelected() }
            } label: {
                Label("Apply", systemImage: "checkmark.circle")
            }
            .keyboardShortcut("a", modifiers: [.shift])
            Button {
                Task { await batchRejectSelected() }
            } label: {
                Label("Reject", systemImage: "xmark.circle")
            }
            Button {
                selectedIDs.removeAll()
            } label: {
                Label("Clear", systemImage: "escape")
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var detailPane: some View {
        VStack(spacing: 0) {
            if let undoToast {
                UndoBanner(toast: undoToast) {
                    Task { await undoLast() }
                }
                Divider()
            }
            if let item = selectedItem {
                QueueDetail(
                    item: item,
                    position: "\(selectedIndex + 1) of \(items.count)",
                    tally: tally,
                    routePageHint: $routePageHint,
                    onDecision: { decision, payload in
                        Task { await decide(decision, payload: payload) }
                    }
                )
            } else if page == nil, isLoading {
                VStack(spacing: 10) {
                    ProgressView()
                    Text("Loading review queue...")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "tray")
                        .font(.system(size: 34))
                        .foregroundStyle(.secondary)
                    Text("Nothing needs you. Nightly runs will add items here.")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var filterEntries: [(kind: String, label: String, count: Int)] {
        let counts = page?.counts
        let byKind = counts?.by_kind ?? [:]
        let preferred = ["conflicts", "unrouted", "topology", "memories", "audit", "anomalies"]
        var entries: [(String, String, Int)] = [("all", "All", counts?.total ?? 0)]
        for kind in preferred where byKind[kind] != nil {
            entries.append((kind, label(for: kind), byKind[kind] ?? 0))
        }
        for kind in byKind.keys.sorted() where !preferred.contains(kind) {
            entries.append((kind, label(for: kind), byKind[kind] ?? 0))
        }
        return entries
    }

    private var progressText: String {
        let total = page?.total ?? 0
        guard !items.isEmpty else {
            return "\(total) items - resolved \(tally.resolved) - skipped \(tally.skipped)"
        }
        return "\(selectedIndex + 1) of \(items.count) shown - \(total) total - resolved \(tally.resolved) - skipped \(tally.skipped)"
    }

    private func label(for kind: String) -> String {
        kind.split(separator: "_").map { word in
            word.prefix(1).uppercased() + word.dropFirst()
        }.joined(separator: " ")
    }

    private func loadQueue() async {
        isLoading = true
        defer { isLoading = false }
        guard let client = await waitForQueueClient() else {
            errorMessage = "Queue is waiting for the daemon API. Use Refresh after the daemon is running."
            return
        }
        do {
            let latest = try await client.queue(kind: filter, limit: queuePageLimit)
            page = latest
            errorMessage = nil
            if selectedID == nil || !latest.items.contains(where: { $0.id == selectedID }) {
                selectedID = latest.items.first?.id
            }
            selectedIDs = selectedIDs.intersection(Set(latest.items.map(\.id)))
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func loadMoreQueueItems() async {
        guard let current = page, let cursor = current.next_cursor else {
            return
        }
        guard let client = await waitForQueueClient() else {
            errorMessage = "Queue is waiting for the daemon API. Use Refresh after the daemon is running."
            return
        }
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let next = try await client.queue(kind: filter, limit: queuePageLimit, cursor: cursor)
            let merged = current.items + next.items
            page = QueuePage(
                kind: next.kind,
                counts: next.counts,
                total: next.total,
                cursor: current.cursor,
                next_cursor: next.next_cursor,
                items: merged
            )
            errorMessage = nil
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func waitForQueueClient() async -> BrainAPIClient? {
        for _ in 0..<25 {
            if let client = appState.daemon.apiClient {
                return client
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        return nil
    }

    private func decide(_ decision: String, payload: [String: JSONValue] = [:]) async {
        guard let item = selectedItem else {
            return
        }
        if decision == "skip" {
            tally.skipped += 1
            advanceSelection(from: selectedIndex + 1, in: items)
            return
        }
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }

        let previousPage = page
        let previousSelection = selectedID
        let previousIndex = selectedIndex
        removeItem(item.id, nextIndex: previousIndex)
        do {
            let result = try await client.decideQueueItem(item.id, decision: decision, payload: payload)
            tally.resolved += 1
            if let handle = result.undo_handle, !handle.isNull {
                undoToast = QueueUndoToast(
                    title: item.displayTitle,
                    handle: handle,
                    expiresAt: Date().addingTimeInterval(6)
                )
            }
            await appState.refreshDigest()
        } catch {
            page = previousPage
            selectedID = previousSelection
            errorMessage = String(describing: error)
        }
    }

    private func undoLast() async {
        guard let toast = undoToast, Date() <= toast.expiresAt else {
            undoToast = nil
            return
        }
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        do {
            _ = try await client.undoQueueDecision(toast.handle)
            undoToast = nil
            tally.resolved = max(0, tally.resolved - 1)
            await loadQueue()
            await appState.refreshDigest()
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func batchApproveSelected() async {
        let ids = selectedIDs
        for id in ids {
            guard let item = page?.items.first(where: { $0.id == id }),
                  let decision = approveDecision(for: item)
            else {
                continue
            }
            selectedID = item.id
            await decide(decision)
        }
        selectedIDs.removeAll()
    }

    private func batchRejectSelected() async {
        let ids = selectedIDs
        for id in ids {
            guard let item = page?.items.first(where: { $0.id == id }),
                  let decision = rejectDecision(for: item)
            else {
                continue
            }
            selectedID = item.id
            await decide(decision)
        }
        selectedIDs.removeAll()
    }

    private func approveDecision(for item: QueueItem) -> String? {
        switch item.group {
        case "conflicts":
            return "candidate_wins"
        case "memories", "topology":
            return "approve"
        case "audit":
            return "mark_ok"
        default:
            return nil
        }
    }

    private func rejectDecision(for item: QueueItem) -> String? {
        item.group == "audit" ? "revert" : "reject"
    }

    private func removeItem(_ id: String, nextIndex: Int) {
        guard let current = page else {
            return
        }
        var updated = current.items
        updated.removeAll { $0.id == id }
        page = QueuePage(
            kind: current.kind,
            counts: current.counts,
            total: max(0, current.total - 1),
            cursor: current.cursor,
            next_cursor: current.next_cursor,
            items: updated
        )
        selectedIDs.remove(id)
        advanceSelection(from: nextIndex, in: updated)
    }

    private func advanceSelection(from index: Int, in currentItems: [QueueItem]) {
        guard !currentItems.isEmpty else {
            selectedID = nil
            return
        }
        selectedID = currentItems[min(index, currentItems.count - 1)].id
    }

    private func toggleSelected(_ id: String) {
        if selectedIDs.contains(id) {
            selectedIDs.remove(id)
        } else {
            selectedIDs.insert(id)
        }
    }

    private func handleKey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if flags.contains(.command) || flags.contains(.control) || flags.contains(.option) {
            return false
        }
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        let shift = flags.contains(.shift)
        switch (key, event.keyCode, shift) {
        case ("j", _, _), (_, 125, _):
            moveSelection(by: 1)
        case ("k", _, _), (_, 126, _):
            moveSelection(by: -1)
        case ("x", _, _):
            if let id = selectedItem?.id {
                toggleSelected(id)
            }
        case (_, 53, _):
            selectedIDs.removeAll()
        case ("a", _, true):
            Task { await batchApproveSelected() }
        case ("u", _, _) where selectedItem?.group != "conflicts":
            Task { await undoLast() }
        case ("1", _, _), ("2", _, _), ("3", _, _), ("4", _, _), ("5", _, _), ("6", _, _):
            handleNumberKey(key)
        case ("a", _, _), ("r", _, _), ("b", _, _), ("c", _, _), ("d", _, _), ("e", _, _), ("o", _, _), ("s", _, _), ("t", _, _), ("u", _, _), ("v", _, _):
            if let decision = decisionForKey(key, item: selectedItem) {
                Task { await decide(decision) }
            } else {
                return false
            }
        default:
            return false
        }
        return true
    }

    private func moveSelection(by offset: Int) {
        guard !items.isEmpty else {
            return
        }
        let next = min(max(selectedIndex + offset, 0), items.count - 1)
        selectedID = items[next].id
    }

    private func handleNumberKey(_ key: String) {
        guard let item = selectedItem else {
            return
        }
        if item.group == "conflicts" {
            return
        }
        guard let index = Int(key), let route = item.route_candidates?[safe: index - 1] else {
            return
        }
        Task {
            await decide("route", payload: ["page_hint": .string(route.page_hint)])
        }
    }

    private func decisionForKey(_ key: String, item: QueueItem?) -> String? {
        guard let item else {
            return nil
        }
        switch item.group {
        case "conflicts":
            return [
                "1": "keep_existing",
                "2": "candidate_wins",
                "3": "both_true",
                "4": "supports_existing",
                "5": "temporal_update",
                "6": "unsure",
            ][key]
        case "unrouted":
            return ["r": "reject", "e": "skip", "s": "skip"][key]
        case "memories":
            return ["a": "approve", "r": "reject", "d": "archive", "e": "skip", "s": "skip"][key]
        case "audit":
            return ["v": "revert", "o": "mark_ok", "e": "skip", "s": "skip"][key]
        default:
            return ["a": "approve", "r": "reject", "e": "skip", "s": "skip"][key]
        }
    }
}

private struct QueueDetail: View {
    let item: QueueItem
    let position: String
    let tally: QueueSessionTally
    @Binding var routePageHint: String
    let onDecision: (String, [String: JSONValue]) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(item.group) - \(shortID(item.id))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(item.displayTitle)
                            .font(.title2.weight(.semibold))
                            .textSelection(.enabled)
                    }
                    Spacer()
                    Text("\(position) - resolved \(tally.resolved) - skipped \(tally.skipped)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let orientation = item.orientation {
                    OrientationPanel(orientation: orientation)
                }
                card
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private var card: some View {
        switch item.group {
        case "conflicts":
            conflictCard
        case "unrouted":
            unroutedCard
        case "memories":
            memoryCard
        case "audit":
            auditCard
        case "topology":
            actionCard
        default:
            genericCard
        }
    }

    private var conflictCard: some View {
        let counterparts = item.counterparts ?? []
        return VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                FactPanel(title: "Candidate", fact: item.candidate)
                if counterparts.count <= 1 {
                    FactPanel(title: "Existing", fact: counterparts.first)
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(Array(counterparts.enumerated()), id: \.offset) { index, fact in
                            FactPanel(title: "Existing \(index + 1)", fact: fact)
                        }
                    }
                }
            }
            DecisionBar {
                DecisionButton("Keep Existing", systemImage: "checkmark.shield", key: "1") {
                    onDecision("keep_existing", [:])
                }
                DecisionButton("Candidate Wins", systemImage: "checkmark.circle", key: "2") {
                    onDecision("candidate_wins", [:])
                }
                DecisionButton("Both True", systemImage: "square.split.2x1", key: "3") {
                    onDecision("both_true", [:])
                }
                DecisionButton("Supports Existing", systemImage: "link", key: "4") {
                    onDecision("supports_existing", [:])
                }
                DecisionButton(
                    "Candidate Current",
                    systemImage: "clock",
                    key: "5",
                    help: "Candidate is the current state; existing fact becomes historical."
                ) {
                    onDecision("temporal_update", [:])
                }
                DecisionButton("Unsure", systemImage: "questionmark.circle", key: "6") {
                    onDecision("unsure", [:])
                }
            }
        }
    }

    private var unroutedCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            FactPanel(title: "Fact", fact: item.candidate)
            VStack(alignment: .leading, spacing: 8) {
                Text("Route Candidates")
                    .font(.headline)
                ForEach(Array((item.route_candidates ?? []).enumerated()), id: \.element.id) { index, route in
                    Button {
                        onDecision("route", ["page_hint": .string(route.page_hint)])
                    } label: {
                        HStack {
                            Image(systemName: "\(index + 1).circle")
                            Text(route.title ?? route.page_hint)
                            Spacer()
                            Text(route.page_hint)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.bordered)
                }
                HStack(spacing: 8) {
                    TextField("concepts/topic.md", text: $routePageHint)
                        .textFieldStyle(.roundedBorder)
                    Button {
                        let pageHint = routePageHint.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !pageHint.isEmpty else {
                            return
                        }
                        onDecision("route", ["page_hint": .string(pageHint)])
                        routePageHint = ""
                    } label: {
                        Label("Route", systemImage: "arrow.turn.down.right")
                    }
                }
            }
            DecisionBar {
                DecisionButton("Reject", systemImage: "xmark.circle", key: "r") {
                    onDecision("reject", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "s") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var memoryCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(item.memory?.content ?? item.summary ?? "")
                .font(.body)
                .textSelection(.enabled)
            MetadataRow(values: [
                item.memory?.memory_type,
                item.memory?.scope,
                confidenceText(item.memory?.confidence),
                item.memory?.status,
            ])
            SourceDocumentsView(documents: item.memory?.source_documents ?? [])
            DecisionBar {
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "a") {
                    onDecision("approve", [:])
                }
                DecisionButton("Reject", systemImage: "xmark.circle", key: "r") {
                    onDecision("reject", [:])
                }
                DecisionButton("Archive", systemImage: "archivebox", key: "d") {
                    onDecision("archive", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "s") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var auditCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(actionType)
                .font(.headline)
            MetadataRow(values: [item.status, item.risk_tier, actionString("audit_status")])
            EvidenceQuote(text: item.summary)
            DecisionBar {
                DecisionButton("Revert", systemImage: "arrow.uturn.backward.circle", key: "v") {
                    onDecision("revert", [:])
                }
                DecisionButton("Mark OK", systemImage: "checkmark.seal", key: "o") {
                    onDecision("mark_ok", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "s") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var actionCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(actionType)
                .font(.headline)
            if let topology = item.topology {
                TopologyTargetPanel(topology: topology)
            }
            MetadataRow(values: [item.status, item.risk_tier, actionString("proposed_by")])
            EvidenceQuote(text: item.summary)
            DecisionBar {
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "a") {
                    onDecision("approve", [:])
                }
                DecisionButton("Reject", systemImage: "xmark.circle", key: "r") {
                    onDecision("reject", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "s") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var genericCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(item.summary ?? item.displayTitle)
                .textSelection(.enabled)
            DecisionBar {
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "a") {
                    onDecision("approve", [:])
                }
                DecisionButton("Reject", systemImage: "xmark.circle", key: "r") {
                    onDecision("reject", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "s") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var actionType: String {
        actionString("action_type") ?? item.kind
    }

    private func actionString(_ key: String) -> String? {
        item.action?[key]?.stringValue
    }
}

private struct TopologyTargetPanel: View {
    let topology: QueueTopology

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("Target")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(title)
                .font(.callout.weight(.semibold))
                .textSelection(.enabled)
            MetadataRow(values: metadata)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var title: String {
        let labels = (topology.entity_labels ?? []).filter { !$0.isEmpty }
        if let target = topology.target_label, !target.isEmpty {
            return target
        }
        if !labels.isEmpty {
            return labels.joined(separator: ", ")
        }
        return "Topology target"
    }

    private var metadata: [String?] {
        let ids = (topology.entity_ids ?? []).filter { !$0.isEmpty }
        let pages = (topology.page_hints ?? []).filter { !$0.isEmpty }
        return [
            ids.isEmpty ? nil : "ids \(ids.joined(separator: ", "))",
            pages.isEmpty ? nil : "pages \(pages.joined(separator: ", "))",
        ]
    }
}

private struct QueueRow: View {
    let item: QueueItem
    let isSelected: Bool
    let isChecked: Bool
    let onCheck: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Button(action: onCheck) {
                Image(systemName: isChecked ? "checkmark.square.fill" : "square")
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.plain)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayTitle)
                    .font(.callout.weight(isSelected ? .semibold : .regular))
                    .lineLimit(2)
                Text("\(item.group) - \(item.kind) - \(item.created_at ?? "")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
        .background(isSelected ? Color.accentColor.opacity(0.13) : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct OrientationPanel: View {
    let orientation: QueueOrientation

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("Mapped to")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(orientation.entity_label ?? orientation.title ?? "Review Target")
                .font(.headline)
                .textSelection(.enabled)
            MetadataRow(values: [
                orientation.page_hint,
                orientation.section_hint,
                relationText,
                orientation.temporal_scope,
                labeledTime("candidate", orientation.candidate_observed_at),
                labeledTime("existing", orientation.existing_observed_at),
                orientation.currentness,
            ])
            EvidenceQuote(text: orientation.relation_rationale)
        }
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) {
            Divider()
        }
        .overlay(alignment: .bottom) {
            Divider()
        }
    }

    private var relationText: String? {
        guard let relation = orientation.relation, !relation.isEmpty else {
            return nil
        }
        if let confidence = orientation.relation_confidence {
            return "relation \(relation) \(String(format: "%.2f", confidence))"
        }
        return "relation \(relation)"
    }
}

private struct FactPanel: View {
    let title: String
    let fact: QueueFact?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.headline)
            Text(fact?.statement ?? "No fact payload.")
                .textSelection(.enabled)
            EvidenceQuote(text: fact?.displayQuote)
            MetadataRow(values: [
                confidenceText(fact?.displayConfidence),
                fact?.page_hint,
                fact?.entity_key,
                fact?.displayID.map(shortID),
            ])
            SourceDocumentsView(documents: fact?.source_documents ?? [])
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct EvidenceQuote: View {
    let text: String?

    var body: some View {
        if let text, !text.isEmpty {
            Text(text)
                .font(.system(.callout, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.secondary.opacity(0.07))
                .clipShape(RoundedRectangle(cornerRadius: 6))
        }
    }
}

private struct MetadataRow: View {
    let values: [String?]

    var body: some View {
        FlowLayout(spacing: 6) {
            ForEach(values.compactMap { value in
                let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                return trimmed.isEmpty ? nil : trimmed
            }, id: \.self) { value in
                Text(value)
                    .font(.caption)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            }
        }
    }
}

private struct SourceDocumentsView: View {
    let documents: [QueueSourceDocument]

    var body: some View {
        if !documents.isEmpty {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(documents.prefix(4)) { document in
                    HStack(spacing: 6) {
                        Image(systemName: "doc.text")
                            .foregroundStyle(.secondary)
                        Text(document.title ?? document.source_id)
                            .lineLimit(1)
                        Spacer()
                        Text(shortID(document.source_id))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                    }
                    .font(.caption)
                }
            }
        }
    }
}

private struct DecisionBar<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        FlowLayout(spacing: 8) {
            content
        }
        .buttonStyle(.bordered)
    }
}

private struct DecisionButton: View {
    let title: String
    let systemImage: String
    let key: String
    let help: String?
    let action: () -> Void

    init(_ title: String, systemImage: String, key: String, help: String? = nil, action: @escaping () -> Void) {
        self.title = title
        self.systemImage = systemImage
        self.key = key
        self.help = help
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            Label {
                HStack(spacing: 7) {
                    Text(title)
                    Text(key.uppercased())
                        .font(.caption2.monospaced().weight(.bold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
                        .accessibilityHidden(true)
                }
            } icon: {
                Image(systemName: systemImage)
            }
        }
        .keyboardShortcut(KeyEquivalent(Character(key)), modifiers: [])
        .help(help ?? "\(key.uppercased()) - \(title)")
        .accessibilityLabel("\(title), keyboard shortcut \(key.uppercased())")
    }
}

private struct UndoBanner: View {
    let toast: QueueUndoToast
    let onUndo: () -> Void

    var body: some View {
        TimelineView(.periodic(from: Date(), by: 1)) { context in
            let remaining = max(0, Int(ceil(toast.expiresAt.timeIntervalSince(context.date))))
            HStack(spacing: 10) {
                Image(systemName: "arrow.uturn.backward.circle")
                Text("\(toast.title) resolved")
                    .lineLimit(1)
                Spacer()
                Text("\(remaining)s")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Button("Undo", action: onUndo)
                    .disabled(remaining <= 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
        .background(Color.accentColor.opacity(0.10))
    }
}

private struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    init(spacing: CGFloat = 8) {
        self.spacing = spacing
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = rows(for: subviews, proposal: proposal)
        return CGSize(
            width: proposal.width ?? rows.map(\.width).max() ?? 0,
            height: rows.last.map { $0.y + $0.height } ?? 0
        )
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for row in rows(for: subviews, proposal: ProposedViewSize(width: bounds.width, height: proposal.height)) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(
                    at: CGPoint(x: x, y: bounds.minY + row.y),
                    proposal: ProposedViewSize(size)
                )
                x += size.width + spacing
            }
        }
    }

    private func rows(for subviews: Subviews, proposal: ProposedViewSize) -> [FlowRow] {
        let maxWidth = proposal.width ?? .infinity
        var rows: [FlowRow] = []
        var current = FlowRow(indices: [], width: 0, height: 0, y: 0)
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            let proposedWidth = current.indices.isEmpty ? size.width : current.width + spacing + size.width
            if proposedWidth > maxWidth, !current.indices.isEmpty {
                rows.append(current)
                let y = current.y + current.height + spacing
                current = FlowRow(indices: [index], width: size.width, height: size.height, y: y)
            } else {
                current.indices.append(index)
                current.width = proposedWidth
                current.height = max(current.height, size.height)
            }
        }
        if !current.indices.isEmpty {
            rows.append(current)
        }
        return rows
    }
}

private struct FlowRow {
    var indices: [Int]
    var width: CGFloat
    var height: CGFloat
    var y: CGFloat
}

private struct QueueKeyCaptureView: NSViewRepresentable {
    let onKey: (NSEvent) -> Bool

    func makeNSView(context: Context) -> KeyView {
        let view = KeyView()
        view.onKey = onKey
        return view
    }

    func updateNSView(_ nsView: KeyView, context: Context) {
        nsView.onKey = onKey
        DispatchQueue.main.async {
            guard let window = nsView.window else {
                return
            }
            if window.firstResponder == nil || window.firstResponder === window.contentView {
                window.makeFirstResponder(nsView)
            }
        }
    }

    final class KeyView: NSView {
        var onKey: ((NSEvent) -> Bool)?

        override var acceptsFirstResponder: Bool {
            true
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            DispatchQueue.main.async {
                self.window?.makeFirstResponder(self)
            }
        }

        override func keyDown(with event: NSEvent) {
            if onKey?(event) == true {
                return
            }
            super.keyDown(with: event)
        }
    }
}

private struct QueueSessionTally: Equatable {
    var resolved = 0
    var skipped = 0
}

private struct QueueUndoToast: Equatable {
    let title: String
    let handle: JSONValue
    let expiresAt: Date
}

private func shortID(_ value: String) -> String {
    String(value.prefix(10))
}

private func confidenceText(_ value: Double?) -> String? {
    guard let value else {
        return nil
    }
    return "conf \(String(format: "%.2f", value))"
}

private func labeledTime(_ label: String, _ value: String?) -> String? {
    guard let value, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        return nil
    }
    return "\(label) \(shortDate(value))"
}

private func shortDate(_ value: String) -> String {
    String(value.prefix(10))
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
