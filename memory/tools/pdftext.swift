// Print the text of a PDF with Apple's built-in PDFKit (runs on this Mac, nothing is sent anywhere).
// usage: pdftext <file.pdf>  -> the text, page by page (a form feed between pages)
import Foundation
import PDFKit

guard CommandLine.arguments.count > 1, let doc = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else {
    FileHandle.standardError.write("cannot read pdf\n".data(using: .utf8)!); exit(1)
}
var pages: [String] = []
for i in 0..<doc.pageCount { pages.append(doc.page(at: i)?.string ?? "") }
print(pages.joined(separator: "\u{0C}"))
