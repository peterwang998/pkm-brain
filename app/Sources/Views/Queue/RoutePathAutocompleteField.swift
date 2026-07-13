import PKMBrainKit
import SwiftUI

struct RoutePathAutocompleteField: View {
    @EnvironmentObject private var appState: AppState
    @Binding var pageHint: String
    @Binding var isFocused: Bool
    let shortcutKey: String
    let onSubmit: () -> Void

    @State private var pages: [WikiPageSummary] = []
    @FocusState private var fieldFocused: Bool

    private var suggestions: [WikiPageSummary] {
        RoutePathMatcher.suggestions(in: pages, matching: pageHint, limit: 6)
    }

    private var trimmedPageHint: String {
        pageHint.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                TextField("concepts/topic.md", text: $pageHint)
                    .textFieldStyle(.roundedBorder)
                    .focused($fieldFocused)
                    .onSubmit(onSubmit)
                Button(action: onSubmit) {
                    Label {
                        HStack(spacing: 7) {
                            Text("Route")
                            Text(shortcutKey)
                                .font(.caption2.monospacedDigit())
                                .padding(.horizontal, 5)
                                .padding(.vertical, 2)
                                .background(.quaternary)
                                .clipShape(RoundedRectangle(cornerRadius: 4))
                        }
                    } icon: {
                        Image(systemName: "arrow.turn.down.right")
                    }
                }
                .queueKeyboardShortcut(fieldFocused ? "" : shortcutKey)
                .disabled(trimmedPageHint.isEmpty)
            }
            if fieldFocused, !suggestions.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(suggestions) { page in
                        Button {
                            pageHint = page.relative_path
                            fieldFocused = true
                        } label: {
                            HStack(spacing: 8) {
                                Text(page.displayTitle)
                                    .lineLimit(1)
                                Spacer(minLength: 12)
                                Text(page.relative_path)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            .contentShape(Rectangle())
                            .padding(.horizontal, 8)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(3)
                .background(.background)
                .overlay {
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(.separator, lineWidth: 1)
                }
            }
        }
        .task(id: appState.daemon.handshake?.pid) {
            guard let client = await appState.waitForAPIClient() else {
                return
            }
            pages = (try? await client.routableWikiPages().pages) ?? []
        }
        .onChange(of: fieldFocused) { _, focused in
            isFocused = focused
        }
        .onChange(of: isFocused) { _, focused in
            fieldFocused = focused
        }
    }
}
