import UIKit
import WebKit

/// App 里弹出的一页：打开同一台服务器上的报告页、文件，和应用的登录授权。
/// 用的是第二个 WKWebView：和主界面共用登录 cookie（同一个默认的数据存储），但没有 App 的命令，页面里的脚本调不到 App。
final class SheetBrowser: UIViewController, WKNavigationDelegate, WKUIDelegate, UIAdaptivePresentationControllerDelegate {
    private let web: WKWebView
    private let start: URL
    private let onClose: () -> Void
    private var closed = false

    init(url: URL, onClose: @escaping () -> Void) {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        web = WKWebView(frame: .zero, configuration: cfg)
        start = url
        self.onClose = onClose
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsLinkPreview = false
        web.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(web)
        NSLayoutConstraint.activate([
            web.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            web.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            web.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        navigationItem.rightBarButtonItem = UIBarButtonItem(barButtonSystemItem: .done, target: self, action: #selector(done))
        navigationItem.leftBarButtonItem = UIBarButtonItem(image: UIImage(systemName: "safari"), style: .plain, target: self, action: #selector(inSafari))
        web.load(URLRequest(url: start))
    }

    static func present(url: URL, from vc: UIViewController?, onClose: @escaping () -> Void) {
        let sheet = SheetBrowser(url: url, onClose: onClose)
        let nav = UINavigationController(rootViewController: sheet)
        nav.modalPresentationStyle = .pageSheet
        nav.presentationController?.delegate = sheet
        guard let vc = top(vc) else { return onClose() }
        vc.present(nav, animated: true)
    }

    private static func top(_ vc: UIViewController?) -> UIViewController? {
        var v = vc
        while let p = v?.presentedViewController { v = p }
        return v
    }

    @objc private func done() { close() }

    @objc private func inSafari() {
        if let u = web.url { UIApplication.shared.open(u) }
    }

    private func close() {
        guard !closed else { return }
        closed = true
        dismiss(animated: true) { self.onClose() }
    }

    // 向下滑关掉
    func presentationControllerDidDismiss(_ presentationController: UIPresentationController) {
        guard !closed else { return }
        closed = true
        onClose()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        title = webView.title
        // 应用授权完成页过一秒多会自己关；这里替它关掉弹出页
        if webView.url?.path == "/oauth/callback" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.close() }
        }
    }

    // 页面里再开新窗口（target=_blank、window.open）：就在这一页里打开
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = navigationAction.request.url { webView.load(URLRequest(url: u)) }
        return nil
    }

    // 页面里的 window.close()
    func webViewDidClose(_ webView: WKWebView) { close() }
}
