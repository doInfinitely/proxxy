import SwiftUI

/// A single chat message bubble.
struct ChatBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack(alignment: .top) {
            if message.role == .user {
                Spacer(minLength: 60)
            }

            VStack(alignment: message.role == .user ? .trailing : .leading, spacing: 2) {
                Text(message.content)
                    .font(.body)
                    .foregroundColor(textColor)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(backgroundColor)
                    .cornerRadius(18)
            }

            if message.role != .user {
                Spacer(minLength: 60)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 2)
    }

    private var backgroundColor: Color {
        switch message.role {
        case .user:
            return .blue
        case .assistant, .done:
            return Color(.systemGray5)
        case .status:
            return Color(.systemGray6)
        }
    }

    private var textColor: Color {
        switch message.role {
        case .user:
            return .white
        case .assistant, .done:
            return .primary
        case .status:
            return .secondary
        }
    }
}
