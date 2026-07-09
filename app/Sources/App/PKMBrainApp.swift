import AppKit
import ServiceManagement
import SwiftUI
@preconcurrency import UserNotifications

@main
struct PKMBrainApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var appState = AppState()

    init() {
        let arguments = Set(CommandLine.arguments.dropFirst())
        if arguments.contains("--login-item-status") {
            print(Self.loginItemStatusLabel())
            Foundation.exit(EXIT_SUCCESS)
        }
        if arguments.contains("--enable-login-item") {
            Self.setLoginItem(enabled: true)
        }
        if arguments.contains("--disable-login-item") {
            Self.setLoginItem(enabled: false)
        }
    }

    var body: some Scene {
        WindowGroup("PKM Brain", id: "main") {
            MainWindowView()
                .environmentObject(appState)
                .task {
                    appDelegate.appState = appState
                    NotificationRouter.shared.appState = appState
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

    private static func setLoginItem(enabled: Bool) -> Never {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            print(loginItemStatusLabel())
            Foundation.exit(EXIT_SUCCESS)
        } catch {
            fputs("\(error)\n", stderr)
            Foundation.exit(EXIT_FAILURE)
        }
    }

    private static func loginItemStatusLabel() -> String {
        switch SMAppService.mainApp.status {
        case .enabled:
            return "enabled"
        case .notRegistered:
            return "notRegistered"
        case .requiresApproval:
            return "requiresApproval"
        case .notFound:
            return "notFound"
        @unknown default:
            return "unknown"
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    @MainActor weak var appState: AppState?
    private var isTerminating = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
    }

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

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        guard let destination = response.notification.request.content.userInfo["destination"] as? String else {
            return
        }
        await NotificationRouter.shared.open(destination: destination)
    }
}

@MainActor
final class NotificationRouter {
    static let shared = NotificationRouter()
    weak var appState: AppState?

    func open(destination: String) {
        switch destination {
        case "queue":
            appState?.selectedDestination = .queue
        case "ops":
            appState?.selectedDestination = .ops
        case "today":
            appState?.selectedDestination = .today
        default:
            break
        }
        NSApp.activate(ignoringOtherApps: true)
    }
}
