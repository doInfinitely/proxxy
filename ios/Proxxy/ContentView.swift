import SwiftUI

/// Root view — tab-based navigation with Chat, History, Templates, and Settings.
struct ContentView: View {
    @EnvironmentObject var ws: WebSocketService
    @EnvironmentObject var sessionManager: SessionManager
    @State var selectedTab = 0

    var body: some View {
        TabView(selection: $selectedTab) {
            ChatTab()
                .tabItem {
                    Label("Chat", systemImage: "bubble.left.and.bubble.right")
                }
                .tag(0)

            HistoryView()
                .tabItem {
                    Label("History", systemImage: "clock")
                }
                .tag(1)

            TemplateListView()
                .tabItem {
                    Label("Templates", systemImage: "doc.text")
                }
                .tag(2)

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gear")
                }
                .tag(3)
        }
        .onAppear {
            ws.connect()
        }
        .onReceive(NotificationCenter.default.publisher(for: .switchToChat)) { _ in
            selectedTab = 0
        }
    }
}

/// The main chat tab with browser + chat split.
struct ChatTab: View {
    @EnvironmentObject var ws: WebSocketService
    @EnvironmentObject var sessionManager: SessionManager
    @State private var browserExpanded = false

    var body: some View {
        GeometryReader { geo in
            VStack(spacing: 0) {
                ToolbarView()

                BrowserView()
                    .frame(height: browserExpanded ? geo.size.height * 0.65 : geo.size.height * 0.35)
                    .clipped()

                HStack {
                    Spacer()
                    Capsule()
                        .fill(Color.secondary.opacity(0.4))
                        .frame(width: 40, height: 5)
                    Spacer()
                }
                .padding(.vertical, 4)
                .background(Color(.systemBackground))
                .onTapGesture {
                    withAnimation(.easeInOut(duration: 0.25)) {
                        browserExpanded.toggle()
                    }
                }

                ChatView()
                    .frame(maxHeight: .infinity)
            }
        }
    }
}

#Preview {
    ContentView()
        .environmentObject(WebSocketService())
        .environmentObject(SessionManager())
}
