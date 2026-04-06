import SwiftUI

@main
struct ProxxyApp: App {
    @StateObject private var webSocketService = WebSocketService()
    @StateObject private var browserBridge = BrowserBridge()
    @StateObject private var sessionManager = SessionManager()
    @StateObject private var settingsManager = SettingsManager()
    @StateObject private var callService = CallService()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(webSocketService)
                .environmentObject(browserBridge)
                .environmentObject(sessionManager)
                .environmentObject(settingsManager)
                .environmentObject(callService)
                .onAppear {
                    browserBridge.ws = webSocketService
                    callService.ws = webSocketService

                    // Track session IDs and send settings
                    webSocketService.onSessionId = { [weak sessionManager, weak settingsManager, weak webSocketService] id in
                        sessionManager?.newSession(id: id)
                        // Send preferences to server
                        if let sm = settingsManager {
                            webSocketService?.sendSettings(voiceId: sm.selectedVoiceId, aboutMe: sm.aboutMe)
                        }
                    }

                    // Track URL visits
                    webSocketService.onURLVisited = { [weak sessionManager] url in
                        sessionManager?.recordURL(url)
                    }

                    // Sync messages to session + narration
                    webSocketService.onMessagesChanged = { [weak sessionManager, weak settingsManager, weak webSocketService] messages in
                        sessionManager?.updateMessages(messages)

                        // Narrate new assistant messages via ElevenLabs
                        if let last = messages.last,
                           last.role == .assistant,
                           let sm = settingsManager,
                           sm.narrationEnabled,
                           !sm.selectedVoiceId.isEmpty,
                           let serverURL = webSocketService?.serverURL {
                            NarrationService.shared.speak(last.content, voiceId: sm.selectedVoiceId, serverURL: serverURL)
                        }
                    }

                    // Sync voice selection to call service
                    callService.voiceId = settingsManager.selectedVoiceId

                    // Handle call events
                    webSocketService.onCallEvent = { [weak callService] type, data in
                        callService?.handleEvent(type: type, data: data)
                    }
                }
        }
    }
}
