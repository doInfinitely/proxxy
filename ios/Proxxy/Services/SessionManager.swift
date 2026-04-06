import Foundation
import Combine

/// Manages session lifecycle: create, switch, save, delete.
final class SessionManager: ObservableObject {
    @Published var sessions: [Session] = []
    @Published var currentSession: Session?

    private let storage = StorageService.shared
    private var cancellables = Set<AnyCancellable>()

    init() {
        sessions = storage.loadSessions()
    }

    /// Create a new session with the given ID from WebSocket.
    func newSession(id: String) {
        saveCurrentSession()
        let session = Session(id: id)
        currentSession = session
    }

    /// Switch to an existing session by ID.
    func switchSession(id: String) {
        saveCurrentSession()
        if let session = sessions.first(where: { $0.id == id }) {
            currentSession = session
            // Remove from saved list — it's now "active"
            sessions.removeAll { $0.id == id }
        }
    }

    /// Save the current session to the list and persist.
    func saveCurrentSession() {
        guard var session = currentSession else { return }
        // Only save if there are messages
        guard !session.messages.isEmpty else { return }

        session.updatedAt = Date()

        // Update title from first user message if still default
        if session.title == "New Chat" {
            if let firstUserMsg = session.messages.first(where: { $0.role == .user }) {
                session.title = String(firstUserMsg.content.prefix(50))
            }
        }

        // Replace or append
        if let idx = sessions.firstIndex(where: { $0.id == session.id }) {
            sessions[idx] = session
        } else {
            sessions.insert(session, at: 0)
        }

        storage.saveSessions(sessions)
    }

    /// Update messages on the current session.
    func updateMessages(_ messages: [ChatMessage]) {
        currentSession?.messages = messages
    }

    /// Record a URL visit on the current session.
    func recordURL(_ url: String, title: String? = nil) {
        currentSession?.urls.append(URLVisit(url: url, title: title))
    }

    /// Delete a session by ID.
    func deleteSession(id: String) {
        sessions.removeAll { $0.id == id }
        storage.saveSessions(sessions)
    }
}
