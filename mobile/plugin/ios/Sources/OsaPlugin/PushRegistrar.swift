import Foundation
import ObjectiveC
import UIKit
import UserNotifications
import WebPushCrypto

/// App Group：App 和通知扩展共用钥匙串里的推送钥匙
let appGroup = "group.com.kadaliao.opensuperagent"

struct PushError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

func zh() -> Bool { (Locale.preferredLanguages.first ?? "zh").hasPrefix("zh") }
func tr(_ zhText: String, _ en: String) -> String { zh() ? zhText : en }

// ---------- 苹果给的推送令牌 ----------
/// tao 的 AppDelegate 没有接推送令牌的方法：运行时给它加上（做法参考 Choochmeque/tauri-plugin-notifications，MIT）
enum DeviceToken {
    private static var waiters: [(Result<Data, Error>) -> Void] = []
    private(set) static var last: Data?
    private static var installed = false

    static func deliver(_ r: Result<Data, Error>) {
        DispatchQueue.main.async {
            if case .success(let d) = r { last = d }
            let w = waiters
            waiters = []
            w.forEach { $0(r) }
        }
    }

    static func installHooks() {
        guard !installed, let delegate = UIApplication.shared.delegate else { return }
        installed = true
        let cls: AnyClass = type(of: delegate)
        let ok: @convention(block) (AnyObject, UIApplication, Data) -> Void = { _, _, token in deliver(.success(token)) }
        let fail: @convention(block) (AnyObject, UIApplication, NSError) -> Void = { _, _, err in deliver(.failure(err)) }
        add(cls, #selector(UIApplicationDelegate.application(_:didRegisterForRemoteNotificationsWithDeviceToken:)), imp_implementationWithBlock(ok))
        add(cls, #selector(UIApplicationDelegate.application(_:didFailToRegisterForRemoteNotificationsWithError:)), imp_implementationWithBlock(fail))
    }

    private static func add(_ cls: AnyClass, _ sel: Selector, _ imp: IMP) {
        if !class_addMethod(cls, sel, imp, "v@:@@"), let m = class_getInstanceMethod(cls, sel) {
            method_setImplementation(m, imp)   // 已经有了（别的插件加的）：换成我们的
        }
    }

    /// 向苹果要令牌，最多等 20 秒
    static func fetch() async throws -> String {
        if let t = last { return hex(t) }
        let data: Data = try await withCheckedThrowingContinuation { cont in
            DispatchQueue.main.async {
                var done = false
                waiters.append { r in
                    guard !done else { return }
                    done = true
                    cont.resume(with: r)
                }
                UIApplication.shared.registerForRemoteNotifications()
                DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
                    guard !done else { return }
                    done = true
                    cont.resume(throwing: PushError(message: tr("没拿到苹果的推送令牌，检查一下网络再试", "Couldn't get a push token from Apple. Check the network and try again.")))
                }
            }
        }
        return hex(data)
    }

    static func hex(_ d: Data) -> String { d.map { String(format: "%02x", $0) }.joined() }
}

// ---------- 推送中转的登记 ----------
final class PushRegistrar {
    let store: RegistrationStore
    private var refreshed = Set<String>()   // 这次启动已经和中转核对过的登记

    init(store: RegistrationStore) {
        self.store = store
    }

    /// 中转的地址。调试时可以用启动参数 -OSARelay http://127.0.0.1:8790 换成本机的
    var relay: String {
        (UserDefaults.standard.string(forKey: "OSARelay") ?? "https://push.cclsp.top").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    /// 开发版（Xcode、模拟器）的令牌走苹果的 sandbox，TestFlight 和 App Store 走 production。中转发现不对会自己换
    var env: String {
        #if targetEnvironment(simulator)
            return "sandbox"
        #else
            guard let path = Bundle.main.path(forResource: "embedded", ofType: "mobileprovision"),
                  let text = try? String(contentsOfFile: path, encoding: .isoLatin1) else { return "production" }
            return text.range(of: "<key>aps-environment</key>\\s*<string>development</string>", options: .regularExpression) != nil ? "sandbox" : "production"
        #endif
    }

    func authorized() async -> Bool {
        let s = await UNUserNotificationCenter.current().notificationSettings()
        return [.authorized, .provisional, .ephemeral].contains(s.authorizationStatus)
    }

    /// 这台手机给 origin 开着推送时，返回订阅（必要时先和中转核对、重新登记）；没开返回 nil
    func current(origin: String, vapid: String) async throws -> Registration? {
        guard var reg = store.forOrigin(origin), await authorized() else { return nil }
        let token = try await DeviceToken.fetch()
        if refreshed.contains(reg.id), reg.token == token, reg.vapid == vapid { return reg }
        if let updated = try await update(&reg, token: token, vapid: vapid) { return updated }
        store.delete(reg.id)   // 中转不认识这个地址了（比如它的数据丢了）：重新登记一个
        return try await create(origin: origin, vapid: vapid, token: token)
    }

    /// 打开推送：要通知权限、拿令牌、在中转登记
    func subscribe(origin: String, vapid: String, provisional: Bool) async throws -> Registration {
        var options: UNAuthorizationOptions = [.alert, .badge, .sound]
        if provisional { options.insert(.provisional) }   // 自动测试：不弹权限框
        let granted = try await UNUserNotificationCenter.current().requestAuthorization(options: options)
        guard granted else {
            throw PushError(message: tr("没有通知权限：请在 iPhone 的 设置 → 通知 → Open Super Agent 里允许通知", "Notifications are off. Allow them in Settings → Notifications → Open Super Agent."))
        }
        let token = try await DeviceToken.fetch()
        if var reg = store.forOrigin(origin) {
            if let updated = try await update(&reg, token: token, vapid: vapid) { return updated }
            store.delete(reg.id)
        }
        return try await create(origin: origin, vapid: vapid, token: token)
    }

    func unsubscribe(origin: String) async {
        guard let reg = store.forOrigin(origin) else { return }
        store.delete(reg.id)
        _ = try? await request("DELETE", "/v1/endpoints/\(reg.id)", secret: reg.secret, body: nil)
    }

    private func create(origin: String, vapid: String, token: String) async throws -> Registration {
        let (status, json) = try await request("POST", "/v1/endpoints", secret: nil, body: ["token": token, "env": env, "vapid": vapid])
        guard status == 201, let id = json["id"] as? String, let secret = json["secret"] as? String, let endpoint = json["endpoint"] as? String else {
            throw PushError(message: tr("推送中转登记失败（\(status)）：", "The push relay refused (\(status)): ") + (json["detail"] as? String ?? ""))
        }
        let reg = Registration(id: id, secret: secret, endpoint: endpoint, origin: origin, vapid: vapid, token: token, env: env, keys: .generate())
        try store.save(reg)
        refreshed.insert(id)
        return reg
    }

    /// 更新中转那边的令牌和服务器公钥。中转回 404（不认识这个地址）时返回 nil
    private func update(_ reg: inout Registration, token: String, vapid: String) async throws -> Registration? {
        let (status, json) = try await request("PUT", "/v1/endpoints/\(reg.id)", secret: reg.secret, body: ["token": token, "env": env, "vapid": vapid])
        if status == 404 || status == 401 { return nil }
        guard status == 200 else {
            throw PushError(message: tr("推送中转出错（\(status)）：", "The push relay failed (\(status)): ") + (json["detail"] as? String ?? ""))
        }
        reg.token = token
        reg.vapid = vapid
        reg.env = env
        try store.save(reg)
        refreshed.insert(reg.id)
        return reg
    }

    private func request(_ method: String, _ path: String, secret: String?, body: [String: String]?) async throws -> (Int, [String: Any]) {
        guard let url = URL(string: relay + path) else { throw PushError(message: "bad relay url") }
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.httpMethod = method
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "content-type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        if let secret { req.setValue("Bearer \(secret)", forHTTPHeaderField: "authorization") }
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            return ((resp as? HTTPURLResponse)?.statusCode ?? 0, json)
        } catch {
            throw PushError(message: tr("连不上推送中转：", "Can't reach the push relay: ") + error.localizedDescription)
        }
    }
}
