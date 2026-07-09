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
                        QueueView()
                    case .wiki:
                        WikiView()
                    case .entities:
                        EntitiesView()
                    case .ask:
                        AskView()
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
