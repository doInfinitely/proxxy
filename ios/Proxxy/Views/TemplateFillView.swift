import SwiftUI

/// Fill a template's parameters and send as a chat message.
struct TemplateFillView: View {
    @Environment(\.dismiss) var dismiss
    @EnvironmentObject var ws: WebSocketService

    let template: Template

    @State private var paramValues: [String: String] = [:]
    @State private var naturalInput = ""
    @State private var isExtracting = false

    var body: some View {
        NavigationView {
            Form {
                Section("Template") {
                    Text(template.prompt)
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                }

                if !template.questions.isEmpty {
                    Section("Fill in the details") {
                        ForEach(Array(zip(template.parameterNames, template.questions)), id: \.0) { param, question in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(question)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                                TextField(param, text: binding(for: param))
                            }
                        }
                    }
                } else {
                    Section("Parameters") {
                        ForEach(template.parameterNames, id: \.self) { param in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(param)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                                TextField(param, text: binding(for: param))
                            }
                        }
                    }
                }

                Section {
                    HStack {
                        TextField("Or describe in natural language...", text: $naturalInput)
                            .textInputAutocapitalization(.never)
                        Button(action: extractParams) {
                            if isExtracting {
                                ProgressView()
                            } else {
                                Image(systemName: "wand.and.stars")
                            }
                        }
                        .disabled(naturalInput.isEmpty || isExtracting)
                    }
                } footer: {
                    Text("e.g. \"pepperoni pizza from Dominos\"")
                        .font(.caption)
                }

                Section {
                    Button(action: sendMessage) {
                        HStack {
                            Spacer()
                            Text("Send")
                                .fontWeight(.semibold)
                            Spacer()
                        }
                    }
                    .disabled(!allParamsFilled)
                }
            }
            .navigationTitle(template.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    private func binding(for param: String) -> Binding<String> {
        Binding(
            get: { paramValues[param] ?? "" },
            set: { paramValues[param] = $0 }
        )
    }

    private var allParamsFilled: Bool {
        template.parameterNames.allSatisfy { !(paramValues[$0] ?? "").isEmpty }
    }

    private func extractParams() {
        isExtracting = true

        let serverURL = ws.serverURL
            .replacingOccurrences(of: "ws://", with: "http://")
            .replacingOccurrences(of: "wss://", with: "https://")
            .replacingOccurrences(of: "/ws", with: "")

        guard let url = URL(string: "\(serverURL)/api/templates/extract-params") else {
            isExtracting = false
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = [
            "prompt": template.prompt,
            "parameters": template.parameterNames,
            "input": naturalInput,
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: request) { data, _, _ in
            DispatchQueue.main.async {
                isExtracting = false
                guard let data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let params = json["params"] as? [String: String] else {
                    return
                }
                for (key, value) in params {
                    paramValues[key] = value
                }
            }
        }.resume()
    }

    private func sendMessage() {
        let filled = template.fill(with: paramValues)
        ws.sendMessage(filled)
        dismiss()
        NotificationCenter.default.post(name: .switchToChat, object: nil)
    }
}
