import Foundation

/// A conversation session with the agent.
struct Session: Identifiable, Codable {
    let id: String           // matches backend session_id
    var title: String        // first user message or "New Chat"
    var messages: [ChatMessage]
    var urls: [URLVisit]     // pages the agent navigated to
    var createdAt: Date
    var updatedAt: Date

    init(id: String, title: String = "New Chat") {
        self.id = id
        self.title = title
        self.messages = []
        self.urls = []
        self.createdAt = Date()
        self.updatedAt = Date()
    }
}

/// A URL visited during a session.
struct URLVisit: Codable {
    let url: String
    let title: String?
    let timestamp: Date

    init(url: String, title: String? = nil) {
        self.url = url
        self.title = title
        self.timestamp = Date()
    }
}
