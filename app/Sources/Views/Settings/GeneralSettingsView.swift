import AppKit
import ServiceManagement
import PKMBrainKit
import SwiftUI

struct GeneralSettingsView: View {
    @EnvironmentObject private var appState: AppState
    @State private var loginItemError: String?
    @State private var curationSettings: CurationSettingsResponse?
    @State private var selectedStrictness = "balanced"
    @State private var selectedTopologyBias = 0.5
    @State private var selectedTopologyReviewThreshold = 8
    @State private var curationError: String?
    @State private var curationStatus: String?
    @State private var isLoadingCuration = false
    @State private var isSavingCuration = false
    @State private var draftHomePath = ""
    @State private var showHomeConfirmation = false
    @State private var isSwitchingHome = false
    @State private var homeSwitchStatus: String?

    let showsHeader: Bool

    init(showsHeader: Bool = false) {
        self.showsHeader = showsHeader
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if showsHeader {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Settings")
                        .font(.largeTitle.weight(.semibold))
                    if let savedText = savedSettingsText(curationSettings?.updated_at) {
                        Text(savedText)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 24)
                .padding(.top, 18)
                .padding(.bottom, 8)
            }
            Form {
                autonomySection
                Section("General") {
                    LabeledContent("Brain Home") {
                        HStack(spacing: 8) {
                            TextField("Brain Home", text: $draftHomePath)
                                .textFieldStyle(.roundedBorder)
                            Button {
                                chooseHome()
                            } label: {
                                Image(systemName: "folder")
                            }
                            .help("Choose Brain Home")
                        }
                    }
                    LabeledContent("Active Home", value: appState.homePath)
                    HStack(spacing: 8) {
                        Button {
                            showHomeConfirmation = true
                        } label: {
                            if isSwitchingHome {
                                Label("Switching", systemImage: "arrow.triangle.2.circlepath")
                            } else {
                                Label("Apply Home", systemImage: "checkmark.circle")
                            }
                        }
                        .disabled(
                            isSwitchingHome
                                || normalizedHome(draftHomePath) == normalizedHome(appState.homePath)
                                || !draftHomePath.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("/")
                        )
                        if isSwitchingHome {
                            ProgressView()
                                .controlSize(.small)
                        }
                        if let homeSwitchStatus {
                            Text(homeSwitchStatus)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                    }
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
                    LabeledContent("Status", value: appState.daemon.status.label)
                    if let handshake = appState.daemon.handshake {
                        LabeledContent("Port", value: String(handshake.port))
                        LabeledContent("PID", value: String(handshake.pid))
                    }
                    Button {
                        Task {
                            await appState.shutdown()
                            await appState.start()
                        }
                    } label: {
                        Label("Restart Daemon", systemImage: "arrow.clockwise")
                    }
                }
            }
            .formStyle(.grouped)
        }
        .frame(minWidth: 560, maxWidth: 760, minHeight: 500, alignment: .topLeading)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .onAppear {
            appState.loginItemEnabled = SMAppService.mainApp.status == .enabled
            draftHomePath = appState.homePath
        }
        .onChange(of: appState.homePath) { _, value in
            if !isSwitchingHome {
                draftHomePath = value
            }
        }
        .task(id: appState.daemon.handshake?.pid) {
            await loadCurationSettings()
        }
        .alert("Switch Brain Home?", isPresented: $showHomeConfirmation) {
            Button("Cancel", role: .cancel) {}
            Button("Switch") {
                Task { await applyHomeSwitch() }
            }
        } message: {
            Text("The daemon will validate \(draftHomePath) and return to \(appState.homePath) if startup fails.")
        }
    }

    private var autonomySection: some View {
        Section("Autonomy & Topology") {
            Picker("Mode", selection: $selectedStrictness) {
                ForEach(profiles) { profile in
                    Text(profile.label).tag(profile.id)
                }
            }
            .pickerStyle(.segmented)
            .disabled(isLoadingCuration || isSavingCuration)

            LabeledContent(
                "Auto-management floor",
                value: minimumConfidence.map { "\(Int(($0 * 100).rounded()))%" } ?? "-"
            )
            LabeledContent("Applies to", value: "Future actions")
            LabeledContent("Existing queue", value: "Unchanged")
            LabeledContent("Hard review", value: "Contradictions and unsafe topology")

            Divider()

            topologyBiasSlider
            topologyReviewThresholdControl
            LabeledContent("Topology scope", value: "Future gardener jobs")

            HStack(spacing: 10) {
                Button {
                    Task { await saveCurationSettings() }
                } label: {
                    if isSavingCuration {
                        Label("Applying", systemImage: "arrow.clockwise")
                    } else {
                        Label("Apply Settings", systemImage: "checkmark.circle")
                    }
                }
                .disabled(
                    isLoadingCuration
                        || isSavingCuration
                        || !curationSettingsChanged
                )
                if isLoadingCuration {
                    ProgressView()
                        .controlSize(.small)
                }
                if let curationStatus {
                    Text(curationStatus)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            if let curationError {
                Text(curationError)
                    .font(.callout)
                    .foregroundStyle(.red)
            }
        }
    }

    private var profiles: [CurationProfile] {
        curationSettings?.profiles ?? [
            CurationProfile(id: "strict", label: "Review First", minimum_auto_confidence: 0.95),
            CurationProfile(id: "balanced", label: "Balanced", minimum_auto_confidence: 0.80),
            CurationProfile(id: "lenient", label: "More Autonomy", minimum_auto_confidence: 0.60),
        ]
    }

    private var minimumConfidence: Double? {
        profiles.first(where: { $0.id == selectedStrictness })?.minimum_auto_confidence
    }

    private var curationSettingsChanged: Bool {
        guard let settings = curationSettings else {
            return false
        }
        return selectedStrictness != settings.strictness
            || abs(selectedTopologyBias - topologyBias(for: settings)) > 0.001
            || selectedTopologyReviewThreshold != settings.topology_review_threshold
    }

    private var topologyBiasSlider: some View {
        LabeledContent {
            VStack(alignment: .trailing, spacing: 6) {
                Text(topologyBiasLabel)
                    .foregroundStyle(.secondary)
                Slider(value: $selectedTopologyBias, in: 0...1, step: 0.1)
                    .accessibilityLabel("Topology bias")
                    .accessibilityValue(topologyBiasLabel)
                HStack(spacing: 0) {
                    Text("Prefer splits")
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text("Balanced")
                        .frame(maxWidth: .infinity, alignment: .center)
                    Text("Prefer merges")
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
        } label: {
            Text("Topology bias")
        }
    }

    private var topologyBiasLabel: String {
        if selectedTopologyBias < 0.45 {
            return "Split leaning"
        }
        if selectedTopologyBias > 0.55 {
            return "Merge leaning"
        }
        return "Balanced"
    }

    private var topologyReviewThresholdControl: some View {
        LabeledContent("Topology review threshold") {
            Stepper(
                "\(selectedTopologyReviewThreshold)+ affected facts/pages",
                value: $selectedTopologyReviewThreshold,
                in: 4...200,
                step: 4
            )
            .disabled(isLoadingCuration || isSavingCuration)
            .help("Higher values send fewer otherwise-safe topology actions to review.")
        }
    }

    private func topologyBias(for settings: CurationSettingsResponse) -> Double {
        let bias = (settings.merge_aggressiveness + (1 - settings.split_aggressiveness)) / 2
        return min(1, max(0, bias))
    }

    private func loadCurationSettings() async {
        isLoadingCuration = true
        defer { isLoadingCuration = false }
        guard let client = await appState.waitForAPIClient() else {
            if !Task.isCancelled {
                curationError = "Daemon API is unavailable."
            }
            return
        }
        do {
            let settings = try await client.curationSettings()
            curationSettings = settings
            selectedStrictness = settings.strictness
            selectedTopologyBias = topologyBias(for: settings)
            selectedTopologyReviewThreshold = settings.topology_review_threshold
            curationError = nil
        } catch {
            curationError = error.localizedDescription
        }
    }

    private func saveCurationSettings() async {
        guard let client = appState.daemon.apiClient else {
            curationError = "Daemon API is unavailable."
            return
        }
        isSavingCuration = true
        defer { isSavingCuration = false }
        do {
            let settings = try await client.updateCurationSettings(
                strictness: selectedStrictness,
                mergeAggressiveness: selectedTopologyBias,
                splitAggressiveness: 1 - selectedTopologyBias,
                topologyReviewThreshold: selectedTopologyReviewThreshold
            )
            curationSettings = settings
            selectedStrictness = settings.strictness
            selectedTopologyBias = topologyBias(for: settings)
            selectedTopologyReviewThreshold = settings.topology_review_threshold
            curationStatus = changesSavedText(settings.updated_at)
            curationError = nil
        } catch {
            curationError = error.localizedDescription
        }
    }

    private func savedSettingsText(_ value: String?) -> String? {
        guard let date = settingsDate(value) else {
            return nil
        }
        return "Last saved \(date.formatted(date: .abbreviated, time: .shortened))"
    }

    private func changesSavedText(_ value: String?) -> String {
        guard let date = settingsDate(value) else {
            return "Changes saved"
        }
        return "Changes saved at \(date.formatted(date: .omitted, time: .shortened))"
    }

    private func settingsDate(_ value: String?) -> Date? {
        guard let value else {
            return nil
        }
        return ISO8601DateFormatter().date(from: value)
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

    private func chooseHome() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: normalizedHome(draftHomePath))
        if panel.runModal() == .OK, let url = panel.url {
            draftHomePath = url.standardizedFileURL.path
            homeSwitchStatus = nil
        }
    }

    private func applyHomeSwitch() async {
        isSwitchingHome = true
        defer { isSwitchingHome = false }
        let target = normalizedHome(draftHomePath)
        if await appState.switchHome(to: target) {
            draftHomePath = appState.homePath
            homeSwitchStatus = "Home switched."
        } else {
            draftHomePath = appState.homePath
            homeSwitchStatus = appState.lastError ?? "Home switch failed."
        }
    }

    private func normalizedHome(_ value: String) -> String {
        URL(
            fileURLWithPath: NSString(string: value).expandingTildeInPath,
            isDirectory: true
        ).standardizedFileURL.path
    }
}
