// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PKMBrainApp",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "PKMBrainKit", targets: ["PKMBrainKit"]),
        .executable(name: "PKMBrainApp", targets: ["PKMBrainApp"]),
        .executable(name: "PKMBrainAcceptance", targets: ["PKMBrainAcceptance"])
    ],
    dependencies: [
        .package(
            url: "https://github.com/swiftlang/swift-markdown.git",
            from: "0.8.0"
        )
    ],
    targets: [
        .target(
            name: "PKMBrainKit",
            path: "Sources/Kit"
        ),
        .executableTarget(
            name: "PKMBrainApp",
            dependencies: [
                "PKMBrainKit",
                .product(name: "Markdown", package: "swift-markdown")
            ],
            path: "Sources",
            exclude: ["Acceptance", "Kit", "App/Info.plist"],
            sources: ["App", "Views"]
        ),
        .executableTarget(
            name: "PKMBrainAcceptance",
            dependencies: ["PKMBrainKit"],
            path: "Sources/Acceptance"
        ),
        .testTarget(
            name: "PKMBrainKitTests",
            dependencies: ["PKMBrainKit"],
            path: "Tests",
            resources: [.process("Fixtures")]
        )
    ]
)
