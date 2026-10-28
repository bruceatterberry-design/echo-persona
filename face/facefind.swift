// facefind: find faces in photos with Apple's Vision framework (on-device, no network).
// Usage: facefind <out_dir> <image>...
// For each face: writes a square crop (JPEG, 512 px) to out_dir and prints one JSON line with the
// box, landmarks-based roll/yaw, Vision's capture quality, and a feature print for "is this the same person" hints.
import Foundation
import Vision
import ImageIO
import CoreImage
import UniformTypeIdentifiers

func orientedImage(_ url: URL) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                 kCGImageSourceCreateThumbnailWithTransform: true,   // apply EXIF rotation
                                 kCGImageSourceThumbnailMaxPixelSize: 4096]
    return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
}

func featurePrint(_ img: CGImage) -> [Float]? {
    let req = VNGenerateImageFeaturePrintRequest()
    try? VNImageRequestHandler(cgImage: img).perform([req])
    guard let fp = req.results?.first as? VNFeaturePrintObservation, fp.elementType == .float else { return nil }
    return fp.data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
}

func writeJPEG(_ img: CGImage, _ url: URL) {
    guard let dst = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else { return }
    CGImageDestinationAddImage(dst, img, [kCGImageDestinationLossyCompressionQuality: 0.95] as CFDictionary)
    CGImageDestinationFinalize(dst)
}

let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("usage: facefind <out_dir> <image>...\n".data(using: .utf8)!); exit(2) }
let outDir = URL(fileURLWithPath: args[1])
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
let ctx = CIContext()

for (fi, path) in args.dropFirst(2).enumerated() {
    let url = URL(fileURLWithPath: path)
    guard let img = orientedImage(url) else {
        print(#"{"image":\#(String(reflecting: path)),"error":"unreadable"}"#); continue
    }
    let W = CGFloat(img.width), H = CGFloat(img.height)
    let rects = VNDetectFaceRectanglesRequest()
    let lms = VNDetectFaceLandmarksRequest()
    let qual = VNDetectFaceCaptureQualityRequest()
    let h = VNImageRequestHandler(cgImage: img)
    try? h.perform([rects])
    let faces = rects.results ?? []
    lms.inputFaceObservations = faces; qual.inputFaceObservations = faces
    try? h.perform([lms, qual])
    let qualities = qual.results ?? []
    var out: [[String: Any]] = []
    for (i, f) in faces.enumerated() {
        // Vision boxes are normalized with origin at bottom-left
        let b = f.boundingBox
        let box = CGRect(x: b.minX * W, y: (1 - b.maxY) * H, width: b.width * W, height: b.height * H)
        let side = max(box.width, box.height) * 1.8          // include hair, chin and a little shoulder
        var crop = CGRect(x: box.midX - side / 2, y: box.midY - side / 2 - box.height * 0.1, width: side, height: side)
        crop = crop.intersection(CGRect(x: 0, y: 0, width: W, height: H)).integral
        guard let c = img.cropping(to: crop) else { continue }
        // scale to 512x512
        let ci = CIImage(cgImage: c).transformed(by: CGAffineTransform(scaleX: 512 / CGFloat(c.width), y: 512 / CGFloat(c.height)))
        guard let small = ctx.createCGImage(ci, from: CGRect(x: 0, y: 0, width: 512, height: 512)) else { continue }
        let name = String(format: "img%04d-face%02d.jpg", fi, i)
        writeJPEG(small, outDir.appendingPathComponent(name))
        let q = (qualities.first { $0.uuid == f.uuid } ?? (i < qualities.count ? qualities[i] : nil))?.faceCaptureQuality
        let fp = featurePrint(small) ?? []
        out.append(["crop": name, "box": [box.minX, box.minY, box.width, box.height].map { Double($0) },
                    "face_px": Int(min(box.width, box.height)),
                    "roll": Double(truncating: (f.roll ?? 0)), "yaw": Double(truncating: (f.yaw ?? 0)),
                    "quality": q.map { Double($0) } ?? -1, "feature": fp.map { Double($0) }])
    }
    let rec: [String: Any] = ["image": path, "width": img.width, "height": img.height, "faces": out]
    let data = try! JSONSerialization.data(withJSONObject: rec)
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)
}
