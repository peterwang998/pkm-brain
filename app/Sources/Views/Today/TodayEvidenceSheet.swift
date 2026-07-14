import PKMBrainKit
import SwiftUI

struct TodayEvidenceSheet: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var appState: AppState
    let request: TodayEvidenceRequest

    @State private var document: TodayRetainedEvidence?
    @State private var errorMessage: String?
    @State private var isLoading = true

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                Group {
                    if isLoading {
                        ProgressView("Loading retained evidence…")
                            .frame(maxWidth: .infinity, minHeight: 300)
                    } else if let document {
                        evidence(document)
                    } else {
                        unavailable
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 680, idealWidth: 760, minHeight: 520, idealHeight: 680)
        .task(id: request.id) {
            await load()
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "lock.doc")
                .font(.title2)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(document?.displayTitle ?? "Retained local evidence")
                    .font(.title2.weight(.semibold))
                    .lineLimit(2)
                Text(document?.sourceLabel ?? "Private normalized source copy")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Done") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(18)
    }

    @ViewBuilder
    private func evidence(_ document: TodayRetainedEvidence) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            sourceMetadata(document)
            if document.source_type == "gmail" {
                gmailEvidence(document)
            } else if document.source_type == "calendar" {
                calendarEvidence(document)
            } else {
                fallbackEvidence(document.evidence)
            }
            Label(
                "This normalized copy is stored only in Brain on this Mac for up to \(document.retention_days) days.",
                systemImage: "lock.shield"
            )
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .textSelection(.enabled)
    }

    private func sourceMetadata(_ document: TodayRetainedEvidence) -> some View {
        GroupBox("Source") {
            VStack(alignment: .leading, spacing: 8) {
                LabeledContent("Provider", value: document.sourceLabel)
                LabeledContent("Source reference") {
                    Text(document.source_ref)
                        .font(.caption.monospaced())
                        .multilineTextAlignment(.trailing)
                }
                if let revision = document.source_revision, !revision.isEmpty {
                    LabeledContent("Version") {
                        Text(revision)
                            .font(.caption.monospaced())
                            .multilineTextAlignment(.trailing)
                    }
                }
                LabeledContent("Local retention", value: "Up to \(document.retention_days) days")
            }
            .padding(.top, 4)
        }
    }

    @ViewBuilder
    private func calendarEvidence(_ document: TodayRetainedEvidence) -> some View {
        let object = document.evidence.objectValue ?? [:]
        VStack(alignment: .leading, spacing: 16) {
            Text(object.string("title") ?? "Calendar event")
                .font(.title3.weight(.semibold))
            GroupBox("Event details") {
                VStack(alignment: .leading, spacing: 8) {
                    if let status = object.string("status") {
                        LabeledContent("Status", value: friendlyStatus(status))
                    }
                    if let start = object.string("starts_at") ?? object.string("start_date") {
                        LabeledContent("Starts", value: friendlyDate(start))
                    }
                    if let end = object.string("ends_at") ?? object.string("end_date") {
                        LabeledContent("Ends", value: friendlyDate(end))
                    }
                    if let location = object.string("location") {
                        LabeledContent("Location", value: location)
                    }
                    if let organizer = object.string("organizer_email") {
                        LabeledContent("Organizer", value: organizer)
                    }
                    if let response = object.string("attendee_response") {
                        LabeledContent("Your response", value: friendlyStatus(response))
                    }
                    if let attendees = object.int("attendee_count") {
                        LabeledContent("Attendees", value: String(attendees))
                    }
                    if let updated = object.string("updated_at") {
                        LabeledContent("Last changed", value: friendlyDate(updated))
                    }
                    if object.bool("cancelled") == true {
                        Label("This event was cancelled.", systemImage: "calendar.badge.minus")
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 4)
            }
            if let details = object.string("details"), !details.isEmpty {
                evidenceText(title: "Description", text: details)
            }
            if object.string("recurring_event_id") != nil {
                Label("This is one occurrence of a recurring event.", systemImage: "repeat")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func gmailEvidence(_ document: TodayRetainedEvidence) -> some View {
        let object = document.evidence.objectValue ?? [:]
        let messages = object.array("messages")
        VStack(alignment: .leading, spacing: 16) {
            Text(object.string("subject") ?? "Email thread")
                .font(.title3.weight(.semibold))
            GroupBox("Thread details") {
                VStack(alignment: .leading, spacing: 8) {
                    LabeledContent("Messages", value: String(messages.count))
                    if let updated = object.string("updated_at") {
                        LabeledContent("Latest message", value: friendlyDate(updated))
                    }
                    if let messageClass = object.string("message_class") {
                        LabeledContent("Type", value: friendlyStatus(messageClass))
                    }
                    if let attachmentCount = object.int("attachment_count"), attachmentCount > 0 {
                        LabeledContent("Attachments mentioned", value: String(attachmentCount))
                    }
                    if object.bool("truncated") == true {
                        Label("Long message text was shortened in this normalized copy.", systemImage: "text.badge.minus")
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 4)
            }
            ForEach(Array(messages.enumerated()), id: \.offset) { index, value in
                gmailMessage(value.objectValue ?? [:], number: index + 1)
            }
            if let attachmentCount = object.int("attachment_count"), attachmentCount > 0 {
                Label("Attachments were not downloaded or included in this evidence.", systemImage: "paperclip.badge.ellipsis")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func gmailMessage(_ message: [String: JSONValue], number: Int) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline) {
                    Text(senderLabel(message))
                        .font(.headline)
                    Spacer()
                    if let timestamp = message.string("timestamp") ?? message.string("date_header") {
                        Text(friendlyDate(timestamp))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                if let recipients = addressList(message, key: "to_addresses") {
                    Text("To: \(recipients)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                let body = message.string("body") ?? ""
                if body.isEmpty {
                    Text("No retained message text.")
                        .italic()
                        .foregroundStyle(.secondary)
                } else {
                    Text(body)
                        .font(.body)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 12) {
                    if let removed = message.int("quoted_chars_removed"), removed > 0 {
                        Label("Earlier quoted text removed", systemImage: "quote.bubble")
                    }
                    if message.bool("truncated") == true {
                        Label("Shortened", systemImage: "text.badge.minus")
                    }
                    if let count = message.int("attachment_count"), count > 0 {
                        Label("\(count) attachment\(count == 1 ? "" : "s") not fetched", systemImage: "paperclip")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .padding(.top, 4)
        } label: {
            Text("Message \(number)")
        }
    }

    private func evidenceText(title: String, text: String) -> some View {
        GroupBox(title) {
            Text(text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
        }
    }

    private func fallbackEvidence(_ value: JSONValue) -> some View {
        GroupBox("Normalized evidence") {
            Text(prettyJSON(value))
                .font(.body.monospaced())
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
        }
    }

    private var unavailable: some View {
        ContentUnavailableView {
            Label("Evidence unavailable", systemImage: "doc.questionmark")
        } description: {
            Text(errorMessage ?? "The retained local copy may have expired.")
        }
        .frame(maxWidth: .infinity, minHeight: 300)
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        guard let client = await appState.waitForAPIClient() else {
            errorMessage = "The Brain service is not available."
            return
        }
        do {
            let loaded = try await client.todayRetainedEvidence(at: request.route)
            guard loaded.schema_version == 1 else {
                errorMessage = "This evidence uses an unsupported format."
                return
            }
            document = loaded
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func senderLabel(_ message: [String: JSONValue]) -> String {
        if message.bool("operator_authored") == true || message.bool("outgoing") == true {
            return "You"
        }
        return addressList(message, key: "from_addresses") ?? "Unknown sender"
    }

    private func addressList(_ object: [String: JSONValue], key: String) -> String? {
        let values = object.array(key).compactMap(\.stringValue)
        return values.isEmpty ? nil : values.joined(separator: ", ")
    }

    private func friendlyStatus(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func friendlyDate(_ value: String) -> String {
        if value.count == 10, value[value.index(value.startIndex, offsetBy: 4)] == "-" {
            return value
        }
        return displayDateTime(value)
    }

    private func prettyJSON(_ value: JSONValue) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let object = try? JSONSerialization.jsonObject(with: data),
              let pretty = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: pretty, encoding: .utf8)
        else {
            return "Evidence could not be formatted."
        }
        return text
    }
}

private extension Dictionary where Key == String, Value == JSONValue {
    func string(_ key: String) -> String? {
        guard let value = self[key]?.stringValue,
              !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else {
            return nil
        }
        return value
    }

    func int(_ key: String) -> Int? {
        self[key]?.intValue
    }

    func bool(_ key: String) -> Bool? {
        self[key]?.boolValue
    }

    func array(_ key: String) -> [JSONValue] {
        self[key]?.arrayValue ?? []
    }
}
