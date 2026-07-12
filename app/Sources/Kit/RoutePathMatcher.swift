import Foundation

public enum RoutePathMatcher {
    public static func suggestions(
        in pages: [WikiPageSummary],
        matching query: String,
        limit: Int = 8
    ) -> [WikiPageSummary] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty, limit > 0 else {
            return []
        }
        return pages
            .filter { page in
                page.relative_path.lowercased().contains(needle)
                    || page.displayTitle.lowercased().contains(needle)
            }
            .sorted { left, right in
                let leftRank = matchRank(left, needle: needle)
                let rightRank = matchRank(right, needle: needle)
                return leftRank == rightRank
                    ? left.relative_path.localizedStandardCompare(right.relative_path)
                        == .orderedAscending
                    : leftRank < rightRank
            }
            .prefix(limit)
            .map { $0 }
    }

    private static func matchRank(_ page: WikiPageSummary, needle: String) -> Int {
        let path = page.relative_path.lowercased()
        let title = page.displayTitle.lowercased()
        if path == needle { return 0 }
        if path.hasPrefix(needle) { return 1 }
        if title.hasPrefix(needle) { return 2 }
        if path.contains(needle) { return 3 }
        return 4
    }
}
