import AppKit
import PKMBrainKit
import SwiftUI

struct QueueView: View {
    @EnvironmentObject private var appState: AppState
    @State private var page: QueuePage?
    @State private var selectedID: String?
    @State private var filter = "all"
    @State private var reviewState = "actionable"
    @State private var sortMode = "retrieval"
    @State private var selectedIDs: Set<String> = []
    @State private var alternativeSelections: [String: Set<String>] = [:]
    @State private var tally = QueueSessionTally()
    @State private var undoToast: QueueUndoToast?
    @State private var routePageHint = ""
    @State private var isLoading = false
    @State private var isLoadingMore = false
    @State private var errorMessage: String?
    @State private var activeLoadID: UUID?
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
            ZStack {
                HStack(spacing: 0) {
                    listPane
                        .frame(width: 340)
                    Divider()
                    detailPane
                }
                .disabled(isLoading)
                if isLoading {
                    queueLoadingState
                }
            }
        }
        .background(
            QueueKeyCaptureView { event in
                handleKey(event)
            }
            .frame(width: 0, height: 0)
        )
        .task(id: appState.daemon.handshake?.pid) {
            if let requested = appState.requestedQueueState {
                reviewState = requested
                appState.requestedQueueState = nil
            }
            await loadQueue()
        }
        .onChange(of: appState.requestedQueueState) { _, requested in
            guard let requested else {
                return
            }
            reviewState = requested
            appState.requestedQueueState = nil
        }
        .toolbar {
            Button {
                startQueueLoad()
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
            Picker("Queue State", selection: $reviewState) {
                Text("Review \(page?.queue_summary?.actionable_total ?? 0)")
                    .tag("actionable")
                Text("Needs Repair \(page?.queue_summary?.blocked_total ?? 0)")
                    .tag("blocked")
                Text("Deferred \(page?.queue_summary?.deferred_total ?? 0)")
                    .tag("deferred")
            }
            .pickerStyle(.segmented)
            .frame(width: 360)
            .onChange(of: reviewState) { _, _ in
                filter = "all"
                selectedID = nil
                selectedIDs.removeAll()
                startQueueLoad()
            }
            Text("Sort")
                .font(.callout)
                .foregroundStyle(.secondary)
            Picker("Sort", selection: $sortMode) {
                Label("Most Retrieved", systemImage: "chart.bar.xaxis").tag("retrieval")
                Label("Review Priority", systemImage: "exclamationmark.triangle").tag("priority")
                Label("Newest", systemImage: "clock").tag("newest")
            }
            .pickerStyle(.menu)
            .labelsHidden()
            .frame(width: 155)
            .onChange(of: sortMode) { _, _ in
                selectedID = nil
                selectedIDs.removeAll()
                startQueueLoad()
            }
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
                        startQueueLoad()
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
                    position: "\(selectedIndex + 1) of \(items.count) loaded - \(page?.total ?? items.count) total",
                    tally: tally,
                    routePageHint: $routePageHint,
                    selectedAlternativeIDs: alternativeSelectionBinding(for: item.id),
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
        let preferred = ["conflicts", "unrouted", "policy", "topology", "memories", "audit", "anomalies"]
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
        let actionable = page?.queue_summary?.actionable_total ?? total
        let blocked = page?.queue_summary?.blocked_total ?? 0
        let deferred = page?.queue_summary?.deferred_total ?? 0
        let workload: String
        switch reviewState {
        case "blocked":
            workload = "\(total) needs repair - \(actionable) actionable"
        case "deferred":
            workload = "\(total) deferred - \(actionable) actionable"
        default:
            workload = "\(total) to review - \(blocked) needs repair - \(deferred) deferred"
        }
        guard !items.isEmpty else {
            return "\(workload) - resolved \(tally.resolved) - skipped \(tally.skipped)"
        }
        return "\(selectedIndex + 1) of \(items.count) shown - \(workload) - resolved \(tally.resolved) - skipped \(tally.skipped)"
    }

    private var queueLoadingState: some View {
        VStack(spacing: 10) {
            ProgressView()
                .controlSize(.regular)
            Text("Loading \(label(for: filter))...")
                .font(.callout.weight(.medium))
            Text("Updating the review list and evidence.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Loading \(label(for: filter)) review items")
    }

    private func label(for kind: String) -> String {
        let labels = [
            "conflicts": "Conflicts",
            "unrouted": "Inbox",
            "topology": "Topology",
            "memories": "Memories",
            "audit": "Audit",
            "anomalies": "Anomalies",
            "policy_escalation": "Policy",
            "policy": "Policy",
        ]
        if let label = labels[kind] {
            return label
        }
        return kind.split(separator: "_").map { word in
            word.prefix(1).uppercased() + word.dropFirst()
        }.joined(separator: " ")
    }

    private func startQueueLoad() {
        let request = beginQueueLoad()
        Task { await performQueueLoad(request) }
    }

    private func loadQueue() async {
        await performQueueLoad(beginQueueLoad())
    }

    private func beginQueueLoad() -> QueueLoadRequest {
        let request = QueueLoadRequest(
            id: UUID(),
            kind: filter,
            state: reviewState,
            sort: sortMode
        )
        activeLoadID = request.id
        isLoading = true
        errorMessage = nil
        return request
    }

    private func performQueueLoad(_ request: QueueLoadRequest) async {
        defer {
            if activeLoadID == request.id {
                isLoading = false
            }
        }
        guard let client = await appState.waitForAPIClient() else {
            if !Task.isCancelled, activeLoadID == request.id {
                errorMessage = "Queue is waiting for the daemon API. Use Refresh after the daemon is running."
            }
            return
        }
        do {
            let latest = try await client.queue(
                kind: request.kind,
                state: request.state,
                sort: request.sort,
                limit: queuePageLimit
            )
            guard activeLoadID == request.id, !Task.isCancelled else {
                return
            }
            page = latest
            appState.acceptQueueSummary(latest.queue_summary)
            errorMessage = nil
            if selectedID == nil || !latest.items.contains(where: { $0.id == selectedID }) {
                selectedID = latest.items.first?.id
            }
            selectedIDs = selectedIDs.intersection(Set(latest.items.map(\.id)))
            let currentIDs = Set(latest.items.map(\.id))
            alternativeSelections = alternativeSelections.filter {
                currentIDs.contains($0.key)
            }
        } catch {
            if activeLoadID == request.id, !Task.isCancelled {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func loadMoreQueueItems() async {
        guard let current = page, let cursor = current.next_cursor else {
            return
        }
        guard let client = await appState.waitForAPIClient() else {
            if !Task.isCancelled {
                errorMessage = "Queue is waiting for the daemon API. Use Refresh after the daemon is running."
            }
            return
        }
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let next = try await client.queue(
                kind: filter,
                state: reviewState,
                sort: sortMode,
                limit: queuePageLimit,
                cursor: cursor
            )
            let merged = current.items + next.items
            page = QueuePage(
                kind: next.kind,
                state: next.state,
                sort: next.sort,
                queue_summary: next.queue_summary,
                counts: next.counts,
                total: next.total,
                cursor: current.cursor,
                next_cursor: next.next_cursor,
                items: merged
            )
            appState.acceptQueueSummary(next.queue_summary)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
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
        guard item.isApprovable else {
            errorMessage = item.blocking_reason ?? "This review card is incomplete."
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
            appState.acceptQueueSummary(result.queue_summary)
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
            errorMessage = error.localizedDescription
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
            let result = try await client.undoQueueDecision(toast.handle)
            appState.acceptQueueSummary(result.queue_summary)
            undoToast = nil
            tally.resolved = max(0, tally.resolved - 1)
            await loadQueue()
            await appState.refreshDigest()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func batchApproveSelected() async {
        let ids = selectedIDs
        for id in ids {
            guard let item = page?.items.first(where: { $0.id == id }),
                  item.isApprovable,
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
                  item.isApprovable,
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
        guard item.isApprovable, !item.isAlternativeComparison else {
            return nil
        }
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
        guard item.isApprovable, !item.isAlternativeComparison else {
            return nil
        }
        return item.group == "audit" ? "revert" : "reject"
    }

    private func removeItem(_ id: String, nextIndex: Int) {
        guard let current = page else {
            return
        }
        var updated = current.items
        updated.removeAll { $0.id == id }
        page = QueuePage(
            kind: current.kind,
            state: current.state,
            sort: current.sort,
            queue_summary: current.queue_summary,
            counts: current.counts,
            total: max(0, current.total - 1),
            cursor: current.cursor,
            next_cursor: current.next_cursor,
            items: updated
        )
        selectedIDs.remove(id)
        alternativeSelections.removeValue(forKey: id)
        advanceSelection(from: nextIndex, in: updated)
    }

    private func alternativeSelectionBinding(for itemID: String) -> Binding<Set<String>> {
        Binding(
            get: { alternativeSelections[itemID] ?? [] },
            set: { alternativeSelections[itemID] = $0 }
        )
    }

    private func submitAlternativeSelection() {
        guard let item = selectedItem, item.isAlternativeComparison else {
            return
        }
        let selected = alternativeSelections[item.id] ?? []
        let orderedIDs = (item.alternatives ?? []).compactMap(\.displayID).filter {
            selected.contains($0)
        }
        guard !orderedIDs.isEmpty else {
            return
        }
        Task {
            await decide(
                "select_facts",
                payload: [
                    "selected_fact_ids": .array(orderedIDs.map(JSONValue.string))
                ]
            )
        }
    }

    private func advanceSelection(from index: Int, in currentItems: [QueueItem]) {
        guard !currentItems.isEmpty else {
            selectedID = nil
            return
        }
        selectedID = currentItems[min(index, currentItems.count - 1)].id
    }

    private func toggleSelected(_ id: String) {
        guard let item = page?.items.first(where: { $0.id == id }),
              item.isApprovable,
              !item.isAlternativeComparison
        else {
            return
        }
        if selectedIDs.contains(id) {
            selectedIDs.remove(id)
        } else {
            selectedIDs.insert(id)
        }
    }

    private func handleKey(_ event: NSEvent) -> Bool {
        guard !isLoading else {
            return true
        }
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
        case (_, 36, _) where selectedItem?.isAlternativeComparison == true:
            submitAlternativeSelection()
        case ("1", _, _), ("2", _, _), ("3", _, _), ("4", _, _), ("5", _, _), ("6", _, _), ("7", _, _), ("8", _, _), ("9", _, _):
            handleNumberKey(key)
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
        if item.isAlternativeComparison {
            let alternatives = item.alternatives ?? []
            if let index = Int(key),
               index <= min(alternatives.count, 7),
               let factID = alternatives[index - 1].displayID
            {
                var selected = alternativeSelections[item.id] ?? []
                if selected.contains(factID) {
                    selected.remove(factID)
                } else {
                    selected.insert(factID)
                }
                alternativeSelections[item.id] = selected
                return
            }
            let selectAllKey = String(min(alternatives.count, 7) + 1)
            let unsureKey = String(min(alternatives.count, 7) + 2)
            if key == selectAllKey {
                alternativeSelections[item.id] = Set(
                    alternatives.compactMap(\.displayID)
                )
            } else if key == unsureKey {
                Task { await decide("unsure") }
            }
            return
        }
        if item.group == "unrouted" {
            let routes = item.route_candidates ?? []
            if let index = Int(key), let route = routes[safe: index - 1] {
                Task {
                    await decide("route", payload: ["page_hint": .string(route.page_hint)])
                }
                return
            }
            let manualRouteKey = String(routes.count + 1)
            if key == manualRouteKey {
                let pageHint = routePageHint.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !pageHint.isEmpty else {
                    return
                }
                Task {
                    await decide("route", payload: ["page_hint": .string(pageHint)])
                    routePageHint = ""
                }
                return
            }
        }
        guard let decision = decisionForKey(key, item: item) else {
            return
        }
        Task { await decide(decision) }
    }

    private func decisionForKey(_ key: String, item: QueueItem?) -> String? {
        guard let item else {
            return nil
        }
        guard item.isApprovable else {
            return nil
        }
        if key == "1", item.topology?.split_preview?.approvable == false {
            return nil
        }
        if item.kind == "document_extraction_anomaly" {
            return ["1": "acknowledge", "2": "dismiss", "3": "skip"][key]
        }
        switch item.group {
        case "conflicts":
            guard !item.isAlternativeComparison else {
                return nil
            }
            return [
                "1": "keep_existing",
                "2": "candidate_wins",
                "3": "both_true",
                "4": "supports_existing",
                "5": "temporal_update",
                "6": "unsure",
            ][key]
        case "unrouted":
            let routeCount = item.route_candidates?.count ?? 0
            return [
                String(routeCount + 2): "reject",
                String(routeCount + 3): "skip",
            ][key]
        case "memories":
            return ["1": "approve", "2": "reject", "3": "archive", "4": "skip"][key]
        case "audit":
            return ["1": "revert", "2": "mark_ok", "3": "skip"][key]
        default:
            return ["1": "approve", "2": "reject", "3": "skip"][key]
        }
    }
}

private struct QueueDetail: View {
    let item: QueueItem
    let position: String
    let tally: QueueSessionTally
    @Binding var routePageHint: String
    @Binding var selectedAlternativeIDs: Set<String>
    @State private var routeFieldFocused = false
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
                HStack(spacing: 8) {
                    RetrievalBadge(
                        count: item.popularity?.retrieval_count,
                        lastRetrievedAt: item.popularity?.last_retrieved_at
                    )
                    ConfidenceBadge(value: item.primaryConfidence)
                }
                if let orientation = item.orientation {
                    OrientationPanel(orientation: orientation)
                }
                if !item.isApprovable {
                    BlockedReviewNotice(reason: item.blocking_reason)
                }
                card
                    .disabled(!item.isApprovable)
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private var card: some View {
        if item.kind == "policy_escalation" {
            policyCard
        } else if item.kind == "unrouted_inbox_batch" {
            inboxBatchCard
        } else if item.kind == "document_extraction_anomaly" {
            anomalyCard
        } else {
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
    }

    private var policyCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            MetadataRow(values: [humanized(actionType), item.risk_tier, item.orientation?.relation])
            EvidenceQuote(text: item.summary)
            if let topology = item.topology {
                TopologyTargetPanel(topology: topology)
                if let preview = topology.split_preview {
                    SplitPreviewPanel(preview: preview)
                }
            }
            if item.candidate != nil {
                FactPanel(title: "Candidate", fact: item.candidate)
            }
            if let counterparts = item.counterparts, !counterparts.isEmpty {
                Text("Existing Context")
                    .font(.headline)
                ForEach(Array(counterparts.enumerated()), id: \.offset) { index, fact in
                    FactPanel(title: "Existing \(index + 1)", fact: fact)
                }
            }
            DecisionBar {
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "1") {
                    onDecision("approve", [:])
                }
                .disabled(item.topology?.split_preview?.approvable == false)
                DecisionButton("Reject", systemImage: "xmark.circle", key: "2") {
                    onDecision("reject", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "3") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var inboxBatchCard: some View {
        let context = item.question?["context"]?.objectValue
        let count = context?["source_question_ids"]?.arrayValue?.count ?? 0
        return VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Inbox Batch")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(item.page_hint ?? context?["page_hint"]?.stringValue ?? "Inbox")
                    .font(.headline)
                MetadataRow(values: ["\(count) facts", context?["section"]?.stringValue ?? "Inbox"])
            }
            Text(item.summary ?? "")
                .textSelection(.enabled)
            DecisionBar {
                DecisionButton("Mark Reviewed", systemImage: "checkmark.circle", key: "1") {
                    onDecision("reviewed", [:])
                }
                DecisionButton("Dismiss", systemImage: "xmark.circle", key: "2") {
                    onDecision("dismiss", [:])
                }
                DecisionButton("Later", systemImage: "arrowshape.turn.up.right", key: "3") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var anomalyCard: some View {
        let anomaly = item.anomaly
        let rate = anomaly?.block_rate.map {
            "\(Int(($0 * 100).rounded()))% blocked"
        }
        return VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Source Document")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(anomaly?.document_title ?? item.displayTitle)
                    .font(.headline)
                    .textSelection(.enabled)
            }
            MetadataRow(values: [
                rate,
                anomaly.map { "\($0.blocked_count) blocked" },
                anomaly.map { "\($0.reviewed_count) reviewed" },
            ])
            EvidenceQuote(text: item.summary)
            DecisionBar {
                DecisionButton(
                    "Confirm Quality Issue",
                    systemImage: "checkmark.circle",
                    key: "1"
                ) {
                    onDecision("acknowledge", [:])
                }
                DecisionButton("False Positive", systemImage: "xmark.circle", key: "2") {
                    onDecision("dismiss", [:])
                }
                DecisionButton("Later", systemImage: "arrowshape.turn.up.right", key: "3") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    @ViewBuilder private var conflictCard: some View {
        if item.isAlternativeComparison {
            let alternatives = item.alternatives ?? []
            let shortcutCount = min(alternatives.count, 7)
            let selectAllKey = String(shortcutCount + 1)
            let unsureKey = String(shortcutCount + 2)
            VStack(alignment: .leading, spacing: 14) {
                ForEach(Array(alternatives.enumerated()), id: \.offset) { index, fact in
                    VStack(alignment: .leading, spacing: 8) {
                        FactPanel(title: "Historical Fact \(index + 1)", fact: fact)
                        Button {
                            guard let factID = fact.displayID else {
                                return
                            }
                            if selectedAlternativeIDs.contains(factID) {
                                selectedAlternativeIDs.remove(factID)
                            } else {
                                selectedAlternativeIDs.insert(factID)
                            }
                        } label: {
                            Label {
                                HStack(spacing: 7) {
                                    Text("Keep This Fact")
                                    if index < shortcutCount {
                                        ShortcutBadge(key: String(index + 1))
                                    }
                                }
                            } icon: {
                                Image(
                                    systemName: selectedAlternativeIDs.contains(
                                        fact.displayID ?? ""
                                    )
                                        ? "checkmark.square.fill"
                                        : "square"
                                )
                            }
                        }
                        .buttonStyle(.bordered)
                        .tint(
                            selectedAlternativeIDs.contains(fact.displayID ?? "")
                                ? .accentColor
                                : .secondary
                        )
                        .queueKeyboardShortcut(
                            index < shortcutCount ? String(index + 1) : ""
                        )
                        .disabled(fact.displayID == nil)
                    }
                }
                DecisionBar {
                    DecisionButton(
                        "Keep Selected",
                        systemImage: "checkmark.circle",
                        key: "enter"
                    ) {
                        let orderedIDs = alternatives.compactMap(\.displayID).filter {
                            selectedAlternativeIDs.contains($0)
                        }
                        onDecision(
                            "select_facts",
                            [
                                "selected_fact_ids": .array(
                                    orderedIDs.map(JSONValue.string)
                                )
                            ]
                        )
                    }
                    .disabled(selectedAlternativeIDs.isEmpty)
                    DecisionButton(
                        "Select All",
                        systemImage: "checkmark.square",
                        key: selectAllKey
                    ) {
                        selectedAlternativeIDs = Set(
                            alternatives.compactMap(\.displayID)
                        )
                    }
                    Button {
                        selectedAlternativeIDs.removeAll()
                    } label: {
                        Label("Clear", systemImage: "xmark.square")
                    }
                    .buttonStyle(.bordered)
                    DecisionButton(
                        "Unsure",
                        systemImage: "questionmark.circle",
                        key: unsureKey
                    ) {
                        onDecision("unsure", [:])
                    }
                }
            }
        } else {
            let counterparts = item.counterparts ?? []
            VStack(alignment: .leading, spacing: 14) {
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
    }

    private var unroutedCard: some View {
        let routes = item.route_candidates ?? []
        let manualRouteKey = String(routes.count + 1)
        let rejectKey = String(routes.count + 2)
        let skipKey = String(routes.count + 3)
        return VStack(alignment: .leading, spacing: 14) {
            FactPanel(title: "Fact", fact: item.candidate)
            VStack(alignment: .leading, spacing: 8) {
                Text("Route Candidates")
                    .font(.headline)
                ForEach(Array(routes.enumerated()), id: \.element.id) { index, route in
                    Button {
                        routeFieldFocused = false
                        onDecision("route", ["page_hint": .string(route.page_hint)])
                    } label: {
                        HStack {
                            ShortcutBadge(key: String(index + 1))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(route.title ?? route.page_hint)
                                if let count = route.document_coherence_count, count > 0 {
                                    Text("\(count) routed facts from this source")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Text(route.page_hint)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.bordered)
                    .queueKeyboardShortcut(
                        routeFieldFocused ? "" : String(index + 1)
                    )
                    .help("\(index + 1) - Route to \(route.title ?? route.page_hint)")
                }
                RoutePathAutocompleteField(
                    pageHint: $routePageHint,
                    isFocused: $routeFieldFocused,
                    shortcutKey: manualRouteKey,
                    onSubmit: submitManualRoute
                )
            }
            DecisionBar {
                DecisionButton(
                    "Reject",
                    systemImage: "xmark.circle",
                    key: rejectKey,
                    shortcutEnabled: !routeFieldFocused
                ) {
                    onDecision("reject", [:])
                }
                DecisionButton(
                    "Skip",
                    systemImage: "arrowshape.turn.up.right",
                    key: skipKey,
                    shortcutEnabled: !routeFieldFocused
                ) {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var manualRoutePageHint: String {
        routePageHint.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func submitManualRoute() {
        guard !manualRoutePageHint.isEmpty else {
            return
        }
        let pageHint = manualRoutePageHint
        routeFieldFocused = false
        routePageHint = ""
        onDecision("route", ["page_hint": .string(pageHint)])
    }

    private var memoryCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(item.memory?.content ?? item.summary ?? "")
                .font(.body)
                .textSelection(.enabled)
            MetadataRow(values: [
                item.memory?.memory_type,
                item.memory?.scope,
                item.memory?.status,
            ])
            ConfidenceBadge(value: item.memory?.confidence)
            SourceDocumentsView(documents: item.memory?.source_documents ?? [])
            DecisionBar {
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "1") {
                    onDecision("approve", [:])
                }
                DecisionButton("Reject", systemImage: "xmark.circle", key: "2") {
                    onDecision("reject", [:])
                }
                DecisionButton("Archive", systemImage: "archivebox", key: "3") {
                    onDecision("archive", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "4") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var auditCard: some View {
        let isFact = item.audit?.action_type == "fact_upsert"
        let rejectsCurrentFact = item.audit?.revert_mode == "reject_current_fact"
        return VStack(alignment: .leading, spacing: 14) {
            Text("Auditor Finding")
                .font(.headline)
            MetadataRow(values: [
                item.audit?.status,
                item.audit?.model,
                item.audit?.audited_at.map { shortDate($0) },
                item.audit?.action_status,
            ])
            EvidenceQuote(text: item.audit?.rationale ?? item.summary)
            if let candidate = item.candidate {
                FactPanel(title: "Applied Fact", fact: candidate)
            }
            if let topology = item.topology {
                Text("Applied Change")
                    .font(.headline)
                TopologyTargetPanel(topology: topology)
                MetadataRow(values: [
                    item.audit?.affected_fact_count.map { "\($0) affected \($0 == 1 ? "fact" : "facts")" },
                    item.audit?.affected_page_count.map { "\($0) affected \($0 == 1 ? "page" : "pages")" },
                    item.audit?.affected_contract_count.map { "\($0) affected \($0 == 1 ? "contract" : "contracts")" },
                ])
                if item.candidate == nil,
                   let facts = item.audit?.affected_facts,
                   !facts.isEmpty {
                    Text("Representative Affected Facts")
                        .font(.subheadline.weight(.semibold))
                    ForEach(Array(facts.enumerated()), id: \.offset) { index, fact in
                        FactPanel(title: "Affected Fact \(index + 1)", fact: fact)
                    }
                }
            }
            if item.audit?.revertible == false {
                Label(auditRevertUnavailableText, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.orange)
            } else if rejectsCurrentFact {
                Label(
                    "Related state changed after apply. Reject will target the current active fact and remain undoable.",
                    systemImage: "info.circle"
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }
            DecisionBar {
                DecisionButton(
                    rejectsCurrentFact
                        ? "Reject Applied Fact"
                        : (isFact ? "Revert Applied Fact" : "Revert Applied Action"),
                    systemImage: rejectsCurrentFact
                        ? "xmark.circle"
                        : "arrow.uturn.backward.circle",
                    key: "1"
                ) {
                    onDecision("revert", [:])
                }
                .disabled(item.audit?.revertible == false)
                DecisionButton(
                    isFact ? "Keep Applied Fact" : "Keep Applied Action",
                    systemImage: "checkmark.seal",
                    key: "2"
                ) {
                    onDecision("mark_ok", [:])
                }
                DecisionButton("Later", systemImage: "arrowshape.turn.up.right", key: "3") {
                    onDecision("skip", [:])
                }
            }
        }
    }

    private var auditRevertUnavailableText: String {
        if item.audit?.reviewability_reason == "audited_fact_still_active_after_related_drift" {
            return "Related state changed after apply. The audited fact is still active, but direct revert is no longer safe."
        }
        return "This action has no safe direct revert."
    }

    private var actionCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(actionType)
                .font(.headline)
            if let topology = item.topology {
                TopologyTargetPanel(topology: topology)
                if let preview = topology.split_preview {
                    SplitPreviewPanel(preview: preview)
                }
            }
            MetadataRow(values: [item.status, item.risk_tier, actionString("proposed_by")])
            EvidenceQuote(text: item.summary)
            DecisionBar {
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "1") {
                    onDecision("approve", [:])
                }
                .disabled(item.topology?.split_preview?.approvable == false)
                DecisionButton("Reject", systemImage: "xmark.circle", key: "2") {
                    onDecision("reject", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "3") {
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
                DecisionButton("Approve", systemImage: "checkmark.circle", key: "1") {
                    onDecision("approve", [:])
                }
                DecisionButton("Reject", systemImage: "xmark.circle", key: "2") {
                    onDecision("reject", [:])
                }
                DecisionButton("Skip", systemImage: "arrowshape.turn.up.right", key: "3") {
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

    private func humanized(_ value: String) -> String {
        value.split(separator: "_").map { word in
            word.prefix(1).uppercased() + word.dropFirst()
        }.joined(separator: " ")
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
        if let page = topology.page_hints?.first, !page.isEmpty {
            return page
        }
        return "Topology target"
    }

    private var metadata: [String?] {
        let ids = (topology.entity_ids ?? []).filter { !$0.isEmpty }
        let pages = (topology.page_hints ?? []).filter { !$0.isEmpty }
        let statuses = ids.compactMap { entityID -> String? in
            guard let status = topology.entity_statuses?[entityID], !status.isEmpty else {
                return nil
            }
            return "\(shortID(entityID)) \(status)"
        }
        let pageStatuses = pages.compactMap { page -> String? in
            guard let status = topology.page_statuses?[page], !status.isEmpty else {
                return nil
            }
            return "\(page) \(status)"
        }
        var values: [String?] = [
            topology.merge_destination_label.map { "destination \($0)" }
        ]
        values += (topology.merge_source_labels ?? []).map { "source \($0)" }
        values += statuses
        if statuses.isEmpty {
            values += ids.map { "id \(shortID($0))" }
        }
        let pageValues = pageStatuses.isEmpty ? pages.map { "page \($0)" } : pageStatuses
        values += pageValues.prefix(6).map { $0 }
        if pageValues.count > 6 {
            values.append("+\(pageValues.count - 6) more pages")
        }
        return values
    }
}

private struct SplitPreviewPanel: View {
    let preview: QueueSplitPreview

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Page Split Preview")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(preview.source_page_hint)
                        .font(.headline)
                        .textSelection(.enabled)
                }
                Spacer()
                MetadataRow(values: [
                    "\(preview.movable_fact_count) facts move",
                    "\(preview.resulting_page_count) resulting pages",
                ])
            }
            if preview.children.isEmpty {
                Text("No movable section facts remain. Approval is disabled.")
                    .foregroundStyle(.red)
            } else {
                ForEach(preview.children) { child in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(child.section)
                                    .font(.callout.weight(.semibold))
                                Text(child.page_hint)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text("\(child.fact_count) facts")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        ForEach(Array(child.representative_facts.enumerated()), id: \.offset) { _, fact in
                            Text(fact.statement ?? "")
                                .font(.callout)
                                .textSelection(.enabled)
                        }
                    }
                    .padding(.vertical, 7)
                    Divider()
                }
            }
        }
    }
}

private struct QueueRow: View {
    let item: QueueItem
    let isSelected: Bool
    let isChecked: Bool
    let onCheck: () -> Void

    var body: some View {
        let batchSelectable = item.isApprovable && !item.isAlternativeComparison
        HStack(alignment: .top, spacing: 8) {
            Button(action: onCheck) {
                Image(systemName: isChecked ? "checkmark.square.fill" : "square")
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.plain)
            .disabled(!batchSelectable)
            .help(
                batchSelectable
                    ? "Select for batch review"
                    : (item.blocking_reason ?? "This comparison requires an individual choice")
            )
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayTitle)
                    .font(.callout.weight(isSelected ? .semibold : .regular))
                    .lineLimit(2)
                Text("\(item.group) - \(item.kind) - \(item.created_at ?? "")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                HStack(spacing: 6) {
                    if !item.isApprovable {
                        Label("Blocked", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.orange)
                    }
                    RetrievalBadge(
                        count: item.popularity?.retrieval_count,
                        lastRetrievedAt: item.popularity?.last_retrieved_at,
                        compact: true
                    )
                    ConfidenceBadge(value: item.primaryConfidence, compact: true)
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
        .background(isSelected ? Color.accentColor.opacity(0.13) : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct BlockedReviewNotice: View {
    let reason: String?

    var body: some View {
        Label {
            VStack(alignment: .leading, spacing: 3) {
                Text("Decision controls disabled")
                    .font(.callout.weight(.semibold))
                Text(reason ?? "This card is missing required review evidence.")
                    .font(.callout)
            }
        } icon: {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: 6))
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
                relationName,
                orientation.temporal_scope,
                labeledTime("candidate", orientation.candidate_observed_at),
                labeledTime("existing", orientation.existing_observed_at),
                orientation.currentness,
            ])
            ConfidenceBadge(value: orientation.relation_confidence)
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

    private var relationName: String? {
        guard let relation = orientation.relation, !relation.isEmpty else {
            return nil
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
                fact?.page_hint,
                fact?.entity_key,
                fact?.displayID.map(shortID),
            ])
            FlowLayout(spacing: 8) {
                SourceDateBadge(
                    value: fact?.source_date,
                    basis: fact?.source_date_basis
                )
                ConfidenceBadge(value: fact?.displayConfidence)
                RetrievalBadge(
                    count: fact?.retrieval_count,
                    lastRetrievedAt: fact?.last_retrieved_at
                )
            }
            SourceDocumentsView(documents: fact?.source_documents ?? [])
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct EvidenceQuote: View {
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

struct MetadataRow: View {
    let values: [String?]

    var body: some View {
        FlowLayout(spacing: 6) {
            ForEach(values.compactMap { value in
                let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                return trimmed.isEmpty ? nil : trimmed
            }, id: \.self) { value in
                Text(value)
                    .font(.caption)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: 360, alignment: .leading)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.10))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .help(value)
            }
        }
    }
}

struct SourceDocumentsView: View {
    let documents: [QueueSourceDocument]

    var body: some View {
        if !documents.isEmpty {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(documents.prefix(4)) { document in
                    VStack(alignment: .leading, spacing: 2) {
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
                        if let sourceDate = document.captured_at ?? document.created_at ?? document.ingested_at {
                            Text("Source \(dateOnly(sourceDate))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .help(sourceDate)
                        }
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
    let shortcutEnabled: Bool
    let help: String?
    let action: () -> Void

    init(
        _ title: String,
        systemImage: String,
        key: String,
        shortcutEnabled: Bool = true,
        help: String? = nil,
        action: @escaping () -> Void
    ) {
        self.title = title
        self.systemImage = systemImage
        self.key = key
        self.shortcutEnabled = shortcutEnabled
        self.help = help
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            Label {
                HStack(spacing: 7) {
                    Text(title)
                    ShortcutBadge(key: key)
                }
            } icon: {
                Image(systemName: systemImage)
            }
        }
        .queueKeyboardShortcut(shortcutEnabled ? key : "")
        .help(help ?? "\(key.uppercased()) - \(title)")
        .accessibilityLabel("\(title), keyboard shortcut \(key.uppercased())")
    }
}

extension View {
    @ViewBuilder
    func queueKeyboardShortcut(_ key: String) -> some View {
        switch key.lowercased() {
        case "enter", "return":
            keyboardShortcut(.return, modifiers: [])
        case "escape", "esc":
            keyboardShortcut(.escape, modifiers: [])
        default:
            if key.count == 1, let character = key.first {
                keyboardShortcut(KeyEquivalent(character), modifiers: [])
            } else {
                self
            }
        }
    }
}

private struct ShortcutBadge: View {
    let key: String

    var body: some View {
        Text(key.uppercased())
            .font(.caption2.monospaced().weight(.bold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
            .accessibilityHidden(true)
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

private struct QueueLoadRequest: Sendable {
    let id: UUID
    let kind: String
    let state: String
    let sort: String
}

private struct QueueUndoToast: Equatable {
    let title: String
    let handle: JSONValue
    let expiresAt: Date
}

func shortID(_ value: String) -> String {
    String(value.prefix(10))
}

func confidenceText(_ value: Double?) -> String? {
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
