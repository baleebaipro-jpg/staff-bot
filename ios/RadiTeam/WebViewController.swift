import UIKit
import WebKit

/// RADI Team для iPhone: кабинет сотрудника/руководителя (тот же, что в Telegram) внутри приложения.
/// - вход через бот сотрудников (ссылка t.me открывается в Telegram);
/// - «назад» — свайп от левого края экрана;
/// - защита от снимков экрана: содержимое лежит в «защищённом» слое (на скриншоте — пустой экран),
///   при записи экрана — закрывается шторкой;
/// - копирование и выделение текста отключены на странице.
final class WebViewController: UIViewController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    static let server = URL(string: "https://staff-bot-production-7a47ab13.up.railway.app/")!
    private var webView: WKWebView!
    private let shield = UIView()
    private let bg = UIColor(red: 6 / 255, green: 5 / 255, blue: 12 / 255, alpha: 1)

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = bg

        let content = WKUserContentController()
        // мост для страницы: window.RadiApp.openExternal(url) — открыть Telegram / внешнюю ссылку
        let bridge = """
        window.RadiApp = { openExternal: function (u) { window.webkit.messageHandlers.radi.postMessage(String(u)); },
                           version: function () { return 'ios'; } };
        """
        content.addUserScript(WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        content.add(self, name: "radi")

        let cfg = WKWebViewConfiguration()
        cfg.userContentController = content
        cfg.allowsInlineMediaPlayback = true
        cfg.dataDetectorTypes = []
        cfg.applicationNameForUserAgent = "RadiTeamApp/ios"

        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = bg
        webView.scrollView.backgroundColor = bg
        webView.allowsLinkPreview = false
        webView.scrollView.contentInsetAdjustmentBehavior = .always

        let host = secureContainer()
        view.addSubview(host)
        host.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            host.topAnchor.constraint(equalTo: view.topAnchor),
            host.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            host.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        host.addSubview(webView)
        webView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: host.topAnchor),
            webView.bottomAnchor.constraint(equalTo: host.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: host.trailingAnchor),
        ])

        // «Назад» — свайп от левого края
        let edge = UIScreenEdgePanGestureRecognizer(target: self, action: #selector(edgeBack(_:)))
        edge.edges = .left
        view.addGestureRecognizer(edge)

        // Шторка при записи экрана / трансляции
        shield.backgroundColor = bg
        shield.isHidden = true
        view.addSubview(shield)
        shield.frame = view.bounds
        shield.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        let label = UILabel()
        label.text = "🔒 RADI Team"
        label.textColor = .white
        label.font = .boldSystemFont(ofSize: 22)
        label.translatesAutoresizingMaskIntoConstraints = false
        shield.addSubview(label)
        label.centerXAnchor.constraint(equalTo: shield.centerXAnchor).isActive = true
        label.centerYAnchor.constraint(equalTo: shield.centerYAnchor).isActive = true
        NotificationCenter.default.addObserver(self, selector: #selector(captureChanged),
                                               name: UIScreen.capturedDidChangeNotification, object: nil)
        captureChanged()

        var comps = URLComponents(url: Self.server, resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "app", value: "ios")]
        webView.load(URLRequest(url: comps.url!))
    }

    /// Контейнер, содержимое которого iOS не показывает на скриншотах (слой «защищённого» поля ввода).
    /// Если на этой версии iOS приём не сработал — обычный контейнер (приложение всё равно работает).
    private func secureContainer() -> UIView {
        let field = UITextField()
        field.isSecureTextEntry = true
        if let secure = field.subviews.first {
            secure.subviews.forEach { $0.removeFromSuperview() }
            secure.isUserInteractionEnabled = true
            return secure
        }
        return UIView()
    }

    @objc private func captureChanged() {
        shield.isHidden = !UIScreen.main.isCaptured
        if !shield.isHidden { view.bringSubviewToFront(shield) }
    }

    @objc private func edgeBack(_ g: UIScreenEdgePanGestureRecognizer) {
        guard g.state == .ended else { return }
        webView.evaluateJavaScript("(function(){try{return (window.appBack && window.appBack()) ? 'yes' : 'no'}catch(e){return 'no'}})()")
    }

    // MARK: мост и навигация

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "radi", let s = message.body as? String, let url = URL(string: s) {
            openExternal(url)
        }
    }

    private func openExternal(_ url: URL) {
        UIApplication.shared.open(url, options: [:], completionHandler: nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { return decisionHandler(.allow) }
        if url.host == Self.server.host || url.scheme == "about" || url.scheme == "data" || url.scheme == "blob" {
            return decisionHandler(.allow)
        }
        openExternal(url)  // Telegram, телефоны, другие сайты — во внешнем приложении
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showOffline()
    }

    private func showOffline() {
        let html = """
        <html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>
        <body style='background:#06050C;color:#f5f3ff;font-family:-apple-system;text-align:center;padding-top:40%'>
        <div style='font-size:40px'>📡</div><h2>Internet yo'q / Нет интернета</h2>
        <button style='margin-top:20px;padding:14px 28px;border:0;border-radius:14px;background:#8b5cf6;color:#fff;font-size:16px'
          onclick="location.href='\(Self.server.absoluteString)?app=ios'">Qayta urinish / Повторить</button></body></html>
        """
        webView.loadHTMLString(html, baseURL: Self.server)
    }

    // MARK: window.alert / window.confirm на странице (без этого confirm в WKWebView всегда «нет»)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(a, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Отмена", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(a, animated: true)
    }
}
