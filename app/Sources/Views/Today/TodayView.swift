import PKMBrainKit
import SwiftUI

struct TodayView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                pulseGrid
                needsYou
                recentFacts
                if let error = appState.lastError {
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
                Task { await appState.refreshDigest() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("Today")
                    .font(.largeTitle.weight(.semibold))
                Text(appState.daemon.status.label)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let generated = appState.digest?.generated_at {
                Text(generated)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var pulseGrid: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 12)], alignment: .leading, spacing: 12) {
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
            Text("Needs You")
                .font(.title2.weight(.semibold))
            if let counts = appState.digest?.queue_counts, counts.total > 0 {
                HStack(spacing: 10) {
                    ForEach(counts.by_kind.sorted(by: { $0.key < $1.key }), id: \.key) { kind, count in
                        Label("\(kind) \(count)", systemImage: "circle.fill")
                            .labelStyle(.titleAndIcon)
                            .font(.callout)
                    }
                }
                Button {
                    appState.selectedDestination = .queue
                } label: {
                    Label("Start Review", systemImage: "arrow.right.circle")
                }
            } else {
                Text("No review items in the current digest.")
                    .foregroundStyle(.secondary)
            }
        }
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
