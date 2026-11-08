import SwiftRs
import Tauri
import UIKit
import UserNotifications
import WebKit
import WebPushCrypto

struct PushArgs: Decodable {
    let origin: String
    let vapid: String?
}

struct BadgeArgs: Decodable { let count: Int }
struct UrlArgs: Decodable { let url: String }
struct ShareArgs: Decodable { let path: String }
struct OriginArgs: Decodable { let origin: String }

struct SubKeys: Encodable { let p256dh: String; let auth: String }
struct Sub: Encodable { let endpoint: String; let keys: SubKeys }
struct SubResult: Encodable { let subscription: Sub? }
struct OpenResult: Encodable { let url: String? }

/// App 的 Rust 部分（mobile/src-tauri）通过 run_mobile_plugin 调这里；服务器的网页调不到（没有给网页开这个插件的命令）。
/// 命令在 Tauri 的后台队列里执行，碰界面的地方都切回主线程。
class OsaPlugin: Plugin, UNUserNotificationCenterDelegate {
    private weak var web: WKWebView?
    private var pending: (origin: String, url: String)?
    private let push = PushRegistrar(store: KeychainStore(accessGroup: appGroup))
    private var grace: UIBackgroundTaskIdentifier = .invalid

    override init() {
        super.init()
        // 插件在 App 启动完成前就建好：这时接管通知回调，冷启动时点的那条通知才不会丢
        UNUserNotificationCenter.current().delegate = self
    }

    override func load(webview: WKWebView) {
        web = webview
        webview.allowsLinkPreview = false   // 长按链接的预览里点“打开”会让主界面跳走
        // 整页不回弹、不能被手指拖着走（弹出键盘时系统把页面往上推，原来还能上下拖来拖去）；
        // 系统为了露出输入框自己推页面不受影响，对话和列表在网页里有自己的滚动，也不受影响
        webview.scrollView.bounces = false
        webview.scrollView.isScrollEnabled = false
        DeviceToken.installHooks()
        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(didEnterBackground), name: UIApplication.didEnterBackgroundNotification, object: nil)
        // 允许过通知的话，每次启动都向苹果要一次令牌（令牌可能会变），页面加载时拿去和中转核对
        Task {
            if await push.authorized() { await MainActor.run { UIApplication.shared.registerForRemoteNotifications() } }
        }
    }

    /// 退到后台时多留几秒：页面要把“没在看”告诉服务器，服务器才会推送（不然它当这个页面还开着）
    @objc private func didEnterBackground() {
        let app = UIApplication.shared
        if grace != .invalid { app.endBackgroundTask(grace) }
        grace = app.beginBackgroundTask { [weak self] in self?.endGrace() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.endGrace() }
    }

    private func endGrace() {
        guard grace != .invalid else { return }
        UIApplication.shared.endBackgroundTask(grace)
        grace = .invalid
    }

    // ---------- 推送 ----------
    private func result(_ reg: Registration?) throws -> SubResult {
        guard let reg else { return SubResult(subscription: nil) }
        let keys = try reg.keys()
        return SubResult(subscription: Sub(endpoint: reg.endpoint, keys: SubKeys(p256dh: b64u(keys.publicKey), auth: b64u(keys.auth))))
    }

    @objc public func pushCurrent(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(PushArgs.self)
        Task {
            do { invoke.resolve(try result(try await push.current(origin: args.origin, vapid: args.vapid ?? ""))) }
            catch { invoke.reject(error.localizedDescription) }
        }
    }

    @objc public func pushSubscribe(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(PushArgs.self)
        let provisional = UserDefaults.standard.bool(forKey: "OSAAutotest")
        Task {
            do { invoke.resolve(try result(try await push.subscribe(origin: args.origin, vapid: args.vapid ?? "", provisional: provisional))) }
            catch { invoke.reject(error.localizedDescription) }
        }
    }

    @objc public func pushUnsubscribe(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(OriginArgs.self)
        Task {
            await push.unsubscribe(origin: args.origin)
            invoke.resolve()
        }
    }

    @objc public func setBadge(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(BadgeArgs.self)
        UNUserNotificationCenter.current().setBadgeCount(max(0, args.count)) { _ in invoke.resolve() }
    }

    // ---------- 弹出页、分享 ----------
    @objc public func openSheet(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(UrlArgs.self)
        guard let url = URL(string: args.url) else { return invoke.reject("bad url") }
        DispatchQueue.main.async {
            SheetBrowser.present(url: url, from: self.manager.viewController) { invoke.resolve() }
        }
    }

    @objc public func share(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(ShareArgs.self)
        DispatchQueue.main.async {
            let file = URL(fileURLWithPath: args.path)
            let vc = UIActivityViewController(activityItems: [file], applicationActivities: nil)
            vc.completionWithItemsHandler = { _, _, _, _ in invoke.resolve() }
            guard var top = self.manager.viewController else { return invoke.resolve() }
            while let p = top.presentedViewController { top = p }
            vc.popoverPresentationController?.sourceView = top.view   // iPad
            vc.popoverPresentationController?.sourceRect = CGRect(x: top.view.bounds.midX, y: top.view.bounds.maxY - 40, width: 1, height: 1)
            top.present(vc, animated: true)
        }
    }

    // ---------- 点了通知 ----------
    /// App 在前台时收到推送（服务器以为没人在看，比如刚切回来）：只放进通知中心，不弹横幅
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler(UIApplication.shared.applicationState == .active ? [.list] : [.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let url = PushPayload.safePath(info["url"] as? String ?? "/")
        let origin = info["origin"] as? String ?? ""
        DispatchQueue.main.async {
            self.open(origin: origin, url: url)
            completionHandler()
        }
    }

    /// 页面已经开着这台服务器：直接告诉它打开哪个对话；还没加载好（冷启动）：记下来，页面加载好后来取
    private func open(origin: String, url: String) {
        if let w = web, !w.isLoading, let cur = w.url, originOf(cur) == origin,
           let detail = try? String(data: JSONSerialization.data(withJSONObject: [url], options: []), encoding: .utf8) {
            w.evaluateJavaScript("window.dispatchEvent(new CustomEvent('osa-open-url', { detail: \(detail)[0] }))")
        } else {
            pending = (origin, url)
        }
    }

    @objc public func takePendingOpen(_ invoke: Invoke) throws {
        let args = try invoke.parseArgs(OriginArgs.self)
        DispatchQueue.main.async {
            if let p = self.pending, p.origin == args.origin {
                self.pending = nil
                invoke.resolve(OpenResult(url: p.url))
            } else {
                invoke.resolve(OpenResult(url: nil))
            }
        }
    }
}

func originOf(_ u: URL) -> String {
    guard let scheme = u.scheme, let host = u.host else { return "" }
    let defaultPort = (scheme == "https" && u.port == 443) || (scheme == "http" && u.port == 80)
    return "\(scheme)://\(host)" + (u.port.map { defaultPort ? "" : ":\($0)" } ?? "")
}

@_cdecl("init_plugin_osa_ios")
public func initPlugin() -> Plugin {
    return OsaPlugin()
}
