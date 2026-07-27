import PKMBrainKit
import SwiftUI

struct TodayView: View {
    @EnvironmentObject private var appState: AppState
    @State private var feedbackDraft: TodayFeedbackDraft?
    @State private var meetingPreparationRequest: TodayMeetingPreparationRequest?
    @State private var reportsMissingItem = false
    @State private var showsSuppressedAudit = false
    @State private var showsHiddenCalendarSeries = false
    @State private var pendingCalendarItemIDs: Set<String> = []
    @State private var pendingCalendarSuppressionIDs: Set<String> = []
    @State private var calendarActionMessage: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                shadowRunStatus
                if appState.todayBriefing?.briefing_id == nil {
                    shadowFirstRunDisclosure
                }
                if let briefing = appState.todayBriefing, briefing.isAvailable {
                    operationalBriefing(briefing)
                    Divider()
                    legacyDigest(title: "Knowledge pulse")
                } else {
                    briefingUnavailable
                    legacyDigest(title: nil)
                }
                if let message = appState.todayFeedbackMessage {
                    Label(message, systemImage: "info.circle")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if let error = appState.todayError ?? appState.lastError {
                    Text(error)
                        .font(.callout)
                        .foregroundStyle(.red)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .toolbar {
            Button {
                Task {
                    await appState.runTodayShadow()
                }
            } label: {
                if appState.isRunningTodayShadow {
                    HStack(spacing: 6) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Running Shadow…")
                    }
                } else {
                    Label("Run Shadow", systemImage: "play.circle")
                }
            }
            .disabled(appState.isRunningTodayShadow)
            .help("Run one read-only Calendar and Gmail operational pass")

            Button {
                reportsMissingItem = true
            } label: {
                Label("Report Missing", systemImage: "plus.bubble")
            }
            .disabled(appState.todayBriefing?.feedback.allows("report_missing") != true)
            .help(reportMissingHelp)

            Button {
                Task {
                    await appState.refreshToday()
                    await appState.refreshDigest()
                }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
        .sheet(item: $feedbackDraft) { draft in
            TodayFeedbackSheet(draft: draft) { note, snoozedUntil in
                Task {
                    await appState.submitTodayFeedback(
                        itemID: draft.item.id,
                        action: draft.action,
                        note: note,
                        snoozedUntil: snoozedUntil
                    )
                }
            }
        }
        .sheet(isPresented: $reportsMissingItem) {
            TodayMissingItemSheet { title, detail, sourceHint in
                Task {
                    await appState.reportMissingTodayItem(
                        title: title,
                        detail: detail,
                        sourceHint: sourceHint
                    )
                }
            }
        }
        .sheet(item: $appState.todayEvidenceRequest) { request in
            TodayEvidenceSheet(request: request)
                .environmentObject(appState)
        }
        .sheet(item: $meetingPreparationRequest) { request in
            TodayMeetingPreparationSheet(request: request)
                .environmentObject(appState)
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Today")
                    .font(.largeTitle.weight(.semibold))
                Text(daemonStatusLabel)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let generated = appState.todayBriefing?.generated_at
                ?? appState.digest?.generated_at {
                Text("Updated \(displayDateTime(generated))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .help(generated)
            }
        }
    }

    private var daemonStatusLabel: String {
        switch appState.daemon.status {
        case .running:
            return "Brain service ready"
        default:
            return appState.daemon.status.label
        }
    }

    @ViewBuilder
    private var shadowRunStatus: some View {
        if appState.isRunningTodayShadow {
            TodayShadowRunStatusCard(
                title: "Shadow run in progress",
                detail: appState.todayShadowRunMessage
                    ?? "Reading Calendar and Gmail without making changes…",
                symbol: "arrow.triangle.2.circlepath",
                tint: .accentColor,
                showsProgress: true
            )
        } else if let error = appState.todayShadowRunError {
            TodayShadowRunStatusCard(
                title: "Shadow run needs attention",
                detail: error,
                footnote: "Today was refreshed with any operational state retained before the failure.",
                symbol: "exclamationmark.triangle.fill",
                tint: .red
            )
        } else if let result = appState.todayShadowRunResult,
                  result.displayKind == .partial {
            TodayShadowRunStatusCard(
                title: "Shadow run finished with partial coverage",
                detail: appState.todayShadowRunMessage ?? result.message,
                footnote: "Review source coverage before treating the briefing as complete.",
                symbol: "exclamationmark.circle.fill",
                tint: .orange
            )
        } else if let message = appState.todayShadowRunMessage {
            TodayShadowRunStatusCard(
                title: "Shadow run complete",
                detail: message,
                symbol: "checkmark.circle.fill",
                tint: .green
            )
        }
    }

    private var shadowFirstRunDisclosure: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: "lock.shield")
                .font(.title2)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text("Your first shadow run is read-only and starts only when you choose it.")
                    .font(.headline)
                Text(
                    "It reads your owned primary Calendar and Gmail and never downloads attachments or changes either service. It keeps a disposable raw API cache on this Mac for up to 7 days and normalized supporting evidence for up to 30 days. Bounded text from changed emails is analyzed by your configured detector model and may leave this Mac if that model is hosted elsewhere; the default detector has no tools or access to other local files."
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            Button {
                Task { await appState.runTodayShadow() }
            } label: {
                Label("Run Shadow", systemImage: "play.circle")
            }
            .buttonStyle(.borderedProminent)
            .disabled(appState.isRunningTodayShadow)
        }
        .padding(14)
        .background(Color.accentColor.opacity(0.07))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.accentColor.opacity(0.18), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder
    private func operationalBriefing(_ briefing: TodayBriefing) -> some View {
        TodayCoverageView(briefing: briefing)

        TodayItemSection(
            title: "Focus",
            subtitle: "Up to five distinct episodes that merit your attention now.",
            symbol: "scope",
            items: briefing.visibleFocus,
            emptyText: "Nothing needs to be forced into focus right now.",
            feedback: briefing.feedback,
            pendingFeedbackItemIDs: pendingCalendarItemIDs,
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )

        if briefing.urgent_overflow.count > 0 {
            VStack(alignment: .leading, spacing: 10) {
                Label(
                    "Urgent overflow · \(briefing.urgent_overflow.count)",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.title2.weight(.semibold))
                .foregroundStyle(.orange)
                Text("These urgent items did not fit in the five-item focus projection.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                ForEach(briefing.urgent_overflow.items) { item in
                    itemCard(item, briefing: briefing, audit: false)
                }
                let undisplayed = briefing.urgent_overflow.count
                    - briefing.urgent_overflow.items.count
                if undisplayed > 0 {
                    Text(
                        "\(undisplayed) additional urgent "
                            + (undisplayed == 1 ? "item is" : "items are")
                            + " disclosed but not expanded."
                    )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }

        calendarSection(briefing)

        TodayItemSection(
            title: "Due & overdue",
            symbol: "calendar.badge.exclamationmark",
            items: briefing.due_overdue,
            emptyText: "No due or overdue items surfaced.",
            feedback: briefing.feedback,
            pendingFeedbackItemIDs: pendingCalendarItemIDs,
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Waiting",
            symbol: "hourglass",
            items: briefing.waiting,
            emptyText: "Nothing is currently waiting on another person.",
            feedback: briefing.feedback,
            pendingFeedbackItemIDs: pendingCalendarItemIDs,
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Attention",
            symbol: "bell.badge",
            items: briefing.attention,
            emptyText: "No additional attention items.",
            feedback: briefing.feedback,
            pendingFeedbackItemIDs: pendingCalendarItemIDs,
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Awareness",
            symbol: "eye",
            items: briefing.awareness,
            emptyText: "No awareness-only updates.",
            feedback: briefing.feedback,
            pendingFeedbackItemIDs: pendingCalendarItemIDs,
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Uncertain",
            subtitle: "Visible because incomplete evidence must not become an all-clear.",
            symbol: "questionmark.diamond",
            items: briefing.uncertain,
            emptyText: "No uncertain items surfaced.",
            feedback: briefing.feedback,
            pendingFeedbackItemIDs: pendingCalendarItemIDs,
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )
        ignoredSuppressedSection(briefing)
    }

    @ViewBuilder
    private func ignoredSuppressedSection(_ briefing: TodayBriefing) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if briefing.ignoredSuppressedTotal == 0 {
                Label("Ignored & suppressed audit", systemImage: "checklist.unchecked")
                    .font(.title2.weight(.semibold))
                Text("No ignored or suppressed items were recorded.")
                    .foregroundStyle(.secondary)
            } else {
                DisclosureGroup(isExpanded: $showsSuppressedAudit) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(
                            "Why these items were withheld from focus, with reversible reason codes."
                        )
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        ForEach(briefing.ignored_suppressed) { item in
                            itemCard(item, briefing: briefing, audit: true)
                        }
                        let undisplayed = briefing.ignoredSuppressedTotal
                            - briefing.ignored_suppressed.count
                        if undisplayed > 0 {
                            Text(
                                "\(undisplayed) additional audit "
                                    + (undisplayed == 1 ? "item remains" : "items remain")
                                    + " in the local ledger."
                            )
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Label(
                        "Ignored & suppressed audit · \(briefing.ignoredSuppressedTotal)",
                        systemImage: "checklist.unchecked"
                    )
                    .font(.title2.weight(.semibold))
                }
            }
        }
    }

    private func calendarSection(_ briefing: TodayBriefing) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Calendar", systemImage: "calendar")
                .font(.title2.weight(.semibold))
            if briefing.calendar.now.isEmpty && briefing.calendar.next.isEmpty {
                Text("No current or upcoming calendar events surfaced.")
                    .foregroundStyle(.secondary)
            } else {
                if !briefing.calendar.now.isEmpty {
                    Text("Now")
                        .font(.headline)
                    ForEach(briefing.calendar.now) { item in
                        itemCard(item, briefing: briefing, audit: false)
                    }
                }
                if !briefing.calendar.next.isEmpty {
                    Text("Next")
                        .font(.headline)
                    ForEach(briefing.calendar.next) { item in
                        itemCard(item, briefing: briefing, audit: false)
                    }
                }
            }
            if !briefing.calendar.hiddenSeries.isEmpty {
                DisclosureGroup(isExpanded: $showsHiddenCalendarSeries) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(
                            "These recurring blocks stay on Google Calendar but are hidden from Today."
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        ForEach(briefing.calendar.hiddenSeries) { suppression in
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(suppression.label)
                                        .font(.callout.weight(.medium))
                                    Text(
                                        "\(suppression.hidden_count) "
                                            + (suppression.hidden_count == 1 ? "occurrence hidden" : "occurrences hidden")
                                    )
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    if let nextStartsAt = suppression.next_starts_at {
                                        Text("Next occurrence: \(displayDateTime(nextStartsAt))")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                Spacer()
                                if pendingCalendarSuppressionIDs.contains(suppression.id) {
                                    ProgressView("Restoring…")
                                        .controlSize(.small)
                                        .accessibilityLabel("Restoring \(suppression.label) to Today")
                                } else {
                                    Button("Undo") {
                                        restoreCalendarSeries(suppression)
                                    }
                                    .buttonStyle(.link)
                                    .accessibilityLabel("Undo hiding \(suppression.label)")
                                    .help("Show this recurring series in Today again")
                                }
                            }
                        }
                    }
                    .padding(.top, 6)
                } label: {
                    Label(
                        "\(briefing.calendar.hiddenSeries.count) recurring "
                            + (briefing.calendar.hiddenSeries.count == 1 ? "series hidden" : "series hidden"),
                        systemImage: "eye.slash"
                    )
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                }
            }
            if let calendarActionMessage {
                Label(calendarActionMessage, systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Calendar update: \(calendarActionMessage)")
            }
        }
    }

    private func itemCard(
        _ item: TodayItem,
        briefing: TodayBriefing,
        audit: Bool
    ) -> some View {
        TodayItemCard(
            item: item,
            feedback: briefing.feedback,
            audit: audit,
            isFeedbackPending: pendingCalendarItemIDs.contains(item.id),
            onFeedback: beginFeedback,
            onPrepare: beginMeetingPreparation,
            onBrainRoute: appState.openTodayBrainRoute
        )
    }

    private func beginMeetingPreparation(_ item: TodayItem) {
        guard item.supportsMeetingPreparation else {
            appState.todayFeedbackMessage = "Meeting preparation is not available for this item."
            return
        }
        meetingPreparationRequest = TodayMeetingPreparationRequest(
            itemID: item.id,
            fallbackTitle: item.title,
            preparedInAdvance: item.isMeetingBriefReady
        )
    }

    private func beginFeedback(_ item: TodayItem, _ action: String) {
        if action == "correct" || action == "snooze" {
            feedbackDraft = TodayFeedbackDraft(item: item, action: action)
            return
        }
        if action == "dismiss_series" {
            guard !pendingCalendarItemIDs.contains(item.id) else { return }
            pendingCalendarItemIDs.insert(item.id)
            calendarActionMessage = nil
            Task {
                await appState.submitTodayFeedback(itemID: item.id, action: action)
                calendarActionMessage = appState.todayFeedbackMessage
                pendingCalendarItemIDs.remove(item.id)
            }
            return
        }
        Task {
            await appState.submitTodayFeedback(itemID: item.id, action: action)
        }
    }

    private func restoreCalendarSeries(_ suppression: TodayCalendarSuppression) {
        guard !pendingCalendarSuppressionIDs.contains(suppression.id) else { return }
        pendingCalendarSuppressionIDs.insert(suppression.id)
        calendarActionMessage = nil
        Task {
            await appState.restoreTodayCalendarSeries(suppression.id)
            calendarActionMessage = appState.todayFeedbackMessage
            pendingCalendarSuppressionIDs.remove(suppression.id)
        }
    }

    private var briefingUnavailable: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Operational briefing unavailable", systemImage: "rectangle.dashed")
                .font(.headline)
            Text(
                appState.todayBriefing?.availability_reason
                    ?? "The Chief-of-Staff projection is not enabled. Knowledge status remains available below."
            )
            .font(.callout)
            .foregroundStyle(.secondary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private var feedbackUnavailableReason: String {
        appState.todayBriefing?.feedback.unavailable_reason
            ?? "Operational feedback is unavailable until the briefing runner is enabled."
    }

    private var reportMissingHelp: String {
        if appState.todayBriefing?.feedback.allows("report_missing") == true {
            return "Tell the Chief of Staff about an item it failed to surface."
        }
        return feedbackUnavailableReason
    }

    @ViewBuilder
    private func legacyDigest(title: String?) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            if let title {
                Text(title)
                    .font(.title2.weight(.semibold))
            }
            pulseGrid
            needsYou
            recentFacts
        }
    }

    private var pulseGrid: some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 220), spacing: 12)],
            alignment: .leading,
            spacing: 12
        ) {
            ForEach(appState.digest?.pulse ?? []) { chip in
                PulseChipView(
                    chip: chip,
                    detail: appState.digest?.detailText(for: chip)
                )
            }
        }
    }

    private var needsYou: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Knowledge review")
                .font(.title2.weight(.semibold))
            if let summary = appState.queueSummary,
               summary.active_total + summary.deferred_total > 0 {
                HStack(spacing: 10) {
                    ForEach(actionableCounts(summary), id: \.key) { kind, count in
                        Label("\(kind) \(count)", systemImage: "circle.fill")
                            .font(.callout)
                    }
                    if summary.blocked_total > 0 {
                        Label(
                            "blocked \(summary.blocked_total)",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .font(.callout)
                        .foregroundStyle(.orange)
                    }
                    if summary.deferred_total > 0 {
                        Label("deferred \(summary.deferred_total)", systemImage: "clock")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                HStack {
                    if summary.actionable_total > 0 {
                        Button("Start Review") { appState.showQueue() }
                    }
                    if summary.deferred_total > 0 {
                        Button("View Deferred") { appState.showQueue(state: "deferred") }
                    }
                }
            } else {
                Text("No knowledge review items in the current digest.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func actionableCounts(_ summary: QueueSummary) -> [(key: String, value: Int)] {
        summary.by_kind.compactMap { kind, activeCount in
            let count = activeCount - (summary.blocked_by_kind?[kind] ?? 0)
            return count > 0 ? (kind, count) : nil
        }
        .sorted { $0.key < $1.key }
    }

    private var recentFacts: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Since Last Look")
                .font(.title2.weight(.semibold))
            let facts = appState.digest?.facts_by_page ?? []
            if facts.isEmpty {
                Text("No new fact deltas in this digest.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(facts.prefix(10)) { item in
                    HStack {
                        Text(item.page_hint)
                            .lineLimit(1)
                        Spacer()
                        Text("\(item.count)")
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                    Divider()
                }
            }
        }
    }
}

private struct TodayShadowRunStatusCard: View {
    let title: String
    let detail: String
    var footnote: String? = nil
    let symbol: String
    let tint: Color
    var showsProgress = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if showsProgress {
                ProgressView()
                    .controlSize(.small)
                    .tint(tint)
                    .padding(.top, 2)
            } else {
                Image(systemName: symbol)
                    .foregroundStyle(tint)
                    .padding(.top, 2)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.headline)
                Text(detail)
                    .font(.callout)
                    .textSelection(.enabled)
                if let footnote {
                    Text(footnote)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.09))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(tint.opacity(0.25), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("Today.shadowRun.status")
    }
}

private struct TodayCoverageView: View {
    let briefing: TodayBriefing

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Label(freshnessLabel, systemImage: freshnessSymbol)
                    .font(.headline)
                    .foregroundStyle(freshnessColor)
                if briefing.status == "partial" {
                    Text("Partial coverage")
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.orange.opacity(0.14))
                        .clipShape(Capsule())
                }
                Spacer()
                if let asOf = briefing.as_of ?? briefing.freshness.as_of {
                    Text("As of \(displayDateTime(asOf))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            if briefing.coverage.isEmpty {
                Text("No source coverage was reported.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 180), spacing: 8)],
                    alignment: .leading,
                    spacing: 8
                ) {
                    ForEach(briefing.coverage) { source in
                        VStack(alignment: .leading, spacing: 4) {
                            Label(source.label, systemImage: coverageSymbol(source.state))
                                .font(.callout.weight(.medium))
                            Text(coverageDetail(source))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        .padding(9)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.secondary.opacity(0.07))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
            Text(
                "Shadow v1 verifies handled state within each source. It does not yet infer that an email was handled through Calendar or another channel."
            )
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private var freshnessLabel: String {
        switch briefing.freshness.state {
        case "fresh": return "Briefing fresh"
        case "stale": return "Briefing stale"
        default: return "Freshness unknown"
        }
    }

    private var freshnessSymbol: String {
        switch briefing.freshness.state {
        case "fresh": return "checkmark.circle.fill"
        case "stale": return "clock.badge.exclamationmark"
        default: return "questionmark.circle"
        }
    }

    private var freshnessColor: Color {
        switch briefing.freshness.state {
        case "fresh": return .green
        case "stale": return .orange
        default: return .secondary
        }
    }

    private func coverageSymbol(_ state: String) -> String {
        switch state {
        case "complete": return "checkmark.circle"
        case "partial": return "circle.lefthalf.filled"
        default: return "xmark.circle"
        }
    }

    private func coverageDetail(_ source: TodayCoverage) -> String {
        if let detail = source.detail { return detail }
        if source.deferred_count > 0 { return "\(source.deferred_count) deferred" }
        if let last = source.last_success_at { return "Updated \(displayDateTime(last))" }
        return source.state.capitalized
    }
}

private struct TodayItemSection: View {
    let title: String
    var subtitle: String? = nil
    let symbol: String
    let items: [TodayItem]
    let emptyText: String
    let feedback: TodayFeedbackCapabilities
    var audit = false
    var pendingFeedbackItemIDs: Set<String> = []
    let onFeedback: (TodayItem, String) -> Void
    let onPrepare: (TodayItem) -> Void
    let onBrainRoute: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: symbol)
                .font(.title2.weight(.semibold))
            if let subtitle {
                Text(subtitle)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            if items.isEmpty {
                Text(emptyText)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(items) { item in
                    TodayItemCard(
                        item: item,
                        feedback: feedback,
                        audit: audit,
                        isFeedbackPending: pendingFeedbackItemIDs.contains(item.id),
                        onFeedback: onFeedback,
                        onPrepare: onPrepare,
                        onBrainRoute: onBrainRoute
                    )
                }
            }
        }
    }
}

private struct TodayItemCard: View {
    let item: TodayItem
    let feedback: TodayFeedbackCapabilities
    let audit: Bool
    let isFeedbackPending: Bool
    let onFeedback: (TodayItem, String) -> Void
    let onPrepare: (TodayItem) -> Void
    let onBrainRoute: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .firstTextBaseline) {
                Text(item.title)
                    .font(.headline)
                Spacer()
                if let priority = item.priority {
                    Text(priority)
                        .font(.caption.weight(.bold))
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(priorityColor.opacity(0.15))
                        .clipShape(Capsule())
                }
            }
            if let summary = item.summary, !summary.isEmpty {
                Text(summary)
                    .font(.callout)
            }
            HStack(spacing: 12) {
                Label(item.handledLabel, systemImage: handledSymbol)
                    .foregroundStyle(handledColor)
                if let timing = timingText {
                    Label(timing, systemImage: "clock")
                }
                if let confidence = item.confidence {
                    Label(
                        "\(Int((confidence * 100).rounded()))%",
                        systemImage: "gauge.with.dots.needle.33percent"
                    )
                    .help("Model confidence; evidence and coverage still govern trust.")
                }
            }
            .font(.caption)

            if let reason = item.handled_reason, !reason.isEmpty {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if audit || !item.reason_codes.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 5) {
                        ForEach(item.reason_codes, id: \.self) { code in
                            Text(code.replacingOccurrences(of: "_", with: " "))
                                .font(.caption2.monospaced())
                                .padding(.horizontal, 6)
                                .padding(.vertical, 3)
                                .background(Color.secondary.opacity(0.1))
                                .clipShape(Capsule())
                        }
                    }
                }
            }
            if !item.evidence.isEmpty {
                HStack(spacing: 8) {
                    ForEach(item.evidence) { evidence in
                        if let route = evidence.brain_route {
                            Button {
                                onBrainRoute(route)
                            } label: {
                                Label(localEvidenceActionTitle(evidence), systemImage: "brain.head.profile")
                            }
                            .buttonStyle(.link)
                            .accessibilityLabel("\(localEvidenceActionTitle(evidence)) for \(item.title)")
                            .help("\(localEvidenceActionTitle(evidence)) · \(evidence.reference)")
                        }
                        if let url = safeProviderURL(evidence.provider_url) {
                            Link(destination: url) {
                                Label(providerEvidenceActionTitle(evidence), systemImage: "arrow.up.right.square")
                            }
                            .buttonStyle(.link)
                            .accessibilityLabel("\(providerEvidenceActionTitle(evidence)) for \(item.title)")
                            .help("\(providerEvidenceActionTitle(evidence)) · \(evidence.reference)")
                        }
                    }
                }
                .font(.caption)
            }
            if item.supportsMeetingPreparation {
                Button {
                    onPrepare(item)
                } label: {
                    Label(
                        item.isMeetingBriefReady ? "Open brief" : "Prepare now",
                        systemImage: item.isMeetingBriefReady
                            ? "doc.text.magnifyingglass"
                            : "sparkles.rectangle.stack"
                    )
                }
                .buttonStyle(.bordered)
                .accessibilityLabel(
                    "\(item.isMeetingBriefReady ? "Open brief" : "Prepare now") for \(item.title)"
                )
                .help(
                    item.isMeetingBriefReady
                        ? "Open the meeting brief prepared in advance"
                        : "Prepare a read-only meeting brief now"
                )
            }
            if !item.feedback_actions.isEmpty {
                Menu {
                    ForEach(item.feedback_actions, id: \.self) { action in
                        Button {
                            onFeedback(item, action)
                        } label: {
                            Label(feedbackTitle(action), systemImage: feedbackSymbol(action))
                        }
                        .disabled(!feedback.allows(action) || isFeedbackPending)
                        .accessibilityLabel("\(feedbackTitle(action)) for \(item.title)")
                    }
                } label: {
                    if isFeedbackPending {
                        Label("Updating…", systemImage: "arrow.triangle.2.circlepath")
                    } else {
                        Label("Update", systemImage: "ellipsis.circle")
                    }
                }
                .disabled(!feedback.enabled || isFeedbackPending)
                .accessibilityLabel("Update \(item.title)")
                .help(feedback.unavailable_reason ?? "Correct this operational item")
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(audit ? Color.secondary.opacity(0.06) : Color.accentColor.opacity(0.06))
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .stroke(Color.secondary.opacity(0.13), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 9))
    }

    private var timingText: String? {
        if let due = item.due_at { return "Due \(displayDateTime(due))" }
        if let starts = item.starts_at { return "Starts \(displayDateTime(starts))" }
        if let ends = item.ends_at { return "Ends \(displayDateTime(ends))" }
        return nil
    }

    private var handledSymbol: String {
        switch item.handled_verdict {
        case "needs_action": return "exclamationmark.circle.fill"
        case "responded_waiting": return "arrowshape.turn.up.left.circle"
        case "being_handled": return "person.2.circle"
        case "fulfilled": return "checkmark.circle.fill"
        default: return "questionmark.circle"
        }
    }

    private func localEvidenceActionTitle(_ evidence: TodayEvidenceLink) -> String {
        switch evidence.source_type {
        case "gmail": return "View local copy"
        case "calendar": return "View local details"
        default: return evidence.label
        }
    }

    private func providerEvidenceActionTitle(_ evidence: TodayEvidenceLink) -> String {
        switch evidence.source_type {
        case "gmail": return "Open in Gmail"
        case "calendar": return "Open in Calendar"
        default: return "Open provider source"
        }
    }

    private var handledColor: Color {
        switch item.handled_verdict {
        case "needs_action": return .orange
        case "fulfilled": return .green
        default: return .secondary
        }
    }

    private var priorityColor: Color {
        switch item.priority {
        case "P0": return .red
        case "P1": return .orange
        case "P2": return .blue
        default: return .secondary
        }
    }

    private func safeProviderURL(_ value: String?) -> URL? {
        guard let value, let url = URL(string: value),
              url.scheme == "https" || url.scheme == "http" else {
            return nil
        }
        return url
    }

    private func feedbackTitle(_ action: String) -> String {
        switch action {
        case "confirm": return "Looks right"
        case "correct": return "This is wrong"
        case "done": return "Mark done"
        case "snooze": return "Snooze"
        case "dismiss": return "Dismiss"
        case "dismiss_series": return "Hide recurring series"
        case "restore": return "Restore"
        default: return action.capitalized
        }
    }

    private func feedbackSymbol(_ action: String) -> String {
        switch action {
        case "confirm": return "checkmark.seal"
        case "correct": return "pencil"
        case "done": return "checkmark"
        case "snooze": return "clock"
        case "dismiss": return "eye.slash"
        case "dismiss_series": return "repeat.circle"
        case "restore": return "arrow.uturn.backward"
        default: return "circle"
        }
    }
}

private struct TodayMeetingPreparationRequest: Identifiable {
    let itemID: String
    let fallbackTitle: String
    let preparedInAdvance: Bool

    var id: String { itemID }
}

private struct TodayMeetingPreparationSheet: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var appState: AppState
    let request: TodayMeetingPreparationRequest

    @State private var packet: TodayMeetingPacket?
    @State private var errorMessage: String?
    @State private var isLoading = true
    @State private var sourcesExpanded = false
    @State private var sourceEvidenceRequest: TodayEvidenceRequest?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                Group {
                    if isLoading {
                        ProgressView(
                            request.preparedInAdvance
                                ? "Opening your prepared meeting brief…"
                                : "Preparing your meeting brief…"
                        )
                            .frame(maxWidth: .infinity, minHeight: 360)
                    } else if let packet {
                        meetingPacket(packet)
                    } else {
                        unavailable
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 720, idealWidth: 820, minHeight: 600, idealHeight: 760)
        .task(id: request.id) {
            await load()
        }
        .sheet(item: $sourceEvidenceRequest) { request in
            TodayEvidenceSheet(request: request)
                .environmentObject(appState)
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "sparkles.rectangle.stack")
                .font(.title2)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(packet?.title ?? request.fallbackTitle)
                    .font(.title2.weight(.semibold))
                    .lineLimit(2)
                Text("Executive meeting brief")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Done") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(18)
    }

    private func meetingPacket(_ packet: TodayMeetingPacket) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            Label(
                "Prepared \(displayDateTime(packet.generated_at)) from Calendar and local Brain context.",
                systemImage: "clock"
            )
            .font(.caption)
            .foregroundStyle(.secondary)

            coverageWarning(packet)
            purposeAndContext(packet)
            relevantBackground(packet)
            suggestedAgenda(packet.suggestions)
            openQuestionsAndPreparation(packet)
            relevantLinks(packet)
            sourcesAndDiagnostics(packet)

            Label(
                "This is a derived, read-only brief. Preparing it did not change Calendar, Gmail, or Brain knowledge.",
                systemImage: "lock.shield"
            )
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func coverageWarning(_ packet: TodayMeetingPacket) -> some View {
        let concerns = packet.coverage.values.filter {
            coveragePresentation($0).hasConcern
        }
        if !concerns.isEmpty || packet.coverage.isEmpty {
            Label(
                "This brief has partial source coverage. Use the available context, then check Sources & diagnostics before relying on omissions.",
                systemImage: "exclamationmark.triangle.fill"
            )
            .font(.callout)
            .foregroundStyle(.orange)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 9))
        }
    }

    private func purposeAndContext(_ packet: TodayMeetingPacket) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                if let context = packet.brief_context {
                    if context.starts_at != nil {
                        Label {
                            Text(scheduleText(context))
                        } icon: {
                            Image(systemName: context.all_day ? "sun.max" : "clock")
                        }
                    }
                    if let location = context.location, !location.isEmpty {
                        Label(location, systemImage: "mappin.and.ellipse")
                    }
                    if let organizer = context.organizer_email, !organizer.isEmpty {
                        Label("Organized by \(organizer)", systemImage: "person.crop.circle")
                    }
                    if let attendeeCount = context.attendee_count, attendeeCount > 0 {
                        Label(
                            "\(attendeeCount) attendee\(attendeeCount == 1 ? "" : "s")",
                            systemImage: "person.2"
                        )
                    }
                    if let response = context.attendee_response, !response.isEmpty {
                        Label(
                            "Your response: \(friendlyStatus(response))",
                            systemImage: "checkmark.circle"
                        )
                    }
                    Divider()
                    if let notes = context.calendar_notes, !notes.isEmpty {
                        Text(notes)
                            .font(.body)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text(calendarNotesPlaceholder(context.calendar_notes_status))
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("No purpose or agenda was included in the prepared Calendar context. Schedule details remain available under Sources & diagnostics.")
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.top, 4)
        } label: {
            Label("Purpose & context", systemImage: "scope")
        }
    }

    private func relevantBackground(_ packet: TodayMeetingPacket) -> some View {
        let claims = packet.brief_knowledge_claims ?? []
        let pages = packet.brief_wiki_context ?? []
        return GroupBox {
            VStack(alignment: .leading, spacing: 14) {
                if claims.isEmpty && pages.isEmpty {
                    Text("No source-backed background was found in the local Brain within this brief's retrieval budget.")
                        .foregroundStyle(.secondary)
                }

                ForEach(claims) { claim in
                    Label {
                        Text(claim.claim)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "circle.fill")
                            .font(.system(size: 6))
                    }
                }

                ForEach(pages) { context in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(context.title)
                                .font(.headline)
                            Spacer()
                            if let path = context.path, !path.isEmpty {
                                Button("Open in Wiki") {
                                    dismiss()
                                    appState.showWiki(path: path)
                                }
                                .buttonStyle(.link)
                            }
                        }
                        if !context.summary.isEmpty {
                            Text(context.summary)
                                .font(.callout)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(10)
                    .background(Color.secondary.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
            .padding(.top, 4)
        } label: {
            Label("Relevant background", systemImage: "text.book.closed")
        }
    }

    private func suggestedAgenda(_ suggestions: [TodayMeetingSuggestion]) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Text("Suggested structure based on the meeting topic and available context—not source-backed claims.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                if suggestions.isEmpty {
                    Text("No agenda prompts were generated.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(suggestions.indices, id: \.self) { index in
                        let suggestion = suggestions[index]
                        HStack(alignment: .firstTextBaseline, spacing: 9) {
                            Text("\(index + 1)")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                                .frame(width: 18, alignment: .trailing)
                            Text(suggestion.suggestion)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if suggestion.is_factual_claim {
                            Label("Verify this possible claim before relying on it.", systemImage: "exclamationmark.triangle")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                    }
                }
            }
            .padding(.top, 4)
        } label: {
            Label("Likely agenda & talking points", systemImage: "list.number")
        }
    }

    private func openQuestionsAndPreparation(_ packet: TodayMeetingPacket) -> some View {
        let questions = packet.brief_open_questions ?? []
        return GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                if questions.isEmpty {
                    Text("No topic-specific open questions were found in Brain.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(questions) { question in
                        Label(question.question, systemImage: "questionmark.bubble")
                    }
                }
                Divider()
                Label(
                    "Decide what outcome would make this meeting worthwhile.",
                    systemImage: "checkmark.circle"
                )
                Label(
                    "Bring any material needed to resolve the decisions above.",
                    systemImage: "checkmark.circle"
                )
            }
            .padding(.top, 4)
        } label: {
            Label("Open questions & preparation", systemImage: "questionmark.circle")
        }
    }

    @ViewBuilder
    private func relevantLinks(_ packet: TodayMeetingPacket) -> some View {
        let links = packet.source_links ?? []
        if !links.isEmpty {
            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(links) { link in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Image(systemName: sourceLinkSymbol(link.source_type))
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(link.label)
                                    .font(.headline)
                                if let detail = link.detail, !detail.isEmpty {
                                    Text(detail)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            sourceLinkAction(link)
                        }
                    }
                }
                .padding(.top, 4)
            } label: {
                Label("Relevant links", systemImage: "link")
            }
        }
    }

    private func sourcesAndDiagnostics(_ packet: TodayMeetingPacket) -> some View {
        DisclosureGroup(isExpanded: $sourcesExpanded) {
            VStack(alignment: .leading, spacing: 16) {
                coverage(packet)
                eventClaims(packet.event_claims)
                backgroundEvidence(packet)
                sourceReferences(packet)
            }
            .padding(.top, 12)
        } label: {
            Label("Sources & diagnostics", systemImage: "doc.text.magnifyingglass")
                .font(.headline)
        }
        .padding(12)
        .background(Color.secondary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 9))
    }

    private func coverage(_ packet: TodayMeetingPacket) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                if packet.coverage.isEmpty {
                    Label("Source coverage was not reported.", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                } else {
                    ForEach(packet.coverage.keys.sorted(), id: \.self) { source in
                        let presentation = coveragePresentation(packet.coverage[source])
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                Text(coverageLabel(source))
                                    .font(.headline)
                                Spacer()
                                Label(
                                    presentation.status,
                                    systemImage: presentation.hasConcern
                                        ? "exclamationmark.triangle.fill"
                                        : "checkmark.circle.fill"
                                )
                                .font(.caption.weight(.medium))
                                .foregroundStyle(presentation.hasConcern ? Color.orange : Color.green)
                            }
                            if let detail = presentation.detail {
                                Text(detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if !packet.retrieval_reasons.isEmpty {
                    Divider()
                    Label("Retrieval notes", systemImage: "info.circle")
                        .font(.headline)
                    ForEach(packet.retrieval_reasons, id: \.self) { reason in
                        Text("• \(reason)")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.top, 4)
        } label: {
            Label("Coverage & freshness", systemImage: "checklist")
        }
    }

    private func eventClaims(_ claims: [TodayMeetingClaim]) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                if claims.isEmpty {
                    Text("No Calendar observations were available for this event.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(claims) { claim in
                        claimView(claim, symbol: "calendar", sourceLabel: "Calendar observation")
                    }
                }
            }
            .padding(.top, 4)
        } label: {
            Label("Event details", systemImage: "calendar")
        }
    }

    private func backgroundEvidence(_ packet: TodayMeetingPacket) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 16) {
                if packet.knowledge_claims.isEmpty && packet.wiki_context.isEmpty {
                    Text("No relevant Brain facts or Wiki context were found within the retrieval budget.")
                        .foregroundStyle(.secondary)
                }

                ForEach(packet.knowledge_claims) { claim in
                    claimView(claim, symbol: "brain.head.profile", sourceLabel: "Brain fact")
                }

                ForEach(packet.wiki_context) { context in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .firstTextBaseline) {
                            Label(context.title, systemImage: "doc.text")
                                .font(.headline)
                            Spacer()
                        }
                        if !context.summary.isEmpty {
                            Text(context.summary)
                                .font(.callout)
                        }
                        if context.source_ids.isEmpty {
                            Label("No supporting source IDs were returned.", systemImage: "exclamationmark.triangle")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        } else {
                            Text("Evidence: \(context.source_ids.joined(separator: ", "))")
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(10)
                    .background(Color.secondary.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
            .padding(.top, 4)
        } label: {
            Label("Background evidence", systemImage: "brain.head.profile")
        }
    }

    @ViewBuilder
    private func sourceReferences(_ packet: TodayMeetingPacket) -> some View {
        let links = packet.source_links ?? []
        let questions = packet.open_questions ?? []
        if !links.isEmpty || questions.contains(where: { !$0.fact_ids.isEmpty }) {
            GroupBox {
                VStack(alignment: .leading, spacing: 9) {
                    ForEach(links) { link in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(link.label)
                                .font(.callout.weight(.medium))
                            Text(link.reference)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                    ForEach(questions.filter { !$0.fact_ids.isEmpty }) { question in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(question.question)
                                .font(.callout.weight(.medium))
                            Text("Related facts: \(question.fact_ids.joined(separator: ", "))")
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.top, 4)
            } label: {
                Label("Source references", systemImage: "number")
            }
        }
    }

    @ViewBuilder
    private func sourceLinkAction(_ link: TodayMeetingSourceLink) -> some View {
        let localRoute = link.brain_route.flatMap { route in
            BrainAPIClient.canLoadTodayEvidence(at: route) ? route : nil
        }
        let providerURL = safeMeetingSourceURL(link.provider_url)
        let wikiPath = link.wiki_path.flatMap { path in
            path.isEmpty ? nil : path
        }

        HStack(spacing: 10) {
            if let providerURL {
                Link(providerActionTitle(link), destination: providerURL)
                    .buttonStyle(.link)
                    .accessibilityLabel("\(providerActionTitle(link)): \(link.label)")
                    .help("Open the original source")
            }
            if let route = localRoute {
                Button(localActionTitle(link)) {
                    sourceEvidenceRequest = TodayEvidenceRequest(route: route)
                }
                .buttonStyle(.link)
                .accessibilityLabel("\(localActionTitle(link)): \(link.label)")
                .help("View the retained local evidence")
            }
            if let path = wikiPath {
                Button("Open in Wiki") {
                    dismiss()
                    appState.showWiki(path: path)
                }
                .buttonStyle(.link)
                .accessibilityLabel("Open \(link.label) in Wiki")
            }
            if providerURL == nil && localRoute == nil && wikiPath == nil {
                Text("Unavailable")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func providerActionTitle(_ link: TodayMeetingSourceLink) -> String {
        switch link.source_type {
        case "gmail": return "Open email"
        case "calendar": return "Open in Calendar"
        default: return "Open source"
        }
    }

    private func localActionTitle(_ link: TodayMeetingSourceLink) -> String {
        switch link.source_type {
        case "gmail": return "View local copy"
        case "calendar": return "View event details"
        default: return "View local evidence"
        }
    }

    private func scheduleText(_ context: TodayMeetingContext) -> String {
        guard let startsAt = context.starts_at else {
            return context.all_day ? "All day" : "Time not reported"
        }
        let start = displayDateTime(startsAt)
        if context.all_day {
            return "All day · \(start)"
        }
        guard let endsAt = context.ends_at else {
            return start
        }
        return "\(start) · ends \(displayDateTime(endsAt))"
    }

    private func calendarNotesPlaceholder(_ status: String) -> String {
        switch status {
        case "not_provided":
            return "No purpose or agenda was included in the Calendar description."
        case "source_unavailable":
            return "The retained Calendar description was unavailable, so this brief does not infer a purpose."
        default:
            return "No purpose or agenda was available."
        }
    }

    private func sourceLinkSymbol(_ source: String) -> String {
        switch source {
        case "calendar": return "calendar"
        case "gmail": return "envelope"
        case "wiki": return "doc.text"
        default: return "link"
        }
    }

    private func safeMeetingSourceURL(_ value: String?) -> URL? {
        guard let value, let url = URL(string: value),
              url.scheme == "https" || url.scheme == "http" else {
            return nil
        }
        return url
    }

    private func claimView(
        _ claim: TodayMeetingClaim,
        symbol: String,
        sourceLabel: String
    ) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Label(claim.claim, systemImage: symbol)
                .font(.callout)
            HStack(spacing: 8) {
                Text(sourceLabel)
                    .font(.caption.weight(.medium))
                if let confidence = claim.confidence {
                    Text("\(Int((min(max(confidence, 0), 1) * 100).rounded()))% confidence")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let factID = claim.fact_id, !factID.isEmpty {
                    Text(factID)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
            }
            if claim.evidence_refs.isEmpty {
                Label("No supporting evidence reference was returned.", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            } else {
                ForEach(Array(claim.evidence_refs.enumerated()), id: \.offset) { _, reference in
                    Text("Evidence: \(evidenceReference(reference))")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var unavailable: some View {
        ContentUnavailableView {
            Label("Meeting brief unavailable", systemImage: "exclamationmark.triangle")
        } description: {
            Text(errorMessage ?? "Brain could not prepare this meeting brief.")
        } actions: {
            Button("Try Again") {
                Task { await load() }
            }
        }
        .frame(maxWidth: .infinity, minHeight: 360)
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        packet = nil
        defer { isLoading = false }
        guard let client = await appState.waitForAPIClient() else {
            errorMessage = "The Brain service is not available."
            return
        }
        do {
            let loaded = try await client.todayMeetingPacket(itemID: request.itemID)
            guard loaded.schema_version == 1 else {
                errorMessage = "This meeting brief uses an unsupported format."
                return
            }
            guard loaded.item_id == request.itemID else {
                errorMessage = "Brain returned a meeting brief for a different event."
                return
            }
            packet = loaded
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func coverageLabel(_ source: String) -> String {
        switch source {
        case "calendar": return "Calendar"
        case "brain_retrieval": return "Brain context"
        case "gmail": return "Gmail"
        default: return source.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private func coveragePresentation(_ value: JSONValue?) -> (
        status: String,
        detail: String?,
        hasConcern: Bool
    ) {
        guard let value else {
            return ("Not reported", nil, true)
        }
        if let raw = value.stringValue {
            let status = friendlyStatus(raw)
            return (
                status,
                "A source freshness timestamp was not reported.",
                !isCompleteCoverage(raw)
            )
        }
        guard let object = value.objectValue else {
            return ("Unknown", nil, true)
        }
        let rawStatus = object["status"]?.stringValue
            ?? object["state"]?.stringValue
            ?? object["verdict"]?.stringValue
            ?? "unknown"
        let stale = object["stale"]?.boolValue == true
            || rawStatus.lowercased().contains("stale")
        var details: [String] = []
        if let detail = object["detail"]?.stringValue ?? object["reason"]?.stringValue,
           !detail.isEmpty {
            details.append(detail)
        }
        if let asOf = object["as_of"]?.stringValue ?? object["last_success_at"]?.stringValue {
            details.append("As of \(displayDateTime(asOf))")
        }
        if let age = object["age_seconds"]?.intValue {
            details.append("Age \(friendlyDuration(age))")
        }
        if stale {
            details.append("Source context is stale")
        }
        return (
            stale ? "Stale · \(friendlyStatus(rawStatus))" : friendlyStatus(rawStatus),
            details.isEmpty ? nil : details.joined(separator: " · "),
            stale || !isCompleteCoverage(rawStatus)
        )
    }

    private func isCompleteCoverage(_ value: String) -> Bool {
        ["complete", "found", "fresh", "available", "ok"].contains(value.lowercased())
    }

    private func friendlyStatus(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func friendlyDuration(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3_600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3_600)h" }
        return "\(seconds / 86_400)d"
    }

    private func evidenceReference(_ value: JSONValue) -> String {
        if let text = value.stringValue, !text.isEmpty {
            return text
        }
        if let object = value.objectValue {
            for key in ["source_ref", "source_id", "event_id", "message_id", "calendar_id"] {
                if let text = object[key]?.stringValue, !text.isEmpty {
                    return text
                }
            }
        }
        return "Local evidence reference"
    }
}

private struct TodayFeedbackDraft: Identifiable {
    let id = UUID()
    let item: TodayItem
    let action: String
}

private struct TodayFeedbackSheet: View {
    @Environment(\.dismiss) private var dismiss
    let draft: TodayFeedbackDraft
    let onSubmit: (String?, String?) -> Void
    @State private var note = ""
    @State private var snoozeUntil = Date().addingTimeInterval(60 * 60)

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(draft.action == "correct" ? "Correct item" : "Snooze item")
                .font(.title2.weight(.semibold))
            Text(draft.item.title)
                .font(.headline)
            if draft.action == "correct" {
                Text("Describe what is wrong or what the item should say.")
                    .foregroundStyle(.secondary)
            } else {
                DatePicker(
                    "Snooze until",
                    selection: $snoozeUntil,
                    displayedComponents: [.date, .hourAndMinute]
                )
            }
            TextEditor(text: $note)
                .frame(minHeight: 90)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Submit") {
                    let snoozed = draft.action == "snooze"
                        ? ISO8601DateFormatter().string(from: snoozeUntil)
                        : nil
                    onSubmit(note.trimmingCharacters(in: .whitespacesAndNewlines), snoozed)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    draft.action == "correct"
                        && note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                )
            }
        }
        .padding(20)
        .frame(width: 460)
    }
}

private struct TodayMissingItemSheet: View {
    @Environment(\.dismiss) private var dismiss
    let onSubmit: (String, String?, String?) -> Void
    @State private var title = ""
    @State private var detail = ""
    @State private var sourceHint = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Report a missing item")
                .font(.title2.weight(.semibold))
            TextField("What is missing?", text: $title)
            TextField("Likely source (optional)", text: $sourceHint)
            TextEditor(text: $detail)
                .frame(minHeight: 100)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Report") {
                    onSubmit(
                        title.trimmingCharacters(in: .whitespacesAndNewlines),
                        detail.trimmingCharacters(in: .whitespacesAndNewlines),
                        sourceHint.trimmingCharacters(in: .whitespacesAndNewlines)
                    )
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 460)
    }
}

private struct PulseChipView: View {
    let chip: PulseChip
    let detail: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Label(chip.label, systemImage: symbol)
                .font(.headline)
            Text(chip.value ?? "unknown")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, minHeight: 112, alignment: .topLeading)
        .background(background)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .help(detail ?? chip.value ?? chip.label)
    }

    private var symbol: String {
        switch chip.state {
        case "ok": return "checkmark.circle"
        case "warn": return "exclamationmark.circle"
        case "bad": return "xmark.octagon"
        default: return "circle"
        }
    }

    private var background: Color {
        switch chip.state {
        case "ok": return Color.green.opacity(0.12)
        case "warn": return Color.yellow.opacity(0.18)
        case "bad": return Color.red.opacity(0.12)
        default: return Color.secondary.opacity(0.08)
        }
    }
}
