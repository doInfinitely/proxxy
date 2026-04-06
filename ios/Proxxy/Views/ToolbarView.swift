import SwiftUI

/// URL bar and navigation controls above the browser view.
struct ToolbarView: View {
    @EnvironmentObject var ws: WebSocketService
    @EnvironmentObject var bridge: BrowserBridge
    @State private var isEditing = false
    @State private var editText = ""
    @FocusState private var urlFocused: Bool

    @EnvironmentObject var sessionManager: SessionManager

    var body: some View {
        HStack(spacing: 8) {
            // New chat button
            Button(action: {
                sessionManager.saveCurrentSession()
                ws.startNewSession()
            }) {
                Image(systemName: "plus.circle")
                    .font(.system(size: 16, weight: .medium))
            }

            // Back button
            Button(action: {
                bridge.webView?.goBack()
            }) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 16, weight: .medium))
            }
            .disabled(!(bridge.webView?.canGoBack ?? false))

            // Forward button
            Button(action: {
                bridge.webView?.goForward()
            }) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 16, weight: .medium))
            }
            .disabled(!(bridge.webView?.canGoForward ?? false))

            // URL bar — tappable to edit, shows TextField when editing
            if isEditing {
                TextField("Enter URL or search", text: $editText)
                    .font(.system(size: 13))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .submitLabel(.go)
                    .focused($urlFocused)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(Color(.systemGray6))
                    .cornerRadius(8)
                    .onSubmit {
                        navigateTo(editText)
                    }
                    .onAppear {
                        urlFocused = true
                    }

                Button(action: {
                    isEditing = false
                    urlFocused = false
                }) {
                    Text("Cancel")
                        .font(.system(size: 13))
                }
            } else {
                Text(displayURL)
                    .font(.system(size: 13))
                    .foregroundColor(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(Color(.systemGray6))
                    .cornerRadius(8)
                    .onTapGesture {
                        editText = ws.currentURL
                        isEditing = true
                    }
            }

            // Reload button
            if !isEditing {
                Button(action: {
                    bridge.webView?.reload()
                }) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 14, weight: .medium))
                }

                // Connection status indicator
                Circle()
                    .fill(ws.isConnected ? Color.green : Color.red)
                    .frame(width: 8, height: 8)

                // Stop button (only visible when agent is working)
                if ws.status == "thinking" {
                    Button(action: {
                        ws.sendStop()
                    }) {
                        Image(systemName: "stop.fill")
                            .font(.system(size: 14))
                            .foregroundColor(.red)
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color(.systemBackground))
    }

    private var displayURL: String {
        let url = ws.currentURL
        if url.isEmpty || url == "about:blank" {
            return "Proxxy"
        }
        // Show just the host
        if let components = URLComponents(string: url) {
            return components.host ?? url
        }
        return url
    }

    private func navigateTo(_ input: String) {
        isEditing = false
        urlFocused = false

        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        let urlString: String
        if trimmed.contains(".") && !trimmed.contains(" ") {
            // Looks like a URL
            urlString = trimmed.hasPrefix("http") ? trimmed : "https://\(trimmed)"
        } else {
            // Treat as a search query
            let encoded = trimmed.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? trimmed
            urlString = "https://www.google.com/search?q=\(encoded)"
        }

        guard let url = URL(string: urlString) else { return }
        DispatchQueue.main.async {
            bridge.webView?.load(URLRequest(url: url))
        }
    }
}
