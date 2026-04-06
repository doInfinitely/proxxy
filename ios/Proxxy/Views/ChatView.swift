import SwiftUI

/// Chat message list with input bar at the bottom.
struct ChatView: View {
    @EnvironmentObject var ws: WebSocketService
    @EnvironmentObject var callService: CallService
    @State private var inputText = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            // Call status bar — shown when a call is active
            if callService.isCallActive {
                CallStatusBar(callService: callService)
            }

            // Message list — scrollable
            ScrollViewReader { proxy in
                ScrollView(.vertical, showsIndicators: true) {
                    LazyVStack(spacing: 0) {
                        ForEach(Array(ws.messages.enumerated()), id: \.element.id) { index, message in
                            // Skip status bubbles — shown as activityText instead
                            if message.role != .status {
                                ChatBubble(message: message)
                                    .id(message.id)

                                // Show activity text under the last user bubble
                                if message.role == .user,
                                   !ws.messages.suffix(from: index + 1).contains(where: { $0.role == .user }),
                                   !ws.activityText.isEmpty {
                                    Text(ws.activityText)
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                        .frame(maxWidth: .infinity, alignment: .trailing)
                                        .padding(.horizontal, 16)
                                        .padding(.top, 2)
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                }
                            }
                        }

                        // Thinking dots in the chat (left-aligned, like next assistant bubble)
                        if ws.status == "thinking" {
                            ThinkingDotsView()
                                .id("thinking-dots")
                        }
                    }
                    .padding(.vertical, 8)
                }
                .scrollDismissesKeyboard(.immediately)
                .onTapGesture {
                    inputFocused = false
                }
                .onChange(of: ws.messages.count) {
                    if ws.status == "thinking" {
                        withAnimation(.easeOut(duration: 0.2)) {
                            proxy.scrollTo("thinking-dots", anchor: .bottom)
                        }
                    } else if let last = ws.messages.last {
                        withAnimation(.easeOut(duration: 0.2)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
                .onChange(of: ws.status) {
                    if ws.status == "thinking" {
                        withAnimation(.easeOut(duration: 0.2)) {
                            proxy.scrollTo("thinking-dots", anchor: .bottom)
                        }
                    }
                }
                .onChange(of: inputFocused) {
                    // Scroll to bottom when keyboard appears
                    if inputFocused, let last = ws.messages.last {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                            withAnimation {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        }
                    }
                }
            }

            Divider()

            // Input bar
            HStack(spacing: 8) {
                TextField("Ask anything...", text: $inputText, axis: .vertical)
                    .textFieldStyle(.plain)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Color(.systemGray6))
                    .cornerRadius(20)
                    .focused($inputFocused)
                    .lineLimit(1...5)
                    .submitLabel(.send)
                    .onSubmit {
                        sendMessage()
                    }

                Button(action: sendMessage) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 30))
                        .foregroundColor(inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .gray : .blue)
                }
                .disabled(inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color(.systemBackground))
        }
    }

    private func sendMessage() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        ws.sendMessage(text)
        inputText = ""
    }
}

/// Compact bar shown during an active phone call with status and controls.
struct CallStatusBar: View {
    @ObservedObject var callService: CallService

    var body: some View {
        HStack(spacing: 12) {
            // Status indicator
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)

            Text(callService.callStatus.isEmpty ? "Calling..." : callService.callStatus.capitalized)
                .font(.subheadline)
                .fontWeight(.medium)

            Spacer()

            // Takeover / Handback
            if callService.isTakenOver {
                Button(action: { callService.handback() }) {
                    Label("Hand Back", systemImage: "arrow.uturn.backward")
                        .font(.caption)
                        .fontWeight(.medium)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            } else {
                Button(action: { callService.takeover() }) {
                    Label("Take Over", systemImage: "person.wave.2")
                        .font(.caption)
                        .fontWeight(.medium)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            // End call
            Button(action: { callService.endCall() }) {
                Image(systemName: "phone.down.fill")
                    .font(.system(size: 14))
                    .foregroundColor(.white)
                    .padding(6)
                    .background(Color.red)
                    .clipShape(Circle())
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color(.systemGray6))
    }

    private var statusColor: Color {
        switch callService.callStatus {
        case "ringing": return .orange
        case "in-progress", "answered": return .green
        default: return .gray
        }
    }
}
