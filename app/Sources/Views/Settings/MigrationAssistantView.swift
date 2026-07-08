import PKMBrainKit
import SwiftUI

struct MigrationAssistantView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        if let plan = appState.migrationPlan, plan.state != "fresh" {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline) {
                    Label(title(for: plan), systemImage: icon(for: plan))
                        .font(.headline)
                    Spacer()
                    Text(plan.state.capitalized)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if !plan.detected_launch_agents.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(plan.detected_launch_agents) { agent in
                            HStack {
                                Image(systemName: "clock.arrow.circlepath")
                                    .foregroundStyle(.secondary)
                                Text(agent.label)
                                    .font(.caption)
                                Spacer()
                                Text(agent.role_set)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                HStack {
                    Button {
                        Task { await appState.installMigrationShims() }
                    } label: {
                        Label("Install Shims", systemImage: "terminal")
                    }
                    Button {
                        Task { await appState.dryRunLaunchAgentRetirement() }
                    } label: {
                        Label("Dry Run Retirement", systemImage: "list.clipboard")
                    }
                    .disabled(plan.detected_launch_agents.isEmpty)
                    Spacer()
                    if let message = appState.migrationActionMessage {
                        Text(message)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(12)
            .background(.regularMaterial)
        }
    }

    private func title(for plan: MigrationPlan) -> String {
        switch plan.state {
        case "migrate":
            return "Migration Ready"
        case "adopt":
            return "Brain Home Ready"
        default:
            return "Setup Ready"
        }
    }

    private func icon(for plan: MigrationPlan) -> String {
        plan.needsMigration ? "arrow.triangle.2.circlepath.circle" : "checkmark.circle"
    }
}
