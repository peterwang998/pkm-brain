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
                        Text(job.displayStatus)
                    }
                    TableColumn("Last Run") { job in
                        Text(job.last_run_at ?? "")
                    }
                    TableColumn("Next Due") { job in
                        Text(job.next_due_at ?? "")
                    }
                    TableColumn("Detail") { job in
                        Text(job.statusDetail ?? "")
                            .lineLimit(2)
                    }
                }
                Text("Today shows the latest recorded automation run. This table shows daemon scheduler checks, including no-op skips.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("Scheduler state is unavailable.")
                    .foregroundStyle(.secondary)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
