import Foundation
import UserNotifications
import WebPushCrypto

/// 通知扩展：推送到了手机上，显示之前先在这里解密。
/// 推送中转转来的只有密文（"e"）和是哪个订阅（"s"）；钥匙在 App 存进钥匙串的登记里（App Group 共用），只有这台手机有。
/// 解不开（比如钥匙没了）就显示中转给的占位文案“你有一条新通知”。
final class NotificationService: UNNotificationServiceExtension {
    private var handler: ((UNNotificationContent) -> Void)?
    private var content: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        handler = contentHandler
        let c = (request.content.mutableCopy() as? UNMutableNotificationContent) ?? UNMutableNotificationContent()
        content = c
        let info = request.content.userInfo
        var outcome = "ok"
        do {
            guard let e = info["e"] as? String, let s = info["s"] as? String, let body = Data(base64Encoded: e) else { throw WebPushError.malformed }
            guard let reg = KeychainStore(accessGroup: "group.com.kadaliao.opensuperagent").get(s) else { throw WebPushError.noKeys }
            guard let p = PushPayload.parse(try Aes128gcm.decrypt(body, keys: try reg.keys())) else { throw WebPushError.malformed }
            c.title = p.title
            c.body = p.body
            if let b = p.badge { c.badge = NSNumber(value: b) }
            c.threadIdentifier = thread(p.url)   // 同一个对话的通知叠在一起
            c.userInfo = ["url": p.url, "origin": reg.origin, "tag": p.tag]
        } catch {
            outcome = "\(error)"
            c.userInfo = [:]
        }
        #if DEBUG
            record(outcome, c)
        #endif
        contentHandler(c)
    }

    override func serviceExtensionTimeWillExpire() {
        if let h = handler, let c = content { h(c) }
    }

    private func thread(_ url: String) -> String {
        let chat = URLComponents(string: url)?.queryItems?.first { $0.name == "chat" }?.value
        return chat.map { "chat-\($0)" } ?? "osa"
    }

    #if DEBUG
        /// 只在调试版里：把结果写进 App Group 的 last-push.json，自动测试从模拟器里读
        private func record(_ outcome: String, _ c: UNNotificationContent) {
            guard let dir = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: "group.com.kadaliao.opensuperagent") else { return }
            let obj: [String: Any] = ["outcome": outcome, "title": c.title, "body": c.body, "badge": c.badge ?? NSNull(),
                                      "thread": c.threadIdentifier, "userInfo": c.userInfo]
            if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted]) {
                try? data.write(to: dir.appendingPathComponent("last-push.json"))
            }
        }
    #endif
}
