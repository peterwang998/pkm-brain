import PKMBrainKit
import SwiftUI

struct TodayView: View {
    @EnvironmentObject private var appState: AppState
    @State private var feedbackDraft: TodayFeedbackDraft?
    @State private var reportsMissingItem = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
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
                if let message = appState.todayShadowRunMessage {
                    Label(
                        message,
                        systemImage: appState.isRunningTodayShadow
                            ? "arrow.triangle.2.circlepath"
                            : "checkmark.circle"
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
                if let error = appState.todayShadowRunError {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(.callout)
                        .foregroundStyle(.red)
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
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Today")
                    .font(.largeTitle.weight(.semibold))
                Text(appState.daemon.status.label)
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
            onFeedback: beginFeedback,
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
                    Text("\(undisplayed) additional urgent item(s) are disclosed but not expanded.")
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
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Waiting",
            symbol: "hourglass",
            items: briefing.waiting,
            emptyText: "Nothing is currently waiting on another person.",
            feedback: briefing.feedback,
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Attention",
            symbol: "bell.badge",
            items: briefing.attention,
            emptyText: "No additional attention items.",
            feedback: briefing.feedback,
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Awareness",
            symbol: "eye",
            items: briefing.awareness,
            emptyText: "No awareness-only updates.",
            feedback: briefing.feedback,
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Uncertain",
            subtitle: "Visible because incomplete evidence must not become an all-clear.",
            symbol: "questionmark.diamond",
            items: briefing.uncertain,
            emptyText: "No uncertain items surfaced.",
            feedback: briefing.feedback,
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
        TodayItemSection(
            title: "Ignored & suppressed audit",
            subtitle: "Why an item was withheld from focus, with reversible reason codes.",
            symbol: "checklist.unchecked",
            items: briefing.ignored_suppressed,
            emptyText: "No ignored or suppressed items were recorded.",
            feedback: briefing.feedback,
            audit: true,
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
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
            onFeedback: beginFeedback,
            onBrainRoute: appState.openTodayBrainRoute
        )
    }

    private func beginFeedback(_ item: TodayItem, _ action: String) {
        if action == "correct" || action == "snooze" {
            feedbackDraft = TodayFeedbackDraft(item: item, action: action)
            return
        }
        Task {
            await appState.submitTodayFeedback(itemID: item.id, action: action)
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
    let onFeedback: (TodayItem, String) -> Void
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
                        onFeedback: onFeedback,
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
    let onFeedback: (TodayItem, String) -> Void
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
                                Label(evidence.label, systemImage: "brain.head.profile")
                            }
                            .buttonStyle(.link)
                            .help("Open local evidence · \(evidence.reference)")
                        }
                        if let url = safeProviderURL(evidence.provider_url) {
                            Link(destination: url) {
                                Label("Source", systemImage: "arrow.up.right.square")
                            }
                            .buttonStyle(.link)
                            .help("Open provider source · \(evidence.reference)")
                        }
                    }
                }
                .font(.caption)
            }
            if !item.feedback_actions.isEmpty {
                Menu {
                    ForEach(item.feedback_actions, id: \.self) { action in
                        Button {
                            onFeedback(item, action)
                        } label: {
                            Label(feedbackTitle(action), systemImage: feedbackSymbol(action))
                        }
                        .disabled(!feedback.allows(action))
                    }
                } label: {
                    Label("Update", systemImage: "ellipsis.circle")
                }
                .disabled(!feedback.enabled)
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
        case "restore": return "arrow.uturn.backward"
        default: return "circle"
        }
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
