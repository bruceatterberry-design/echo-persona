// Read a scanned PDF with Apple's Vision framework (on this Mac, nothing sent anywhere) and make it searchable.
// usage: pdfread <in.pdf> <out_dir>
//   out_dir/text.json      [{"page": 1, "text": "..."}, ...]   (top to bottom)
//   out_dir/searchable.pdf the same pages with the recognised text laid invisibly on top (select / search / copy)
//   out_dir/thumb.jpg      first page, 360 px wide
import AppKit
import CoreText
import Foundation
import PDFKit
import Vision

func fail(_ m: String) -> Never { FileHandle.standardError.write((m + "\n").data(using: .utf8)!); exit(1) }
let a = CommandLine.arguments
guard a.count == 3, let doc = PDFDocument(url: URL(fileURLWithPath: a[1])) else { fail("usage: pdfread <in.pdf> <out_dir>") }
let out = URL(fileURLWithPath: a[2])
guard let ctx = CGContext(out.appendingPathComponent("searchable.pdf") as CFURL, mediaBox: nil, nil) else { fail("cannot write pdf") }
var pages: [[String: Any]] = []

for i in 0..<doc.pageCount {
    guard let page = doc.page(at: i) else { continue }
    let box = page.bounds(for: .mediaBox)
    let scale = min(4.0, 2400.0 / max(box.width, box.height))          // OCR at ~2400 px on the long side
    let w = Int(box.width * scale), h = Int(box.height * scale)
    guard let bctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                               space: CGColorSpaceCreateDeviceRGB(),
                               bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { continue }
    bctx.setFillColor(.white); bctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    bctx.scaleBy(x: scale, y: scale); bctx.translateBy(x: -box.origin.x, y: -box.origin.y)
    page.draw(with: .mediaBox, to: bctx)
    guard let img = bctx.makeImage() else { continue }
    if i == 0 {
        let tw = 360, th = Int(360.0 * Double(h) / Double(w))
        if let t = CGContext(data: nil, width: tw, height: th, bitsPerComponent: 8, bytesPerRow: 0,
                             space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
            t.interpolationQuality = .high; t.draw(img, in: CGRect(x: 0, y: 0, width: tw, height: th))
            if let ti = t.makeImage(), let jpg = NSBitmapImageRep(cgImage: ti).representation(using: .jpeg, properties: [.compressionFactor: 0.8]) {
                try? jpg.write(to: out.appendingPathComponent("thumb.jpg"))
            }
        }
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate; req.usesLanguageCorrection = true; req.recognitionLanguages = ["en-US"]
    try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    let obs = (req.results ?? []).sorted { $0.boundingBox.maxY > $1.boundingBox.maxY }
    pages.append(["page": i + 1, "text": obs.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")])

    var mb = box
    let info = [kCGPDFContextMediaBox as String: NSData(bytes: &mb, length: MemoryLayout<CGRect>.size)] as CFDictionary
    ctx.beginPDFPage(info)
    page.draw(with: .mediaBox, to: ctx)                                 // the page as scanned
    ctx.setTextDrawingMode(.invisible)                                  // + the words, invisible, where they are
    for o in obs {
        guard let s = o.topCandidates(1).first?.string, !s.isEmpty else { continue }
        let r = CGRect(x: box.minX + o.boundingBox.minX * box.width, y: box.minY + o.boundingBox.minY * box.height,
                       width: o.boundingBox.width * box.width, height: o.boundingBox.height * box.height)
        let font = CTFontCreateWithName("Helvetica" as CFString, max(1, r.height * 0.85), nil)
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: s, attributes: [.font: font]))
        let lw = CTLineGetTypographicBounds(line, nil, nil, nil)
        guard lw > 0 else { continue }
        ctx.saveGState()
        ctx.textMatrix = .identity
        ctx.translateBy(x: r.minX, y: r.minY + r.height * 0.15)
        ctx.scaleBy(x: r.width / lw, y: 1)
        ctx.textPosition = .zero
        CTLineDraw(line, ctx)
        ctx.restoreGState()
    }
    ctx.endPDFPage()
}
ctx.closePDF()
let data = try JSONSerialization.data(withJSONObject: pages, options: [])
try data.write(to: out.appendingPathComponent("text.json"))
