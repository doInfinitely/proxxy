import Foundation
import WebKit

/// Bridges server browser commands to a WKWebView instance.
/// Receives ``browser_cmd`` messages from WebSocketService, executes
/// them in the WKWebView, and sends ``browser_result`` back.
final class BrowserBridge: ObservableObject {

    weak var webView: WKWebView?
    weak var ws: WebSocketService?

    init() {}

    /// Called when a browser_cmd arrives from the server.
    func handleCommand(id: String, cmd: String, payload: [String: Any]) {
        guard let webView else {
            sendResult(id: id, success: false, error: "WKWebView not available")
            return
        }

        switch cmd {
        case "navigate":
            guard let urlString = payload["url"] as? String,
                  let url = URL(string: urlString) else {
                sendResult(id: id, success: false, error: "Invalid URL")
                return
            }
            DispatchQueue.main.async {
                webView.load(URLRequest(url: url))
            }
            // Wait for navigation to start, then respond
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.sendResult(id: id, success: true)
            }

        case "evaluate":
            guard let js = payload["js"] as? String else {
                sendResult(id: id, success: false, error: "No JS provided")
                return
            }
            let args = payload["args"] as? [Any] ?? []
            let wrappedJS = wrapJSWithArgs(js: js, args: args)

            DispatchQueue.main.async {
                webView.evaluateJavaScript(wrappedJS) { [weak self] result, error in
                    if let error {
                        self?.sendResult(id: id, success: false, error: error.localizedDescription)
                    } else {
                        let data: String
                        if let result {
                            if let str = result as? String {
                                data = str
                            } else if JSONSerialization.isValidJSONObject(result),
                                      let jsonData = try? JSONSerialization.data(withJSONObject: result),
                                      let jsonStr = String(data: jsonData, encoding: .utf8) {
                                data = jsonStr
                            } else {
                                data = "\(result)"
                            }
                        } else {
                            data = "null"
                        }
                        self?.sendResult(id: id, success: true, data: data)
                    }
                }
            }

        case "screenshot":
            DispatchQueue.main.async {
                let config = WKSnapshotConfiguration()
                webView.takeSnapshot(with: config) { [weak self] image, error in
                    if let error {
                        self?.sendResult(id: id, success: false, error: error.localizedDescription)
                        return
                    }
                    guard let image,
                          let jpegData = image.jpegData(compressionQuality: 0.7) else {
                        self?.sendResult(id: id, success: false, error: "Screenshot failed")
                        return
                    }
                    let b64 = jpegData.base64EncodedString()
                    self?.sendResult(id: id, success: true, data: b64)
                }
            }

        case "get_url":
            DispatchQueue.main.async {
                let url = webView.url?.absoluteString ?? ""
                self.sendResult(id: id, success: true, data: url)
            }

        case "get_html":
            let selector = payload["selector"] as? String ?? "body"
            let js = "document.querySelector('\(selector.replacingOccurrences(of: "'", with: "\\'"))')?.outerHTML ?? ''"
            DispatchQueue.main.async {
                webView.evaluateJavaScript(js) { [weak self] result, error in
                    if let error {
                        self?.sendResult(id: id, success: false, error: error.localizedDescription)
                    } else {
                        self?.sendResult(id: id, success: true, data: result as? String ?? "")
                    }
                }
            }

        default:
            sendResult(id: id, success: false, error: "Unknown command: \(cmd)")
        }
    }

    // MARK: - Helpers

    /// Wrap a JS function expression with arguments into a self-invoking call.
    /// e.g. "(selector) => { ... }" with args ["#date"] becomes
    /// "((selector) => { ... })(\"#date\")"
    private func wrapJSWithArgs(js: String, args: [Any]) -> String {
        if args.isEmpty {
            // If it looks like a function, invoke it
            if js.contains("=>") || js.hasPrefix("function") {
                return "(\(js))()"
            }
            return js
        }

        // Serialize args to JS literals
        let serializedArgs = args.map { arg -> String in
            if let str = arg as? String {
                // Wrap in array for NSJSONSerialization, then extract the encoded string
                if let data = try? JSONSerialization.data(withJSONObject: [str]),
                   let encoded = String(data: data, encoding: .utf8) {
                    // "[\"hello\"]" → drop first/last char → "\"hello\""
                    let inner = encoded.dropFirst().dropLast()
                    return String(inner)
                }
                return "\"\(str)\""
            } else if let bool = arg as? Bool {
                return bool ? "true" : "false"
            } else if let num = arg as? NSNumber {
                return "\(num)"
            } else if JSONSerialization.isValidJSONObject(arg),
                      let data = try? JSONSerialization.data(withJSONObject: arg),
                      let str = String(data: data, encoding: .utf8) {
                return str
            } else {
                return "null"
            }
        }

        return "(\(js))(\(serializedArgs.joined(separator: ", ")))"
    }

    private func sendResult(id: String, success: Bool, data: String? = nil, error: String? = nil) {
        let result = BrowserResult(id: id, success: success, data: data, error: error)
        ws?.sendBrowserResult(result)
    }
}
