import Foundation

/// Manages phone call state via WebSocket messages.
final class CallService: ObservableObject {
    @Published var isCallActive = false
    @Published var callStatus = ""  // "ringing", "in-progress", "ended"
    @Published var transcript: [TranscriptEntry] = []
    @Published var isTakenOver = false

    weak var ws: WebSocketService?

    struct TranscriptEntry: Identifiable {
        let id = UUID()
        let speaker: String  // "agent", "contact", "user"
        let text: String
        let timestamp: Date
    }

    /// Selected ElevenLabs voice ID for calls.
    var voiceId: String = ""

    /// Start a phone call to the given number with contact info.
    func startCall(phone: String, contactName: String = "") {
        var msg: [String: Any] = [
            "type": "start_call",
            "business": [
                "phone": phone,
                "name": contactName,
            ],
        ]
        if !voiceId.isEmpty {
            msg["voice_id"] = voiceId
        }
        ws?.sendJSON(msg)
        isCallActive = true
        callStatus = "ringing"
    }

    /// End the current call.
    func endCall() {
        ws?.sendJSON(["type": "end_call"])
    }

    /// Take over the call (user joins conference).
    func takeover() {
        ws?.sendJSON(["type": "call_takeover"])
        isTakenOver = true
    }

    /// Hand back to the AI agent.
    func handback() {
        ws?.sendJSON(["type": "call_handback"])
        isTakenOver = false
    }

    /// Handle call events from WebSocket.
    func handleEvent(type: String, data: [String: Any]) {
        switch type {
        case "call_status":
            let status = data["status"] as? String ?? ""
            callStatus = status
            if status == "ended" || status == "completed" || status == "failed" {
                isCallActive = false
                isTakenOver = false
            }

        case "call_transcript":
            let speaker = data["speaker"] as? String ?? "agent"
            let text = data["content"] as? String ?? data["text"] as? String ?? ""
            if !text.isEmpty {
                transcript.append(TranscriptEntry(
                    speaker: speaker,
                    text: text,
                    timestamp: Date()
                ))
            }

        case "call_ended":
            callStatus = "ended"
            isCallActive = false
            isTakenOver = false
            if let content = data["content"] as? String, !content.isEmpty {
                transcript.append(TranscriptEntry(
                    speaker: "system",
                    text: content,
                    timestamp: Date()
                ))
            }

        default:
            break
        }
    }

    /// Reset state for a new call.
    func reset() {
        isCallActive = false
        callStatus = ""
        transcript.removeAll()
        isTakenOver = false
    }
}
