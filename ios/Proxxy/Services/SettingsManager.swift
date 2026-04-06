import Foundation
import SwiftUI

/// Manages user preferences via @AppStorage.
final class SettingsManager: ObservableObject {
    @AppStorage("selectedVoiceId") var selectedVoiceId: String = ""
    @AppStorage("narrationEnabled") var narrationEnabled: Bool = false
    @AppStorage("aboutMe") var aboutMe: String = ""
}
