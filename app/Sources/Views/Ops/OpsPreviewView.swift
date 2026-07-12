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
                            Text(connector.manifest.display_name)
                                .font(.headline)
                            MetadataRow(values: [
                                connector.health.status,
                                connector.health.last_run_at,
                                connector.state.enabled ? "enabled" : "disabled",
                                "every \(connector.state.cadence_s)s",
                            ])
                            if let error = connector.health.last_error, !error.isEmpty {
                                Text(error)
                                    .font(.callout)
                                    .foregroundStyle(.red)
                                    .textSelection(.enabled)
                            }
                        }
                        Spacer()
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
