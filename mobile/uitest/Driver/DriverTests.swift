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

    // ---------- App 里的手势（左边缘右滑返回、不能缩放、整页不被拖着走）----------
    let osa = XCUIApplication(bundleIdentifier: "com.kadaliao.opensuperagent")
    func at(_ x: CGFloat, _ y: CGFloat) -> XCUICoordinate { osa.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)) }

    func testEdgeSwipe() {   // 从左边缘往右滑过大半个屏幕
        osa.activate()
        at(0.01, 0.5).press(forDuration: 0.05, thenDragTo: at(0.8, 0.5), withVelocity: 800, thenHoldForDuration: 0)
    }

    func testEdgeSwipeShort() {   // 只滑一小段、慢慢松手：应该弹回去
        osa.activate()
        at(0.01, 0.5).press(forDuration: 0.05, thenDragTo: at(0.15, 0.5), withVelocity: 60, thenHoldForDuration: 0.3)
    }

    func testPinch() {
        osa.activate()
        osa.webViews.firstMatch.pinch(withScale: 2.5, velocity: 3)
    }

    func testDoubleTap() {
        osa.activate()
        at(0.5, 0.5).doubleTap()
    }

    func testDragPage() {   // 在不能滚的地方（顶上的标题栏）往下拖
        osa.activate()
        at(0.5, 0.08).press(forDuration: 0.05, thenDragTo: at(0.5, 0.6), withVelocity: 600, thenHoldForDuration: 0.5)
    }

    var nums: [CGFloat] { text.split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }.map { CGFloat($0) } }

    func testTap() {   // 文字里写 "x,y"（0–1，相对屏幕）
        osa.activate()
        let n = nums
        guard n.count == 2 else { return XCTFail("need x,y") }
        at(n[0], n[1]).tap()
    }

    func testDrag() {   // 文字里写 "x0,y0,x1,y1"
        osa.activate()
        let n = nums
        guard n.count == 4 else { return XCTFail("need x0,y0,x1,y1") }
        at(n[0], n[1]).press(forDuration: 0.05, thenDragTo: at(n[2], n[3]), withVelocity: 600, thenHoldForDuration: 0.5)
    }

    func testDump() {
        print(springboard.debugDescription)
    }
}
