import SwiftUI

struct MainWindowView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        NavigationSplitView {
            List(selection: $appState.selectedDestination) {
                ForEach(AppState.Destination.allCases) { destination in
                    Label(destination.rawValue, systemImage: destination.symbol)
                        .tag(destination)
                }
            }
            .navigationSplitViewColumnWidth(min: 180, ideal: 200)
        } detail: {
            VStack(spacing: 0) {
                MigrationAssistantView()
                Group {
                    switch appState.selectedDestination {
                    case .today:
                        TodayView()
                    case .queue:
                        PlaceholderView(title: "Queue", systemImage: "tray.full")
                    case .wiki:
                        PlaceholderView(title: "Wiki", systemImage: "doc.text")
                    case .entities:
                        PlaceholderView(title: "Entities", systemImage: "person.2")
                    case .ask:
                        PlaceholderView(title: "Ask", systemImage: "text.magnifyingglass")
                    case .ops:
                        OpsPreviewView()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(minWidth: 760, minHeight: 520)
        }
    }
}
