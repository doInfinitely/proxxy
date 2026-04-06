import SwiftUI

/// Shows past conversation sessions, sorted by most recent.
struct HistoryView: View {
    @EnvironmentObject var sessionManager: SessionManager
    @EnvironmentObject var ws: WebSocketService

    var body: some View {
        NavigationView {
            Group {
                if sessionManager.sessions.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "clock")
                            .font(.system(size: 40))
                            .foregroundColor(.secondary)
                        Text("No history yet")
                            .font(.headline)
                            .foregroundColor(.secondary)
                        Text("Your conversations will appear here.")
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                    }
                } else {
                    List {
                        ForEach(sortedSessions) { session in
                            Button(action: {
                                resumeSession(session)
                            }) {
                                SessionRow(session: session)
                            }
                            .buttonStyle(.plain)
                        }
                        .onDelete(perform: deleteSessions)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("History")
        }
    }

    private var sortedSessions: [Session] {
        sessionManager.sessions.sorted { $0.updatedAt > $1.updatedAt }
    }

    private func resumeSession(_ session: Session) {
        sessionManager.switchSession(id: session.id)
        ws.reconnectWithSession(session.id, messages: session.messages)
        // Switch to chat tab
        if let contentView = findContentView() {
            contentView.selectedTab = 0
        }
    }

    private func deleteSessions(at offsets: IndexSet) {
        let sorted = sortedSessions
        for index in offsets {
            sessionManager.deleteSession(id: sorted[index].id)
        }
    }

    /// Walk up to ContentView to switch tabs.
    private func findContentView() -> ContentView? {
        // Tab switching is handled via binding — use notification instead
        NotificationCenter.default.post(name: .switchToChat, object: nil)
        return nil
    }
}

/// A single row in the history list.
struct SessionRow: View {
    let session: Session

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(session.title)
                    .font(.headline)
                    .lineLimit(1)
                Spacer()
                Text(session.updatedAt, style: .relative)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            HStack(spacing: 12) {
                Label("\(session.messages.count)", systemImage: "bubble.left")
                    .font(.caption)
                    .foregroundColor(.secondary)

                if let firstURL = session.urls.first {
                    Text(shortURL(firstURL.url))
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func shortURL(_ url: String) -> String {
        if let components = URLComponents(string: url) {
            return components.host ?? url
        }
        return url
    }
}

extension Notification.Name {
    static let switchToChat = Notification.Name("switchToChat")
}
