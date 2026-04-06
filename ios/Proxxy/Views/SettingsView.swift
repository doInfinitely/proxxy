import SwiftUI

/// Settings tab for voice, narration, and connection info.
struct SettingsView: View {
    @EnvironmentObject var settings: SettingsManager
    @EnvironmentObject var ws: WebSocketService

    @State private var voices: [VoiceOption] = []
    @State private var isLoadingVoices = false

    var body: some View {
        NavigationView {
            Form {
                Section {
                    if isLoadingVoices {
                        HStack {
                            ProgressView()
                                .padding(.trailing, 8)
                            Text("Loading voices...")
                                .foregroundColor(.secondary)
                        }
                    } else if voices.isEmpty {
                        Text("No voices available")
                            .foregroundColor(.secondary)
                    } else {
                        Picker("Voice", selection: $settings.selectedVoiceId) {
                            ForEach(voices) { voice in
                                Text(voice.name).tag(voice.id)
                            }
                        }
                    }

                    Button("Preview Voice") {
                        NarrationService.shared.speak(
                            "Hello, I'm your Proxxy assistant. How can I help you today?",
                            voiceId: settings.selectedVoiceId,
                            serverURL: ws.serverURL
                        )
                    }
                    .disabled(settings.selectedVoiceId.isEmpty)
                } header: {
                    Text("Agent Voice")
                } footer: {
                    Text("Used for phone calls and narration.")
                }

                Section {
                    TextEditor(text: $settings.aboutMe)
                        .frame(minHeight: 100)
                        .overlay(alignment: .topLeading) {
                            if settings.aboutMe.isEmpty {
                                Text("My name is... I live in... I like...")
                                    .foregroundColor(.secondary)
                                    .padding(.top, 8)
                                    .padding(.leading, 4)
                                    .allowsHitTesting(false)
                            }
                        }
                } header: {
                    Text("About Me")
                } footer: {
                    Text("Proxxy uses this for browsing and phone calls.")
                }

                Section("Narration") {
                    Toggle("Read assistant messages aloud", isOn: $settings.narrationEnabled)
                }

                Section("Connection") {
                    HStack {
                        Text("Status")
                        Spacer()
                        HStack(spacing: 6) {
                            Circle()
                                .fill(ws.isConnected ? Color.green : Color.red)
                                .frame(width: 8, height: 8)
                            Text(ws.isConnected ? "Connected" : "Disconnected")
                                .font(.subheadline)
                                .foregroundColor(.secondary)
                        }
                    }

                    if !ws.sessionId.isEmpty {
                        HStack {
                            Text("Session")
                            Spacer()
                            Text(ws.sessionId)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }

                Section("About") {
                    HStack {
                        Text("Version")
                        Spacer()
                        Text("1.0.0")
                            .foregroundColor(.secondary)
                    }
                }
            }
            .navigationTitle("Settings")
            .onTapGesture {
                UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            }
            .onAppear {
                loadVoices()
            }
        }
    }

    private func loadVoices() {
        guard !isLoadingVoices else { return }
        isLoadingVoices = true

        let serverURL = ws.serverURL
            .replacingOccurrences(of: "ws://", with: "http://")
            .replacingOccurrences(of: "wss://", with: "https://")
            .replacingOccurrences(of: "/ws", with: "")

        guard let url = URL(string: "\(serverURL)/api/voices") else {
            isLoadingVoices = false
            return
        }

        URLSession.shared.dataTask(with: url) { data, _, _ in
            DispatchQueue.main.async {
                isLoadingVoices = false
                guard let data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let voiceList = json["voices"] as? [[String: Any]] else {
                    return
                }
                voices = voiceList.compactMap { v in
                    guard let id = v["voice_id"] as? String,
                          let name = v["name"] as? String else { return nil }
                    return VoiceOption(id: id, name: name)
                }

                // Auto-select first voice if none selected
                if settings.selectedVoiceId.isEmpty, let first = voices.first {
                    settings.selectedVoiceId = first.id
                }
            }
        }.resume()
    }
}

struct VoiceOption: Identifiable {
    let id: String
    let name: String
}
