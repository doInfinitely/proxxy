import AVFoundation

/// Speaks assistant messages aloud using ElevenLabs TTS via backend.
final class NarrationService {
    static let shared = NarrationService()

    private var player: AVAudioPlayer?
    private var currentTask: URLSessionDataTask?

    /// Speak the given text using ElevenLabs TTS streamed from backend.
    func speak(_ text: String, voiceId: String, serverURL: String) {
        // Cancel any in-flight request
        stop()

        let baseURL = serverURL
            .replacingOccurrences(of: "ws://", with: "http://")
            .replacingOccurrences(of: "wss://", with: "https://")
            .replacingOccurrences(of: "/ws", with: "")

        guard let url = URL(string: "\(baseURL)/api/tts") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: String] = ["text": text, "voice_id": voiceId]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        currentTask = URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let data, error == nil,
                  let httpResp = response as? HTTPURLResponse,
                  httpResp.statusCode == 200 else {
                return
            }

            DispatchQueue.main.async {
                do {
                    try AVAudioSession.sharedInstance().setCategory(.playback)
                    try AVAudioSession.sharedInstance().setActive(true)
                    self?.player = try AVAudioPlayer(data: data)
                    self?.player?.play()
                } catch {
                    print("[Narration] Playback error: \(error)")
                }
            }
        }
        currentTask?.resume()
    }

    /// Stop any current narration.
    func stop() {
        currentTask?.cancel()
        currentTask = nil
        player?.stop()
        player = nil
    }
}
