import Foundation
import Markdown
import SwiftUI

struct MarkdownDocumentView: View {
    private let blocks: [Markup]

    init(markdown: String) {
        blocks = Array(Document(parsing: markdown).children)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                MarkdownBlockView(block: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct MarkdownBlockView: View {
    let block: Markup

    @ViewBuilder
    var body: some View {
        if let heading = block as? Heading {
            SwiftUI.Text(attributed(block))
                .font(headingFont(heading.level))
                .fontWeight(.semibold)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, heading.level <= 2 ? 6 : 2)
        } else if let code = block as? CodeBlock {
            ScrollView(.horizontal) {
                SwiftUI.Text(code.code)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(Color.secondary.opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        } else if block is ThematicBreak {
            Divider()
        } else if block is BlockQuote {
            HStack(alignment: .top, spacing: 10) {
                Rectangle()
                    .fill(Color.accentColor.opacity(0.55))
                    .frame(width: 3)
                SwiftUI.Text(attributed(block))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            SwiftUI.Text(attributed(block))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func attributed(_ markup: Markup) -> AttributedString {
        (try? AttributedString(
            markdown: markup.format(),
            options: .init(
                interpretedSyntax: .full,
                failurePolicy: .returnPartiallyParsedIfPossible
            )
        )) ?? AttributedString(markup.format())
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title2
        case 2: return .title3
        case 3: return .headline
        default: return .subheadline
        }
    }
}
