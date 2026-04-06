import SwiftUI

/// Phone call interface with live transcript and controls.
struct CallView: View {
    @EnvironmentObject var ws: WebSocketService
    @EnvironmentObject var callService: CallService
    @EnvironmentObject var settings: SettingsManager

    @State private var phoneNumber = ""
    @State private var businessName = ""

    var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                if callService.isCallActive {
                    // Active call UI
                    activeCallView
                } else {
                    // Dial UI
                    dialView
                }
            }
            .navigationTitle("Phone Call")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    // MARK: - Dial View

    private var dialView: some View {
        VStack(spacing: 20) {
            Spacer()

            Image(systemName: "phone.circle.fill")
                .font(.system(size: 80))
                .foregroundColor(.green)

            TextField("Business name (optional)", text: $businessName)
                .textFieldStyle(.roundedBorder)
                .padding(.horizontal, 40)

            TextField("Phone number", text: $phoneNumber)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.phonePad)
                .padding(.horizontal, 40)

            Button(action: {
                callService.voiceId = settings.selectedVoiceId
                callService.startCall(phone: phoneNumber, businessName: businessName)
            }) {
                Label("Call", systemImage: "phone.fill")
                    .font(.headline)
                    .foregroundColor(.white)
                    .frame(width: 160, height: 50)
                    .background(Color.green)
                    .cornerRadius(25)
            }
            .disabled(phoneNumber.isEmpty)

            Spacer()
        }
    }

    // MARK: - Active Call View

    private var activeCallView: some View {
        VStack(spacing: 0) {
            // Status bar
            HStack {
                Circle()
                    .fill(statusColor)
                    .frame(width: 10, height: 10)
                Text(callService.callStatus.capitalized)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
                Spacer()
                if callService.isTakenOver {
                    Text("You're on the call")
                        .font(.caption)
                        .foregroundColor(.blue)
                }
            }
            .padding()
            .background(Color(.systemGray6))

            // Transcript
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(callService.transcript) { entry in
                            TranscriptBubble(entry: entry)
                                .id(entry.id)
                        }
                    }
                    .padding()
                }
                .onChange(of: callService.transcript.count) {
                    if let last = callService.transcript.last {
                        withAnimation {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }

            Divider()

            // Call controls
            HStack(spacing: 30) {
                if callService.isTakenOver {
                    Button(action: { callService.handback() }) {
                        VStack {
                            Image(systemName: "arrow.uturn.backward.circle.fill")
                                .font(.system(size: 40))
                                .foregroundColor(.blue)
                            Text("Hand Back")
                                .font(.caption)
                        }
                    }
                } else {
                    Button(action: { callService.takeover() }) {
                        VStack {
                            Image(systemName: "person.wave.2.fill")
                                .font(.system(size: 40))
                                .foregroundColor(.blue)
                            Text("Take Over")
                                .font(.caption)
                        }
                    }
                }

                Button(action: { callService.endCall() }) {
                    VStack {
                        Image(systemName: "phone.down.circle.fill")
                            .font(.system(size: 40))
                            .foregroundColor(.red)
                        Text("End Call")
                            .font(.caption)
                    }
                }
            }
            .padding()
        }
    }

    private var statusColor: Color {
        switch callService.callStatus {
        case "ringing": return .orange
        case "in-progress", "answered": return .green
        case "ended", "completed", "failed": return .red
        default: return .gray
        }
    }
}

/// A single transcript entry bubble.
struct TranscriptBubble: View {
    let entry: CallService.TranscriptEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(speakerLabel)
                .font(.caption2)
                .foregroundColor(speakerColor)
                .fontWeight(.semibold)
            Text(entry.text)
                .font(.subheadline)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(speakerColor.opacity(0.1))
        .cornerRadius(8)
    }

    private var speakerLabel: String {
        switch entry.speaker {
        case "agent": return "AI Agent"
        case "business": return "Business"
        case "user": return "You"
        case "system": return "System"
        default: return entry.speaker.capitalized
        }
    }

    private var speakerColor: Color {
        switch entry.speaker {
        case "agent": return .purple
        case "business": return .blue
        case "user": return .green
        case "system": return .gray
        default: return .secondary
        }
    }
}
