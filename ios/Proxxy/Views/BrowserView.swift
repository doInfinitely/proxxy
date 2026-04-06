import SwiftUI
import WebKit

/// UIViewRepresentable wrapping WKWebView for the agent-controlled browser.
struct BrowserView: UIViewRepresentable {
    @EnvironmentObject var ws: WebSocketService
    @EnvironmentObject var bridge: BrowserBridge

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true

        // Allow JavaScript
        config.defaultWebpagePreferences.allowsContentJavaScript = true

        // Force pinch-to-zoom even if pages disable it via viewport meta
        let zoomScript = WKUserScript(
            source: """
            var meta = document.querySelector('meta[name="viewport"]');
            if (meta) {
                meta.setAttribute('content', meta.content
                    .replace(/user-scalable\\s*=\\s*no/gi, 'user-scalable=yes')
                    .replace(/maximum-scale\\s*=\\s*[\\d.]+/gi, 'maximum-scale=10'));
            }
            """,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        )
        config.userContentController.addUserScript(zoomScript)

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.isOpaque = false
        webView.backgroundColor = .secondarySystemBackground

        // Provide the webView to the bridge so it can execute commands
        bridge.webView = webView

        // Wire up the WebSocket to forward browser commands to the bridge
        ws.onBrowserCommand = { [weak bridge] id, cmd, payload in
            bridge?.handleCommand(id: id, cmd: cmd, payload: payload)
        }

        // Load a placeholder page initially
        let placeholderHTML = """
        <html><body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
        font-family:-apple-system,system-ui;color:#888;background:#f2f2f7;">
        <div style="text-align:center"><p style="font-size:40px;margin:0">🌐</p>
        <p style="font-size:15px;margin-top:8px">Waiting for agent...</p></div></body></html>
        """
        webView.loadHTMLString(placeholderHTML, baseURL: nil)

        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        // No dynamic updates needed — the server drives navigation via commands
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(ws: ws)
    }

    // MARK: - WKNavigationDelegate

    class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let ws: WebSocketService

        init(ws: WebSocketService) {
            self.ws = ws
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            // Notify server of URL changes so it can check for subharness matches
            let url = webView.url?.absoluteString ?? ""
            if !url.isEmpty && url != "about:blank" {
                ws.sendJSON([
                    "type": "url_changed",
                    "url": url,
                ])
                DispatchQueue.main.async {
                    self.ws.currentURL = url
                }
            }
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            let url = webView.url?.absoluteString ?? ""
            DispatchQueue.main.async {
                self.ws.currentURL = url
            }
        }

        // Allow all navigations (the server controls what pages to visit)
        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            decisionHandler(.allow)
        }

        // Handle target="_blank" links — load in the same webView instead of dropping
        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            // If the link wants a new window/tab, load it in the current webView
            if navigationAction.targetFrame == nil || !navigationAction.targetFrame!.isMainFrame {
                webView.load(navigationAction.request)
            }
            return nil  // Don't create a new WKWebView
        }

        // Handle JS alerts
        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping () -> Void
        ) {
            // Auto-dismiss alerts (the agent doesn't need user interaction)
            completionHandler()
        }

        // Handle JS confirms (always accept)
        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (Bool) -> Void
        ) {
            completionHandler(true)
        }
    }
}
