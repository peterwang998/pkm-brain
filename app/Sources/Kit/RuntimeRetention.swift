import Foundation

public enum RuntimeRetentionError: Error, Equatable {
    case processInventoryUnavailable
}

public struct RuntimeRetentionPlan: Equatable, Sendable {
    public let currentRuntimeID: String?
    public let activeRuntimeIDs: [String]
    public let rollbackRuntimeIDs: [String]
    public let removableRuntimeIDs: [String]
    public let reclaimableBytes: Int64
}

public struct RuntimeRetentionManager: Sendable {
    public let runtimeRoot: URL

    public init(runtimeRoot: URL) {
        self.runtimeRoot = runtimeRoot.standardizedFileURL
    }

    public func plan(
        currentRuntimeID: String?,
        keepRollbacks: Int = 1,
        processCommands: [String]? = nil,
        calculateBytes: Bool = true
    ) throws -> RuntimeRetentionPlan {
        let entries = try runtimeEntries()
        let knownIDs = Set(entries.map(\.id))
        let resolvedCurrentID = currentRuntimeID ?? currentSymlinkRuntimeID()
        guard let commands = processCommands ?? Self.runningProcessCommands() else {
            throw RuntimeRetentionError.processInventoryUnavailable
        }
        let activeIDs = activeRuntimeIDs(from: commands).intersection(knownIDs)
        let rollbackIDs = entries
            .filter { $0.id != resolvedCurrentID }
            .prefix(max(0, keepRollbacks))
            .map(\.id)
        let retained = activeIDs
            .union(rollbackIDs)
            .union(resolvedCurrentID.map { [$0] } ?? [])
        let removable = entries.filter { !retained.contains($0.id) }
        let bytes = calculateBytes
            ? removable.reduce(Int64(0)) { $0 + directorySize($1.url) }
            : 0
        return RuntimeRetentionPlan(
            currentRuntimeID: resolvedCurrentID,
            activeRuntimeIDs: activeIDs.sorted(),
            rollbackRuntimeIDs: rollbackIDs.sorted(),
            removableRuntimeIDs: removable.map(\.id).sorted(),
            reclaimableBytes: bytes
        )
    }

    @discardableResult
    public func prune(
        currentRuntimeID: String?,
        keepRollbacks: Int = 1,
        processCommands: [String]? = nil
    ) throws -> RuntimeRetentionPlan {
        guard let commands = processCommands ?? Self.runningProcessCommands() else {
            throw RuntimeRetentionError.processInventoryUnavailable
        }
        let plan = try plan(
            currentRuntimeID: currentRuntimeID,
            keepRollbacks: keepRollbacks,
            processCommands: commands,
            calculateBytes: false
        )
        let protected = Set(plan.activeRuntimeIDs)
            .union(plan.rollbackRuntimeIDs)
            .union(plan.currentRuntimeID.map { [$0] } ?? [])
        let fileManager = FileManager.default
        for runtimeID in plan.removableRuntimeIDs where !protected.contains(runtimeID) {
            let candidate = runtimeRoot.appendingPathComponent(runtimeID, isDirectory: true).standardizedFileURL
            guard candidate.deletingLastPathComponent() == runtimeRoot else {
                continue
            }
            try fileManager.removeItem(at: candidate)
        }
        return plan
    }

    public static func runningProcessCommands() -> [String]? {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-axo", "command="]
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                return nil
            }
            return String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
        } catch {
            return nil
        }
    }

    private func runtimeEntries() throws -> [(id: String, url: URL, modifiedAt: Date)] {
        let fileManager = FileManager.default
        guard fileManager.fileExists(atPath: runtimeRoot.path) else {
            return []
        }
        return try fileManager.contentsOfDirectory(
            at: runtimeRoot,
            includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey, .contentModificationDateKey],
            options: [.skipsHiddenFiles]
        )
        .compactMap { url -> (id: String, url: URL, modifiedAt: Date)? in
            guard url.lastPathComponent != "current", url.lastPathComponent != "dev" else {
                return nil
            }
            let values = try? url.resourceValues(forKeys: [
                .isDirectoryKey,
                .isSymbolicLinkKey,
                .contentModificationDateKey,
            ])
            guard values?.isDirectory == true, values?.isSymbolicLink != true else {
                return nil
            }
            return (url.lastPathComponent, url, values?.contentModificationDate ?? .distantPast)
        }
        .sorted { lhs, rhs in
            if lhs.modifiedAt != rhs.modifiedAt {
                return lhs.modifiedAt > rhs.modifiedAt
            }
            return lhs.id > rhs.id
        }
    }

    private func currentSymlinkRuntimeID() -> String? {
        let current = runtimeRoot.appendingPathComponent("current")
        guard FileManager.default.fileExists(atPath: current.path) else {
            return nil
        }
        return current.resolvingSymlinksInPath().lastPathComponent
    }

    private func activeRuntimeIDs(from commands: [String]) -> Set<String> {
        let marker = runtimeRoot.path + "/"
        return Set(commands.compactMap { command in
            guard let range = command.range(of: marker) else {
                return nil
            }
            let suffix = command[range.upperBound...]
            guard let id = suffix.split(separator: "/", maxSplits: 1).first else {
                return nil
            }
            let value = String(id)
            return value == "current" ? currentSymlinkRuntimeID() : value
        })
    }

    private func directorySize(_ root: URL) -> Int64 {
        let keys: Set<URLResourceKey> = [.isRegularFileKey, .totalFileAllocatedSizeKey, .fileAllocatedSizeKey]
        guard let enumerator = FileManager.default.enumerator(
            at: root,
            includingPropertiesForKeys: Array(keys),
            options: [.skipsHiddenFiles]
        ) else {
            return 0
        }
        var total: Int64 = 0
        for case let fileURL as URL in enumerator {
            guard let values = try? fileURL.resourceValues(forKeys: keys), values.isRegularFile == true else {
                continue
            }
            total += Int64(values.totalFileAllocatedSize ?? values.fileAllocatedSize ?? 0)
        }
        return total
    }
}
