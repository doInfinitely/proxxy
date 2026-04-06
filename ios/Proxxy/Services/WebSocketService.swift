import Foundation
import Combine

/// Manages the WebSocket connection to the backend.
/// Handles the iOS hello handshake, chat messages, and browser commands.
final class WebSocketService: ObservableObject {

    // MARK: - Published state

    @Published var messages: [ChatMessage] = []
    @Published var isConnected = false
    @Published var status: String = "idle"  // "idle", "thinking"
    @Published var currentURL: String = ""
    @Published var sessionId: String = ""
    /// Small activity line shown under the last user bubble (e.g. "Browsing: url").
    /// Cleared when the agent finishes or sends a real message.
    @Published var activityText: String = ""

    // MARK: - Callbacks

    /// Set by BrowserBridge to handle incoming browser commands.
    var onBrowserCommand: ((_ id: String, _ cmd: String, _ payload: [String: Any]) -> Void)?
    /// Called when server assigns a session ID.
    var onSessionId: ((String) -> Void)?
    /// Called when the browser navigates to a new URL.
    var onURLVisited: ((String) -> Void)?
    /// Called when messages array changes.
    var onMessagesChanged: (([ChatMessage]) -> Void)?
    /// Called when a call event is received.
    var onCallEvent: ((String, [String: Any]) -> Void)?

    // MARK: - Private

    private var webSocketTask: URLSessionWebSocketTask?
    private var session: URLSession?
    private var pingTimer: Timer?
    private var reconnectAttempts = 0
    private let maxReconnectAttempts = 10
    private var helloSent = false

    /// Server URL — production Railway deployment.
    var serverURL: String = {
        ProcessInfo.processInfo.environment["AGENT_SERVER_URL"]
            ?? "wss://backend-production-112f.up.railway.app/ws"
    }()

    deinit {
        disconnect()
    }

    // MARK: - Connection lifecycle

    func connect() {
        guard let url = URL(string: serverURL) else {
            print("[WS] Invalid server URL: \(serverURL)")
            return
        }

        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        session = URLSession(configuration: config)

        webSocketTask = session?.webSocketTask(with: url)
        webSocketTask?.resume()

        // hello is sent after receiving session_id (see handleMessage)
        receiveMessage()
        startPing()

        DispatchQueue.main.async {
            self.isConnected = true
            self.reconnectAttempts = 0
        }

        print("[WS] Connected to \(serverURL)")
    }

    func disconnect() {
        pingTimer?.invalidate()
        pingTimer = nil
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        webSocketTask = nil
        session?.invalidateAndCancel()
        session = nil
        helloSent = false

        DispatchQueue.main.async {
            self.isConnected = false
        }
    }

    // MARK: - Sending

    /// Send the iOS hello handshake to identify as a mobile client.
    private func sendHello() {
        guard !helloSent else { return }
        helloSent = true
        let hello: [String: Any] = [
            "type": "hello",
            "client": "ios",
            "version": "1.0",
        ]
        sendJSON(hello)
    }

    /// Send a user chat message.
    func sendMessage(_ text: String) {
        let msg: [String: Any] = [
            "type": "message",
            "content": text,
        ]
        sendJSON(msg)

        // Add to local messages immediately
        DispatchQueue.main.async {
            self.messages.append(ChatMessage(role: .user, content: text))
            self.onMessagesChanged?(self.messages)
        }
    }

    /// Send a browser_result back to the server.
    func sendBrowserResult(_ result: BrowserResult) {
        sendJSON(result.toJSON())
    }

    /// Send a Firebase auth token.
    func sendAuth(token: String) {
        sendJSON(["type": "auth", "token": token])
    }

    /// Send settings (voice_id, about_me) to the server.
    func sendSettings(voiceId: String, aboutMe: String) {
        var msg: [String: Any] = ["type": "settings"]
        if !voiceId.isEmpty { msg["voice_id"] = voiceId }
        if !aboutMe.isEmpty { msg["about_me"] = aboutMe }
        sendJSON(msg)
    }

    /// Stop the current agent task.
    func sendStop() {
        sendJSON(["type": "stop"])
    }

    /// Start a new session — disconnects and reconnects to get a fresh session.
    func startNewSession() {
        disconnect()
        DispatchQueue.main.async {
            self.messages.removeAll()
            self.currentURL = ""
            self.status = "idle"
            self.activityText = ""
            self.sessionId = ""
        }
        connect()
    }

    /// Reconnect with an existing session ID to resume a conversation.
    func reconnectWithSession(_ sessionId: String, messages: [ChatMessage]) {
        disconnect()
        DispatchQueue.main.async {
            self.messages = messages
            self.sessionId = sessionId
        }
        // Connect then send hello with existing session ID
        guard let url = URL(string: serverURL) else { return }
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        session = URLSession(configuration: config)
        webSocketTask = session?.webSocketTask(with: url)
        webSocketTask?.resume()

        // hello is sent after receiving session_id (see handleMessage)
        receiveMessage()
        startPing()

        DispatchQueue.main.async {
            self.isConnected = true
            self.reconnectAttempts = 0
        }
    }

    func sendJSON(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let text = String(data: data, encoding: .utf8) else {
            return
        }
        webSocketTask?.send(.string(text)) { error in
            if let error {
                print("[WS] Send error: \(error)")
            }
        }
    }

    // MARK: - Receiving

    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            guard let self else { return }

            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    self.handleMessage(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        self.handleMessage(text)
                    }
                @unknown default:
                    break
                }
                // Continue receiving
                self.receiveMessage()

            case .failure(let error):
                print("[WS] Receive error: \(error)")
                DispatchQueue.main.async {
                    self.isConnected = false
                }
                self.attemptReconnect()
            }
        }
    }

    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String else {
            return
        }

        DispatchQueue.main.async { [self] in
            switch type {
            case "session_id":
                self.sessionId = json["session_id"] as? String ?? ""
                self.onSessionId?(self.sessionId)
                // Connection is confirmed — send iOS hello handshake now
                self.sendHello()

            case "hello_ack":
                print("[WS] Server acknowledged iOS client")

            case "status":
                // Two kinds: {"status": "thinking"} or {"content": "Browsing: ..."}
                if let s = json["status"] as? String {
                    self.status = s
                }
                if let content = json["content"] as? String, !content.isEmpty {
                    // Show as small activity text under last user bubble (not a chat bubble)
                    self.activityText = content
                }

            case "assistant":
                let content = json["content"] as? String ?? ""
                self.messages.append(ChatMessage(role: .assistant, content: content))
                self.activityText = ""
                self.onMessagesChanged?(self.messages)

            case "done":
                let content = json["content"] as? String ?? "Task completed."
                self.messages.append(ChatMessage(role: .done, content: content))
                self.status = "idle"
                self.activityText = ""
                self.onMessagesChanged?(self.messages)

            case "screenshot":
                // Screenshots are handled by the browser view via the bridge
                // For mobile, we could show these as thumbnails but they're mainly
                // for server-side viewers. The WKWebView already shows the page.
                break

            case "url_update":
                let url = json["url"] as? String ?? ""
                self.currentURL = url
                if !url.isEmpty {
                    self.onURLVisited?(url)
                }

            case "browser_cmd":
                // Forward to BrowserBridge
                let cmdId = json["id"] as? String ?? ""
                let cmd = json["cmd"] as? String ?? ""
                self.onBrowserCommand?(cmdId, cmd, json)

            case "usage_update":
                // Could display usage info in settings
                break

            case "rate_limited":
                let content = json["content"] as? String ?? "Rate limit reached."
                self.messages.append(ChatMessage(role: .status, content: content))

            case "auth_ok":
                print("[WS] Authenticated as \(json["uid"] as? String ?? "?")")

            case "auth_error":
                let content = json["content"] as? String ?? "Auth failed"
                self.messages.append(ChatMessage(role: .status, content: content))

            case "call_status", "call_transcript", "call_ended":
                self.onCallEvent?(type, json)

            default:
                print("[WS] Unhandled message type: \(type)")
            }
        }
    }

    // MARK: - Keepalive

    private func startPing() {
        pingTimer?.invalidate()
        pingTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.webSocketTask?.sendPing { error in
                if let error {
                    print("[WS] Ping error: \(error)")
                }
            }
        }
    }

    // MARK: - Reconnection

    private func attemptReconnect() {
        guard reconnectAttempts < maxReconnectAttempts else {
            print("[WS] Max reconnect attempts reached")
            return
        }

        reconnectAttempts += 1
        let delay = min(pow(2.0, Double(reconnectAttempts)), 30.0)
        print("[WS] Reconnecting in \(delay)s (attempt \(reconnectAttempts))")

        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.connect()
        }
    }
}
