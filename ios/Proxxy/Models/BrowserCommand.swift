import Foundation

/// A browser command sent from the server to be executed in WKWebView.
struct BrowserCommand: Identifiable {
    let id: String
    let cmd: CommandType
    let payload: [String: Any]

    enum CommandType: String {
        case navigate
        case evaluate
        case screenshot
        case getUrl = "get_url"
        case getHtml = "get_html"
    }
}

/// Result sent back to the server after executing a browser command.
struct BrowserResult: Encodable {
    let type = "browser_result"
    let id: String
    let success: Bool
    var data: String?
    var error: String?

    func toJSON() -> [String: Any] {
        var dict: [String: Any] = [
            "type": type,
            "id": id,
            "success": success,
        ]
        if let data { dict["data"] = data }
        if let error { dict["error"] = error }
        return dict
    }
}
