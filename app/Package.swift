// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PKMBrainApp",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "PKMBrainKit", targets: ["PKMBrainKit"]),
        .executable(name: "PKMBrainApp", targets: ["PKMBrainApp"])
    ],
    targets: [
        .target(
            name: "PKMBrainKit",
            path: "Sources/Kit"
        ),
        .executableTarget(
            name: "PKMBrainApp",
            dependencies: ["PKMBrainKit"],
            path: "Sources",
            exclude: ["Kit", "App/Info.plist"],
            sources: ["App", "Views"]
        ),
        .testTarget(
            name: "PKMBrainKitTests",
            dependencies: ["PKMBrainKit"],
            path: "Tests",
            resources: [.process("Fixtures")]
        )
    ]
)
