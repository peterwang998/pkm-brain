import AppKit
import SwiftUI

@main
struct PKMBrainApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup("PKM Brain", id: "main") {
            MainWindowView()
                .environmentObject(appState)
                .task {
                    appDelegate.appState = appState
                    await appState.start()
                }
        }
        .commands {
            CommandMenu("Brain") {
                Button("Refresh Today") {
                    Task { await appState.refreshDigest() }
                }
                .keyboardShortcut("r", modifiers: [.command])
                Button("Run Capture Now") {
                    Task { await appState.runCaptureNow() }
                }
            }
        }

        Settings {
            GeneralSettingsView()
                .environmentObject(appState)
        }

        MenuBarExtra("PKM Brain", systemImage: appState.menuBarSymbol) {
            MenuBarContent()
                .environmentObject(appState)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor weak var appState: AppState?
    private var isTerminating = false

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if isTerminating {
            return .terminateNow
        }
        isTerminating = true
        Task { @MainActor in
            await appState?.shutdown()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
