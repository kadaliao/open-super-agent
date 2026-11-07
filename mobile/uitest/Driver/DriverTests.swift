import XCTest

/// Drives the simulator UI for the push test: system alerts, lock button, tapping a notification.
/// Each test is one step; run.sh runs one with -only-testing. Text to look for comes from TEST_RUNNER_NOTIF_TEXT.
final class DriverTests: XCTestCase {
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    var text: String { ProcessInfo.processInfo.environment["NOTIF_TEXT"] ?? "" }

    func testAllowNotifications() {
        let alert = springboard.alerts.firstMatch
        guard alert.waitForExistence(timeout: 30) else { return XCTFail("no permission alert") }
        for label in ["Allow", "允许"] where alert.buttons[label].exists {
            alert.buttons[label].tap(); return
        }
        XCTFail("alert has no Allow button: \(alert.debugDescription)")
    }

    func testLock() {
        XCUIDevice.shared.perform(NSSelectorFromString("pressLockButton"))
    }

    func testHome() {
        XCUIDevice.shared.press(.home)
    }

    func testWaitNotification() {
        let el = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
        XCTAssertTrue(el.waitForExistence(timeout: 60), "no element containing \(text)")
        print("FOUND: \(el.label)")
    }

    func testTapNotification() {
        let el = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
        guard el.waitForExistence(timeout: 60) else { return XCTFail("no element containing \(text)") }
        print("TAPPING: \(el.label)")
        el.tap()
    }

    /// Lock screen: tap the notification, then swipe up to unlock (no passcode), which completes the open.
    func testTapNotificationAndUnlock() {
        let el = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
        guard el.waitForExistence(timeout: 60) else { return XCTFail("no element containing \(text)") }
        print("TAPPING: \(el.label)")
        el.tap()
        sleep(1)
        let start = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.995))
        start.press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35)))
    }

    func testSwipeUp() {
        let start = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.995))
        start.press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35)))
    }

    func testDump() {
        print(springboard.debugDescription)
    }
}
