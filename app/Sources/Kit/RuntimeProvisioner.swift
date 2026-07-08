import CryptoKit
import Foundation

public enum RuntimeProvisionerError: Error, Equatable {
    case cannotProvision(String)
}

@MainActor
public final class RuntimeProvisioner: ObservableObject {
    @Published public private(set) var phase: String = "Idle"
    @Published public private(set) var message: String = ""

    public let appSupportURL: URL
    public let fileManager: FileManager

    public init(
        appSupportURL: URL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("PKM Brain", isDirectory: true),
        fileManager: FileManager = .default
    ) {
        self.appSupportURL = appSupportURL
        self.fileManager = fileManager
    }

    public var currentBrainExecutableURL: URL {
        appSupportURL.appendingPathComponent("runtime/current/bin/brain")
    }

    public var currentRuntimeIDURL: URL {
        appSupportURL.appendingPathComponent("runtime/current/.pkm-brain-runtime-id")
    }

    public func ensureRuntime(bundle: Bundle = .main) async throws -> URL {
        phase = "Checking Runtime"
        message = "Checking app-managed Python runtime"
        if let bundledRuntime = try? bundledRuntimeSeed(bundle: bundle) {
            if fileManager.isExecutableFile(atPath: currentBrainExecutableURL.path),
               currentRuntimeID() == bundledRuntime.id {
                try await smokeBrainExecutable(currentBrainExecutableURL)
                phase = "Ready"
                message = "Runtime is ready"
                return currentBrainExecutableURL
            }
            return try await provisionBundledRuntime(bundledRuntime)
        }
        if let devBin = ProcessInfo.processInfo.environment["PKM_BRAIN_DEV_BRAIN_BIN"], fileManager.isExecutableFile(atPath: devBin) {
            phase = "Ready"
            message = "Using development brain executable"
            return URL(fileURLWithPath: devBin)
        }
        if let repo = ProcessInfo.processInfo.environment["PKM_BRAIN_REPO_PATH"] {
            return try createDevelopmentRuntime(repoPath: URL(fileURLWithPath: repo))
        }
        if fileManager.isExecutableFile(atPath: currentBrainExecutableURL.path) {
            phase = "Ready"
            message = "Runtime is ready"
            return currentBrainExecutableURL
        }
        throw RuntimeProvisionerError.cannotProvision(
            "No packaged runtime is bundled yet. Build with scripts/build-app.sh or set PKM_BRAIN_REPO_PATH for development."
        )
    }

    private func bundledRuntimeSeed(bundle: Bundle) throws -> BundledRuntimeSeed {
        guard let resourceURL = bundle.resourceURL else {
            throw RuntimeProvisionerError.cannotProvision("App bundle has no resource directory.")
        }
        let runtime = resourceURL.appendingPathComponent("runtime", isDirectory: true)
        let requirements = runtime.appendingPathComponent("requirements.lock")
        let pythonVersion = runtime.appendingPathComponent("python-version")
        guard fileManager.fileExists(atPath: requirements.path), fileManager.fileExists(atPath: pythonVersion.path) else {
            throw RuntimeProvisionerError.cannotProvision("Bundled runtime resources are missing.")
        }
        let wheels = try fileManager.contentsOfDirectory(
            at: runtime,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )
        .filter { $0.pathExtension == "whl" }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
        guard let wheel = wheels.first else {
            throw RuntimeProvisionerError.cannotProvision("Bundled pkm-brain wheel is missing.")
        }
        let bundledUV = resourceURL.appendingPathComponent("bin/uv")
        let uv = fileManager.isExecutableFile(atPath: bundledUV.path)
            ? bundledUV.path
            : findExecutable("uv")
        guard let uv else {
            throw RuntimeProvisionerError.cannotProvision("Bundled uv is missing and no system uv was found.")
        }
        let requirementsData = try Data(contentsOf: requirements)
        let lockHash = SHA256.hash(data: requirementsData).map { String(format: "%02x", $0) }.joined().prefix(8)
        let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
        let id = "\(version)-\(lockHash)"
        let python = try String(contentsOf: pythonVersion, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return BundledRuntimeSeed(
            id: id,
            uv: URL(fileURLWithPath: uv),
            pythonVersion: python,
            requirements: requirements,
            wheel: wheel
        )
    }

    private func provisionBundledRuntime(_ seed: BundledRuntimeSeed) async throws -> URL {
        phase = "Provisioning Runtime"
        message = "Creating app-managed Python runtime"
        let runtimeRoot = appSupportURL.appendingPathComponent("runtime", isDirectory: true)
        let target = runtimeRoot.appendingPathComponent(seed.id, isDirectory: true)
        let brain = target.appendingPathComponent("bin/brain")
        if fileManager.isExecutableFile(atPath: brain.path) {
            do {
                try await smokeBrainExecutable(brain)
                try writeRuntimeID(seed.id, to: target)
                try activateRuntime(target)
                phase = "Ready"
                message = "Runtime is ready"
                return currentBrainExecutableURL
            } catch {
                try? fileManager.removeItem(at: target)
            }
        }

        try fileManager.createDirectory(at: runtimeRoot, withIntermediateDirectories: true)
        try? fileManager.removeItem(at: target)
        do {
            message = "Installing Python \(seed.pythonVersion)"
            try await run(seed.uv, arguments: ["python", "install", seed.pythonVersion])
            message = "Creating virtual environment"
            try await run(seed.uv, arguments: ["venv", "--python", seed.pythonVersion, target.path])
            let python = target.appendingPathComponent("bin/python")
            message = "Installing locked dependencies"
            try await run(seed.uv, arguments: ["pip", "install", "--python", python.path, "-r", seed.requirements.path])
            message = "Installing PKM Brain"
            try await run(seed.uv, arguments: ["pip", "install", "--python", python.path, seed.wheel.path])
            guard fileManager.isExecutableFile(atPath: target.appendingPathComponent("bin/brain").path) else {
                throw RuntimeProvisionerError.cannotProvision("Provisioned runtime did not install a brain executable.")
            }
            try await smokeBrainExecutable(target.appendingPathComponent("bin/brain"))
            try writeRuntimeID(seed.id, to: target)
            try activateRuntime(target)
            phase = "Ready"
            message = "Runtime is ready"
            return currentBrainExecutableURL
        } catch {
            try? fileManager.removeItem(at: target)
            throw error
        }
    }

    private func smokeBrainExecutable(_ brain: URL) async throws {
        message = "Checking PKM Brain runtime"
        try await run(brain, arguments: ["--version"])
    }

    private func activateRuntime(_ target: URL) throws {
        let current = appSupportURL.appendingPathComponent("runtime/current")
        try? fileManager.removeItem(at: current)
        try fileManager.createDirectory(at: current.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fileManager.createSymbolicLink(at: current, withDestinationURL: target)
    }

    private func currentRuntimeID() -> String? {
        try? String(contentsOf: currentRuntimeIDURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func writeRuntimeID(_ id: String, to runtime: URL) throws {
        try id.write(to: runtime.appendingPathComponent(".pkm-brain-runtime-id"), atomically: true, encoding: .utf8)
    }

    private func run(_ executable: URL, arguments: [String]) async throws {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = executable
            process.arguments = arguments
            process.environment = ProcessInfo.processInfo.environment
            process.terminationHandler = { process in
                if process.terminationStatus == 0 {
                    continuation.resume(returning: ())
                } else {
                    let command = ([executable.path] + arguments).joined(separator: " ")
                    continuation.resume(
                        throwing: RuntimeProvisionerError.cannotProvision(
                            "\(command) exited with status \(process.terminationStatus)"
                        )
                    )
                }
            }
            do {
                try process.run()
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }

    private func createDevelopmentRuntime(repoPath: URL) throws -> URL {
        phase = "Provisioning Runtime"
        message = "Creating development runtime shim"
        let target = appSupportURL.appendingPathComponent("runtime/dev/bin", isDirectory: true)
        try fileManager.createDirectory(at: target, withIntermediateDirectories: true)
        let brain = target.appendingPathComponent("brain")
        let uv = findExecutable("uv") ?? "/opt/homebrew/bin/uv"
        let script = """
        #!/bin/zsh
        exec "\(uv)" --directory "\(repoPath.path)" run brain "$@"
        """
        try script.write(to: brain, atomically: true, encoding: .utf8)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: brain.path)
        try writeRuntimeID("dev", to: appSupportURL.appendingPathComponent("runtime/dev", isDirectory: true))
        let current = appSupportURL.appendingPathComponent("runtime/current")
        try? fileManager.removeItem(at: current)
        try fileManager.createDirectory(at: current.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fileManager.createSymbolicLink(
            at: current,
            withDestinationURL: appSupportURL.appendingPathComponent("runtime/dev", isDirectory: true)
        )
        phase = "Ready"
        message = "Development runtime is ready"
        return brain
    }

    private func findExecutable(_ name: String) -> String? {
        let paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        return paths
            .map { URL(fileURLWithPath: $0).appendingPathComponent(name).path }
            .first { fileManager.isExecutableFile(atPath: $0) }
    }
}

private struct BundledRuntimeSeed {
    let id: String
    let uv: URL
    let pythonVersion: String
    let requirements: URL
    let wheel: URL
}
