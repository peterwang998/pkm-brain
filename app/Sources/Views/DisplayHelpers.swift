import Foundation
import SwiftUI

func countText(_ count: Int?, _ label: String) -> String? {
    guard let count else {
        return nil
    }
    return "\(count) \(label)"
}

func scoreText(_ value: Double?) -> String? {
    guard let value else {
        return nil
    }
    return String(format: "%.2f", value)
}

enum ConfidenceBand {
    case high
    case medium
    case low

    init(value: Double) {
        if value >= 0.85 {
            self = .high
        } else if value >= 0.65 {
            self = .medium
        } else {
            self = .low
        }
    }

    var label: String {
        switch self {
        case .high: return "High"
        case .medium: return "Medium"
        case .low: return "Low"
        }
    }

    var interval: String {
        switch self {
        case .high: return "85-100%"
        case .medium: return "65-84%"
        case .low: return "below 65%"
        }
    }

    var color: Color {
        switch self {
        case .high: return .green
        case .medium: return .orange
        case .low: return .red
        }
    }

    var symbol: String {
        switch self {
        case .high: return "checkmark.circle.fill"
        case .medium: return "minus.circle.fill"
        case .low: return "exclamationmark.circle.fill"
        }
    }
}

struct ConfidenceBadge: View {
    let value: Double?
    var compact = false

    @ViewBuilder var body: some View {
        if let value {
            let normalized = min(max(value, 0), 1)
            let band = ConfidenceBand(value: normalized)
            Label {
                Text(compact ? "\(Int((normalized * 100).rounded()))%" : "\(band.label) \(Int((normalized * 100).rounded()))%")
            } icon: {
                Image(systemName: band.symbol)
            }
            .font(.caption.weight(.medium))
            .foregroundStyle(band.color)
            .padding(.horizontal, compact ? 5 : 7)
            .padding(.vertical, 3)
            .background(band.color.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 5))
            .help("\(band.label) confidence (\(band.interval))")
            .accessibilityLabel("\(band.label) confidence, \(Int((normalized * 100).rounded())) percent")
        }
    }
}

struct RetrievalBadge: View {
    let count: Int?
    let lastRetrievedAt: String?
    var compact = false

    @ViewBuilder var body: some View {
        if let count {
            Label(
                compact ? "\(count)" : "\(count) retrieval\(count == 1 ? "" : "s")",
                systemImage: "text.magnifyingglass"
            )
            .font(.caption.monospacedDigit().weight(.medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, compact ? 5 : 7)
            .padding(.vertical, 3)
            .background(Color.secondary.opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 5))
            .help(retrievalHelp(count: count, lastRetrievedAt: lastRetrievedAt))
            .accessibilityLabel("Returned in \(count) distinct retrievals")
        }
    }

    private func retrievalHelp(count: Int, lastRetrievedAt: String?) -> String {
        guard let lastRetrievedAt, !lastRetrievedAt.isEmpty else {
            return "Returned in \(count) distinct retrievals"
        }
        return "Returned in \(count) distinct retrievals; latest \(String(lastRetrievedAt.prefix(10)))"
    }
}

struct SourceDateBadge: View {
    let value: String?
    let basis: String?

    var body: some View {
        Label(displayText, systemImage: value == nil ? "calendar.badge.exclamationmark" : "calendar")
            .font(.caption.weight(.medium))
            .foregroundStyle(value == nil ? Color.orange : Color.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background((value == nil ? Color.orange : Color.secondary).opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 5))
            .help(helpText)
            .accessibilityLabel(displayText)
    }

    private var displayText: String {
        guard let value, !value.isEmpty else {
            return "Source date unavailable"
        }
        return "Source date \(dateOnly(value))"
    }

    private var helpText: String {
        guard let value, !value.isEmpty else {
            return "No fact observation or source-document date is available."
        }
        let source = sourceDateBasisLabel(basis)
        return "\(source): \(value)"
    }
}

func dateOnly(_ value: String) -> String {
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    guard trimmed.count >= 10 else {
        return trimmed
    }
    let index = trimmed.index(trimmed.startIndex, offsetBy: 10)
    return String(trimmed[..<index])
}

func displayDateTime(_ value: String) -> String {
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let standard = ISO8601DateFormatter()
    guard let date = fractional.date(from: value) ?? standard.date(from: value) else {
        return value
    }
    return date.formatted(date: .abbreviated, time: .shortened)
}

private func sourceDateBasisLabel(_ basis: String?) -> String {
    switch basis {
    case "observed_at": return "Fact observation time"
    case "source_captured_at": return "Source capture time"
    case "source_created_at": return "Source creation time"
    case "source_ingested_at": return "Source ingestion time"
    default: return "Fact source date"
    }
}
