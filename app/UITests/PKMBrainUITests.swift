import XCTest

final class PKMBrainUITests: XCTestCase {
    @MainActor
    private func launchApp() throws -> XCUIApplication {
        let configURL = URL(fileURLWithPath: "/private/tmp/pkm-brain-ui-acceptance.json")
        let config = try JSONDecoder().decode(
            AcceptanceConfig.self,
            from: Data(contentsOf: configURL)
        )
        let app = XCUIApplication()
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
        return app
    }

    @MainActor
    func testAllDestinationsRenderAndRemainNavigable() throws {
        continueAfterFailure = false
        let app = try launchApp()
        defer { app.terminate() }

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
            if destination == "Ops" {
                try captureConnectorAuthentication(in: app)
            }
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "destination-\(destination.lowercased())"
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }

    @MainActor
    private func captureConnectorAuthentication(in app: XCUIApplication) throws {
        XCTAssertTrue(
            app.staticTexts["Running"].waitForExistence(timeout: 180),
            "Daemon did not become ready for connector acceptance"
        )
        let picker = app.radioGroups["ops-section-picker"]
        XCTAssertTrue(picker.waitForExistence(timeout: 10), "Missing Ops section picker")
        let connectorsSegment = picker.radioButtons["Connectors"]
        XCTAssertTrue(connectorsSegment.waitForExistence(timeout: 10), "Missing Connectors segment")
        connectorsSegment.click()

        XCTAssertTrue(
            app.staticTexts["Google Calendar"].waitForExistence(timeout: 20),
            "Missing Google Calendar connector"
        )
        XCTAssertTrue(app.staticTexts["Gmail"].waitForExistence(timeout: 10), "Missing Gmail connector")
        XCTAssertTrue(app.staticTexts["Slack"].exists, "Missing Slack connector")
        let connectorsAttachment = XCTAttachment(screenshot: app.screenshot())
        connectorsAttachment.name = "ops-connectors"
        connectorsAttachment.lifetime = .keepAlways
        add(connectorsAttachment)

        let calendarSetup = app.buttons["connector-auth-calendar"]
        XCTAssertTrue(calendarSetup.waitForExistence(timeout: 10), "Missing Calendar setup action")
        calendarSetup.click()
        XCTAssertTrue(
            app.staticTexts["Connector Authentication"].waitForExistence(timeout: 10),
            "Missing Calendar authentication sheet"
        )
        XCTAssertTrue(
            app.staticTexts["Read events on your owned primary calendar only"]
                .waitForExistence(timeout: 10),
            "Missing Calendar read-only access summary"
        )
        XCTAssertTrue(app.staticTexts["http://127.0.0.1:53682/oauth/callback/calendar"].exists)
        app.buttons["Done"].click()

        let setup = app.buttons["connector-auth-gmail"]
        XCTAssertTrue(setup.waitForExistence(timeout: 10), "Missing Gmail setup action")
        setup.click()
        XCTAssertTrue(
            app.staticTexts["Connector Authentication"].waitForExistence(timeout: 10),
            "Missing Gmail authentication sheet"
        )
        XCTAssertTrue(
            app.staticTexts["Read Gmail messages and threads only"]
                .waitForExistence(timeout: 10),
            "Missing Gmail read-only access summary"
        )
        XCTAssertTrue(app.staticTexts["http://127.0.0.1:53682/oauth/callback/gmail"].exists)

        let authAttachment = XCTAttachment(screenshot: app.screenshot())
        authAttachment.name = "ops-gmail-auth"
        authAttachment.lifetime = .keepAlways
        add(authAttachment)
        app.buttons["Done"].click()
    }

}

private struct AcceptanceConfig: Decodable {
    let home: String
    let brain: String
    let appSupport: String
}
