import SwiftUI

struct OpsPreviewView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Ops")
                .font(.largeTitle.weight(.semibold))
            if let scheduler = appState.daemon.scheduler {
                Table(scheduler.jobs) {
                    TableColumn("Job") { job in
                        Text(job.id)
                    }
                    TableColumn("Status") { job in
                        Text(job.last_status ?? "pending")
                    }
                    TableColumn("Next Due") { job in
                        Text(job.next_due_at ?? "")
                    }
                }
            } else {
                Text("Scheduler state is unavailable.")
                    .foregroundStyle(.secondary)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

