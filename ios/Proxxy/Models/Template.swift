import Foundation

/// A reusable prompt template with parameter placeholders.
struct Template: Identifiable, Codable {
    let id: UUID
    var name: String             // e.g. "Order food"
    var prompt: String           // e.g. "Order {item} from {restaurant}"
    var questions: [String]      // LLM-generated: ["What restaurant?", "What item?"]
    var parameterNames: [String] // extracted: ["item", "restaurant"]
    var isActive: Bool
    var createdAt: Date

    init(name: String, prompt: String, questions: [String] = [], parameterNames: [String] = []) {
        self.id = UUID()
        self.name = name
        self.prompt = prompt
        self.questions = questions
        self.parameterNames = parameterNames
        self.isActive = true
        self.createdAt = Date()
    }

    /// Extract parameter names from `{param}` placeholders in the prompt.
    static func extractParameters(from prompt: String) -> [String] {
        let pattern = "\\{([^}]+)\\}"
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return [] }
        let range = NSRange(prompt.startIndex..., in: prompt)
        let matches = regex.matches(in: prompt, range: range)
        return matches.compactMap { match in
            guard let paramRange = Range(match.range(at: 1), in: prompt) else { return nil }
            return String(prompt[paramRange])
        }
    }

    /// Fill the template prompt with parameter values.
    func fill(with params: [String: String]) -> String {
        var result = prompt
        for (key, value) in params {
            result = result.replacingOccurrences(of: "{\(key)}", with: value)
        }
        return result
    }
}
