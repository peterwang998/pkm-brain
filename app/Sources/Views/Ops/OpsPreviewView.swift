import AppKit
import PKMBrainKit
import SwiftUI

struct OpsPreviewView: View {
    @EnvironmentObject private var appState: AppState
    @State private var section = "scheduler"
    @State private var scheduler: SchedulerState?
    @State private var runs: OpsRunsResponse?
    @State private var connectors: [ConnectorPayload] = []
    @State private var storage: StorageInventory?
    @State private var isLoading = false
    @State private var activeOperation: String?
    @State private var statusMessage: String?
    @State private var errorMessage: String?
    @State private var authConnector: ConnectorPayload?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Group {
                switch section {
                case "runs": runsView
                case "connectors": connectorsView
                case "storage": storageView
                default: schedulerView
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .task(id: appState.daemon.handshake?.pid) {
            scheduler = appState.daemon.scheduler
            await loadSection()
        }
        .onChange(of: section) { _, _ in
            Task { await loadSection() }
        }
        .toolbar {
            Button {
                Task { await loadSection(force: true) }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
        .sheet(item: $authConnector, onDismiss: {
            Task { await loadSection(force: true) }
        }) { connector in
            ConnectorAuthSheet(connector: connector)
                .environmentObject(appState)
        }
    }

    private var header: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Ops")
                    .font(.largeTitle.weight(.semibold))
                HStack(spacing: 8) {
                    Text(appState.daemon.status.label)
                    if let summary = appState.queueSummary {
                        Text("\(summary.actionable_total) review")
                        Text("\(summary.deferred_total) deferred")
                    }
                }
                .foregroundStyle(.secondary)
            }
            Spacer()
            Picker("Operations", selection: $section) {
                Label("Scheduler", systemImage: "clock.arrow.circlepath").tag("scheduler")
                Label("Runs", systemImage: "list.bullet.rectangle").tag("runs")
                Label("Connectors", systemImage: "point.3.connected.trianglepath.dotted").tag("connectors")
                Label("Storage", systemImage: "internaldrive").tag("storage")
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 620)
            .accessibilityIdentifier("ops-section-picker")
            if isLoading {
                ProgressView()
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 16)
    }

    private var schedulerView: some View {
        VStack(alignment: .leading, spacing: 12) {
            operationMessages
            HStack(spacing: 8) {
                Button {
                    Task { await pauseScheduler() }
                } label: {
                    Label("Pause 1 Hour", systemImage: "pause.circle")
                }
                Button {
                    Task { await resumeScheduler() }
                } label: {
                    Label("Resume", systemImage: "play.circle")
                }
                .disabled((scheduler ?? appState.daemon.scheduler)?.paused_until == nil)
                Spacer()
                if let paused = (scheduler ?? appState.daemon.scheduler)?.paused_until {
                    Text("Paused until \(paused)")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            if let jobs = (scheduler ?? appState.daemon.scheduler)?.jobs {
                Table(jobs) {
                    TableColumn("Job") { job in
                        Text(job.id)
                    }
                    TableColumn("Status") { job in
                        Text(job.displayStatus)
                    }
                    TableColumn("Last Run") { job in
                        Text(job.last_run_at ?? "-")
                    }
                    TableColumn("Next Due") { job in
                        Text(job.next_due_at ?? "-")
                    }
                    TableColumn("Detail") { job in
                        Text(job.statusDetail ?? "")
                            .lineLimit(2)
                    }
                    TableColumn("") { job in
                        Button {
                            Task { await runJob(job.id) }
                        } label: {
                            Image(systemName: "play.fill")
                        }
                        .buttonStyle(.borderless)
                        .help("Run \(job.id) now")
                        .disabled(activeOperation != nil || !job.enabled)
                    }
                    .width(36)
                }
            } else {
                ContentUnavailableView("Scheduler unavailable", systemImage: "clock.badge.exclamationmark")
            }
        }
        .padding(22)
    }

    private var runsView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                operationMessages
                runSection("Automation", runs?.automation_runs ?? [])
                runSection("Ingestion", runs?.ingestion_runs ?? [])
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var connectorsView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                operationMessages
                if connectors.isEmpty, !isLoading {
                    ContentUnavailableView("No connectors", systemImage: "point.3.connected.trianglepath.dotted")
                }
                ForEach(connectors) { connector in
                    HStack(alignment: .top, spacing: 14) {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 8) {
                                Image(systemName: connectorIcon(connector.id))
                                    .foregroundStyle(.secondary)
                                Text(connector.manifest.display_name)
                                    .font(.headline)
                                if let auth = connector.state.auth {
                                    ConnectorAuthBadge(status: auth.status)
                                }
                            }
                            Text(connector.manifest.description)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            MetadataRow(values: connectorMetadata(connector))
                            if let note = connector.manifest.activation_note, !note.isEmpty {
                                Text(note)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            if let error = connector.health.last_error, !error.isEmpty {
                                Text(error)
                                    .font(.callout)
                                    .foregroundStyle(.red)
                                    .textSelection(.enabled)
                            }
                        }
                        Spacer()
                        if connector.state.auth != nil {
                            Button {
                                authConnector = connector
                            } label: {
                                Label(
                                    connector.state.auth?.status == "connected" ? "Manage" : "Set Up",
                                    systemImage: connector.state.auth?.status == "connected" ? "key.fill" : "key"
                                )
                            }
                            .accessibilityIdentifier("connector-auth-\(connector.id)")
                            .disabled(activeOperation != nil)
                        }
                        if connector.manifest.canCapture {
                            Button {
                                Task { await toggleConnector(connector) }
                            } label: {
                                Image(systemName: connector.state.enabled ? "pause.circle" : "play.circle")
                            }
                            .help(connector.state.enabled ? "Disable connector" : "Enable connector")
                            .disabled(activeOperation != nil)
                            Button {
                                Task { await runConnector(connector) }
                            } label: {
                                Image(systemName: "arrow.clockwise")
                            }
                            .help("Run connector now")
                            .disabled(activeOperation != nil)
                        }
                    }
                    .padding(10)
                    .background(Color.secondary.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var storageView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                operationMessages
                if let storage {
                    LabeledContent("Managed roots", value: bytes(storage.managed_root_bytes))
                    storageSection("Roots", storage.roots)
                    storageSection("Details", storage.details)
                    Text("Measured \(storage.generated_at)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if !isLoading {
                    ContentUnavailableView("Storage inventory unavailable", systemImage: "internaldrive")
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var operationMessages: some View {
        if let statusMessage {
            Text(statusMessage)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        if let errorMessage {
            Text(errorMessage)
                .font(.callout)
                .foregroundStyle(.red)
                .textSelection(.enabled)
        }
    }

    private func runSection(_ title: String, _ items: [OpsRun]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.title3.weight(.semibold))
            if items.isEmpty {
                Text("No recorded runs.")
                    .foregroundStyle(.secondary)
            }
            ForEach(items.prefix(100)) { run in
                VStack(alignment: .leading, spacing: 4) {
                    Text(run.job_name ?? run.source_type ?? run.run_id ?? "Run")
                        .font(.callout.weight(.medium))
                    MetadataRow(values: [run.status, run.started_at, run.finished_at])
                    if let error = run.error, !error.isEmpty {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                }
                .padding(.vertical, 4)
                Divider()
            }
        }
    }

    private func storageSection(_ title: String, _ entries: [StorageEntry]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.title3.weight(.semibold))
            ForEach(entries) { entry in
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(entry.key.replacingOccurrences(of: "_", with: " ").capitalized)
                            .font(.callout.weight(.medium))
                        Text(entry.path)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .textSelection(.enabled)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(bytes(entry.bytes))
                            .monospacedDigit()
                        Text(entry.policy.replacingOccurrences(of: "_", with: " "))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Divider()
            }
        }
    }

    private func connectorMetadata(_ connector: ConnectorPayload) -> [String?] {
        if let auth = connector.state.auth {
            return [
                "authentication only",
                auth.account_label,
                auth.connected_at,
            ]
        }
        if connector.manifest.lifecycleStatus == "passive" {
            return ["passive inbox", connector.health.status]
        }
        return [
            connector.health.status,
            connector.health.last_run_at,
            connector.state.enabled ? "enabled" : "disabled",
            "every \(connector.state.cadence_s)s",
        ]
    }

    private func connectorIcon(_ connectorID: String) -> String {
        switch connectorID {
        case "gmail": "envelope"
        case "slack": "bubble.left.and.bubble.right"
        case "hyprnote": "waveform"
        case "files": "folder"
        case "codex", "claude", "opencode": "terminal"
        default: "point.3.connected.trianglepath.dotted"
        }
    }

    private func loadSection(force: Bool = false) async {
        guard let client = await appState.waitForAPIClient() else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            switch section {
            case "runs" where runs == nil || force:
                runs = try await client.opsRuns()
            case "connectors" where connectors.isEmpty || force:
                connectors = try await client.connectors().connectors
            case "storage" where storage == nil || force:
                storage = try await client.storageInventory()
            case "scheduler":
                scheduler = try await client.scheduler()
            default:
                break
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func runJob(_ id: String) async {
        await perform(id: "job:\(id)") { client in
            scheduler = try await client.runSchedulerJob(id)
            return "\(id) completed."
        }
    }

    private func pauseScheduler() async {
        await perform(id: "scheduler:pause") { client in
            scheduler = try await client.pauseScheduler(seconds: 3600)
            return "Scheduler paused for one hour."
        }
    }

    private func resumeScheduler() async {
        await perform(id: "scheduler:resume") { client in
            scheduler = try await client.resumeScheduler()
            return "Scheduler resumed."
        }
    }

    private func toggleConnector(_ connector: ConnectorPayload) async {
        await perform(id: "connector:\(connector.id)") { client in
            _ = try await client.setConnectorEnabled(
                connector.id,
                enabled: !connector.state.enabled
            )
            connectors = try await client.connectors().connectors
            return "\(connector.manifest.display_name) updated."
        }
    }

    private func runConnector(_ connector: ConnectorPayload) async {
        await perform(id: "connector-run:\(connector.id)") { client in
            _ = try await client.runConnector(connector.id)
            connectors = try await client.connectors().connectors
            return "\(connector.manifest.display_name) completed."
        }
    }

    private func perform(
        id: String,
        operation: (BrainAPIClient) async throws -> String
    ) async {
        guard let client = appState.daemon.apiClient else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        activeOperation = id
        defer { activeOperation = nil }
        do {
            statusMessage = try await operation(client)
            errorMessage = nil
            await appState.refreshDigest()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func bytes(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }
}

private struct ConnectorAuthBadge: View {
    let status: String

    var body: some View {
        Label(label, systemImage: symbol)
            .font(.caption.weight(.medium))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 5))
            .help(label)
    }

    private var label: String {
        switch status {
        case "connected": "Connected"
        case "ready": "Ready"
        case "authorizing": "Authorizing"
        case "reauthorization_required": "Reconnect"
        case "unavailable": "Unavailable"
        case "error": "Error"
        default: "Not configured"
        }
    }

    private var symbol: String {
        switch status {
        case "connected": "checkmark.circle.fill"
        case "ready": "key"
        case "authorizing": "clock.arrow.circlepath"
        case "reauthorization_required": "arrow.trianglehead.2.clockwise.rotate.90"
        case "unavailable", "error": "exclamationmark.triangle.fill"
        default: "key.slash"
        }
    }

    private var color: Color {
        switch status {
        case "connected": .green
        case "ready", "authorizing": .blue
        case "reauthorization_required": .orange
        case "unavailable", "error": .red
        default: .secondary
        }
    }
}

private struct ConnectorAuthSheet: View {
    @EnvironmentObject private var appState: AppState
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    @State private var connector: ConnectorPayload
    @State private var clientID: String
    @State private var clientSecret = ""
    @State private var isWorking = false
    @State private var statusMessage: String?
    @State private var errorMessage: String?
    @State private var pollTask: Task<Void, Never>?

    init(connector: ConnectorPayload) {
        _connector = State(initialValue: connector)
        _clientID = State(initialValue: connector.state.auth?.client_id ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if let manifest = connector.manifest.auth, let state = connector.state.auth {
                authForm(manifest: manifest, state: state)
            } else {
                ContentUnavailableView("Authentication unavailable", systemImage: "key.slash")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            footer
        }
        .frame(minWidth: 620, minHeight: 460)
        .accessibilityIdentifier("connector-auth-sheet-\(connector.id)")
        .onDisappear {
            pollTask?.cancel()
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: connectorIcon)
                .font(.title2)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(connector.manifest.display_name)
                    .font(.title2.weight(.semibold))
                Text("Connector Authentication")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let status = connector.state.auth?.status {
                ConnectorAuthBadge(status: status)
            }
        }
        .padding(20)
    }

    private func authForm(
        manifest: ConnectorAuthManifest,
        state: ConnectorAuthState
    ) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                LabeledContent("Access", value: "Identity only")
                if let account = state.account_label, !account.isEmpty {
                    LabeledContent("Account", value: account)
                }
                LabeledContent("Client ID") {
                    TextField("OAuth client ID", text: $clientID)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 380)
                }
                LabeledContent("Client secret") {
                    SecureField(
                        state.client_secret_configured ? "Stored in Keychain" : "OAuth client secret",
                        text: $clientSecret
                    )
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 380)
                }
                LabeledContent("Redirect URL") {
                    HStack(spacing: 8) {
                        Text(manifest.redirect_uri)
                            .font(.callout.monospaced())
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                        Button {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(manifest.redirect_uri, forType: .string)
                        } label: {
                            Image(systemName: "doc.on.doc")
                        }
                        .buttonStyle(.borderless)
                        .help("Copy redirect URL")
                    }
                }
                LabeledContent("Permissions") {
                    MetadataRow(values: manifest.requested_scopes.map(Optional.some))
                }
                if let statusMessage {
                    Label(statusMessage, systemImage: "info.circle")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if let detail = errorMessage ?? state.last_error, !detail.isEmpty {
                    Label(detail, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if let setupURL = connector.manifest.auth?.setup_url,
               let url = URL(string: setupURL) {
                Button {
                    openURL(url)
                } label: {
                    Label("Provider Console", systemImage: "arrow.up.right.square")
                }
            }
            Spacer()
            if connector.state.auth?.can_disconnect == true {
                Button(role: .destructive) {
                    Task { await disconnect() }
                } label: {
                    Label("Disconnect", systemImage: "link.badge.minus")
                }
                .disabled(isWorking)
            }
            Button("Done") {
                dismiss()
            }
            .keyboardShortcut(.cancelAction)
            Button {
                Task { await saveCredentials() }
            } label: {
                Label("Save", systemImage: "key")
            }
            .disabled(isWorking || !credentialsAreValid)
            Button {
                Task { await authorize() }
            } label: {
                Label(
                    connector.state.auth?.status == "connected" ? "Reconnect" : "Connect",
                    systemImage: "link"
                )
            }
            .buttonStyle(.borderedProminent)
            .disabled(isWorking || !credentialsAreValid)
        }
        .padding(16)
    }

    private var credentialsAreValid: Bool {
        guard !clientID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return false
        }
        guard connector.manifest.auth?.client_secret_required == true else {
            return true
        }
        return connector.state.auth?.client_secret_configured == true
            || !clientSecret.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var connectorIcon: String {
        switch connector.id {
        case "gmail": "envelope"
        case "slack": "bubble.left.and.bubble.right"
        default: "key"
        }
    }

    @MainActor
    private func saveCredentials() async {
        guard let client = await appState.waitForAPIClient() else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        isWorking = true
        defer { isWorking = false }
        do {
            connector = try await client.configureConnectorAuth(
                connector.id,
                clientID: clientID.trimmingCharacters(in: .whitespacesAndNewlines),
                clientSecret: normalizedSecret
            )
            clientSecret = ""
            statusMessage = "Credentials saved in Keychain."
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func authorize() async {
        guard let client = await appState.waitForAPIClient() else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        isWorking = true
        defer { isWorking = false }
        do {
            connector = try await client.configureConnectorAuth(
                connector.id,
                clientID: clientID.trimmingCharacters(in: .whitespacesAndNewlines),
                clientSecret: normalizedSecret
            )
            clientSecret = ""
            let started = try await client.startConnectorAuth(connector.id)
            connector = started.connector
            guard let url = URL(string: started.authorization_url) else {
                throw APIClientError.invalidResponse
            }
            openURL(url)
            statusMessage = "Authorization opened in your browser."
            errorMessage = nil
            startPolling(client)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func disconnect() async {
        guard let client = await appState.waitForAPIClient() else {
            errorMessage = "Daemon API is unavailable."
            return
        }
        isWorking = true
        defer { isWorking = false }
        do {
            pollTask?.cancel()
            connector = try await client.disconnectConnectorAuth(connector.id)
            statusMessage = "Connection removed from this Mac."
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @MainActor
    private func startPolling(_ client: BrainAPIClient) {
        pollTask?.cancel()
        let connectorID = connector.id
        pollTask = Task {
            for _ in 0..<150 {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled else { return }
                do {
                    let response = try await client.connectors()
                    guard let refreshed = response.connectors.first(where: { $0.id == connectorID }) else {
                        continue
                    }
                    connector = refreshed
                    if refreshed.state.auth?.status != "authorizing" {
                        statusMessage = refreshed.state.auth?.status == "connected"
                            ? "Authorization complete."
                            : statusMessage
                        return
                    }
                } catch {
                    continue
                }
            }
        }
    }

    private var normalizedSecret: String? {
        let value = clientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }
}
