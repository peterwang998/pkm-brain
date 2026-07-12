import SwiftUI

struct MenuBarContent: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(appState.daemon.status.label)
                .font(.headline)
            if let latest = appState.digest?.latest_run {
                Label(latest.status ?? "nightly", systemImage: "moon")
                Text(latest.finished_at ?? latest.started_at ?? "No run timestamp")
                    .foregroundStyle(.secondary)
            }
            if appState.queueTotal > 0 {
                Button("Review \(appState.queueTotal) Items") {
                    appState.showQueue()
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
            if let deferred = appState.queueSummary?.deferred_total, deferred > 0 {
                Button("View \(deferred) Deferred") {
                    appState.showQueue(state: "deferred")
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
            Divider()
            Button("Run Capture Now") {
                Task { await appState.runCaptureNow() }
            }
            Button("Pause Jobs for 1 Hour") {
                Task { await appState.pauseOneHour() }
            }
            Button("Resume Jobs") {
                Task { await appState.resumeScheduler() }
            }
            Divider()
            Button("Open PKM Brain") {
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
            Button("Quit") {
                NSApp.terminate(nil)
            }
        }
        .padding(.vertical, 4)
        .frame(width: 240, alignment: .leading)
    }
}
