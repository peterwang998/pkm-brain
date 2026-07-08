import ServiceManagement
import SwiftUI

struct GeneralSettingsView: View {
    @EnvironmentObject private var appState: AppState
    @State private var loginItemError: String?

    var body: some View {
        Form {
            Section("General") {
                TextField("Brain Home", text: $appState.homePath)
                    .textFieldStyle(.roundedBorder)
                Toggle("Serve browser fallback UI", isOn: $appState.serveWeb)
                Toggle("Notifications", isOn: $appState.notificationsEnabled)
                Toggle("Open at Login", isOn: Binding(
                    get: { appState.loginItemEnabled },
                    set: { enabled in
                        appState.loginItemEnabled = enabled
                        updateLoginItem(enabled)
                    }
                ))
                if let loginItemError {
                    Text(loginItemError)
                        .foregroundStyle(.red)
                }
            }
            Section("Daemon") {
                Text(appState.daemon.status.label)
                if let handshake = appState.daemon.handshake {
                    LabeledContent("Port", value: String(handshake.port))
                    LabeledContent("PID", value: String(handshake.pid))
                }
                Button("Restart Daemon") {
                    Task {
                        await appState.shutdown()
                        await appState.start()
                    }
                }
            }
        }
        .padding(24)
        .frame(width: 520)
        .onAppear {
            appState.loginItemEnabled = SMAppService.mainApp.status == .enabled
        }
    }

    private func updateLoginItem(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            loginItemError = nil
        } catch {
            loginItemError = String(describing: error)
        }
    }
}
