import Foundation

func countText(_ count: Int?, _ label: String) -> String? {
    guard let count else {
        return nil
    }
    return "\(count) \(label)"
}

func scoreText(_ value: Double?) -> String? {
    guard let value else {
        return nil
    }
    return String(format: "%.2f", value)
}
