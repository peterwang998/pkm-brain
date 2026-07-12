import XCTest

final class PKMBrainUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        let configURL = URL(fileURLWithPath: "/private/tmp/pkm-brain-ui-acceptance.json")
        let config = try JSONDecoder().decode(
            AcceptanceConfig.self,
            from: Data(contentsOf: configURL)
        )
        app = XCUIApplication()
        app.launchEnvironment["PKM_BRAIN_HOME"] = config.home
        app.launchEnvironment["PKM_BRAIN_DEV_BRAIN_BIN"] = config.brain
        app.launchEnvironment["PKM_BRAIN_APP_SUPPORT"] = config.appSupport
        app.launchEnvironment["PKM_BRAIN_UI_TEST"] = "1"
        app.launchArguments = ["--destination=Today", "-ApplePersistenceIgnoreState", "YES"]
        app.launch()
        if !app.windows.firstMatch.waitForExistence(timeout: 3) {
            app.activate()
            app.typeKey("n", modifierFlags: [.command])
        }
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 20))
    }

    override func tearDownWithError() throws {
        app?.terminate()
        app = nil
    }

    @MainActor
    func testAllDestinationsRenderAndRemainNavigable() throws {
        for destination in ["Today", "Queue", "Wiki", "Entities", "Ask", "Ops", "Settings"] {
            let navigationLabel = app.staticTexts[destination].firstMatch
            XCTAssertTrue(navigationLabel.waitForExistence(timeout: 10), "Missing navigation label \(destination)")
            navigationLabel.click()
            XCTAssertGreaterThanOrEqual(
                app.staticTexts.matching(identifier: destination).count,
                2,
                "Missing rendered heading for \(destination)"
            )
            if destination == "Queue" {
                app.typeKey("1", modifierFlags: [])
                app.typeKey(.return, modifierFlags: [])
                XCTAssertTrue(app.windows.firstMatch.exists)
            }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "destination-\(destination.lowercased())"
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }

}

private struct AcceptanceConfig: Decodable {
    let home: String
    let brain: String
    let appSupport: String
}
