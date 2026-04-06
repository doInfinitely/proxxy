import SwiftUI

/// Editor for creating/editing a prompt template.
struct TemplateEditorView: View {
    @Environment(\.dismiss) var dismiss
    @EnvironmentObject var ws: WebSocketService

    @State private var name = ""
    @State private var prompt = ""
    @State private var questions: [String] = []
    @State private var isGenerating = false
    @State private var errorMessage: String?

    var onSave: (Template) -> Void

    var body: some View {
        NavigationView {
            Form {
                Section("Template Name") {
                    TextField("e.g. Order food", text: $name)
                }

                Section {
                    TextEditor(text: $prompt)
                        .frame(minHeight: 80)
                } header: {
                    Text("Prompt")
                } footer: {
                    Text("Use {param} for placeholders. Example: \"Order {item} from {restaurant}\"")
                        .font(.caption)
                }

                if !extractedParams.isEmpty {
                    Section("Detected Parameters") {
                        ForEach(extractedParams, id: \.self) { param in
                            Label(param, systemImage: "textformat.abc")
                        }
                    }
                }

                if !questions.isEmpty {
                    Section("Generated Questions") {
                        ForEach(questions, id: \.self) { q in
                            Text(q)
                                .font(.subheadline)
                        }
                    }
                }

                if let error = errorMessage {
                    Section {
                        Text(error)
                            .foregroundColor(.red)
                            .font(.caption)
                    }
                }

                Section {
                    Button(action: generateQuestions) {
                        HStack {
                            if isGenerating {
                                ProgressView()
                                    .padding(.trailing, 4)
                            }
                            Text(questions.isEmpty ? "Generate Questions" : "Regenerate Questions")
                        }
                    }
                    .disabled(prompt.isEmpty || extractedParams.isEmpty || isGenerating)
                }
            }
            .navigationTitle("New Template")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { saveTemplate() }
                        .disabled(name.isEmpty || prompt.isEmpty)
                }
            }
        }
    }

    private var extractedParams: [String] {
        Template.extractParameters(from: prompt)
    }

    private func generateQuestions() {
        isGenerating = true
        errorMessage = nil

        let serverURL = ws.serverURL
            .replacingOccurrences(of: "ws://", with: "http://")
            .replacingOccurrences(of: "wss://", with: "https://")
            .replacingOccurrences(of: "/ws", with: "")

        guard let url = URL(string: "\(serverURL)/api/templates/generate-questions") else {
            errorMessage = "Invalid server URL"
            isGenerating = false
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = ["prompt": prompt, "parameters": extractedParams]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: request) { data, _, error in
            DispatchQueue.main.async {
                isGenerating = false
                if let error {
                    errorMessage = error.localizedDescription
                    return
                }
                guard let data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let qs = json["questions"] as? [String] else {
                    errorMessage = "Failed to parse response"
                    return
                }
                questions = qs
            }
        }.resume()
    }

    private func saveTemplate() {
        let template = Template(
            name: name,
            prompt: prompt,
            questions: questions,
            parameterNames: extractedParams
        )
        onSave(template)
        dismiss()
    }
}
