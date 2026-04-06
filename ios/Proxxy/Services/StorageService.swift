import Foundation

/// Simple JSON file persistence for sessions and templates.
final class StorageService {
    static let shared = StorageService()

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }()

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    private var documentsDir: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    // MARK: - Sessions

    private var sessionsURL: URL { documentsDir.appendingPathComponent("sessions.json") }

    func loadSessions() -> [Session] {
        guard let data = try? Data(contentsOf: sessionsURL) else { return [] }
        return (try? decoder.decode([Session].self, from: data)) ?? []
    }

    func saveSessions(_ sessions: [Session]) {
        guard let data = try? encoder.encode(sessions) else { return }
        try? data.write(to: sessionsURL, options: .atomic)
    }

    // MARK: - Templates

    private var templatesURL: URL { documentsDir.appendingPathComponent("templates.json") }

    func loadTemplates() -> [Template] {
        guard let data = try? Data(contentsOf: templatesURL) else { return [] }
        return (try? decoder.decode([Template].self, from: data)) ?? []
    }

    func saveTemplates(_ templates: [Template]) {
        guard let data = try? encoder.encode(templates) else { return }
        try? data.write(to: templatesURL, options: .atomic)
    }
}
