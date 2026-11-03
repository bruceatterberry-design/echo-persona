// Echo's speech recognition on the Apple Neural Engine: FluidAudio + Parakeet TDT 0.6B v2 (English) as Core ML.
// Proof stage (2026-11-02): reads paths of raw 16 kHz mono float32 files on stdin, one per line, and prints one JSON
// line per file: {"path", "text", "ms"}. Loading messages go to stderr. The encoder runs on CPU + Neural Engine
// (FluidAudio's default for this model), never the GPU.
import Foundation
import FluidAudio

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

let t0 = Date()
let models = try await AsrModels.downloadAndLoad(version: .v2)
let asr = AsrManager(config: .default)
try await asr.loadModels(models)
log("ane-stt: model loaded in \(String(format: "%.1f", Date().timeIntervalSince(t0))) s")

while let line = readLine() {
    let path = line.trimmingCharacters(in: .whitespacesAndNewlines)
    if path.isEmpty { continue }
    do {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let samples: [Float] = data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
        let t = Date()
        var state = TdtDecoderState.make()   // fresh per utterance
        let r = try await asr.transcribe(samples, decoderState: &state)
        let obj: [String: Any] = ["path": path, "text": r.text, "ms": Date().timeIntervalSince(t) * 1000]
        print(String(data: try JSONSerialization.data(withJSONObject: obj), encoding: .utf8)!)
    } catch {
        let obj: [String: Any] = ["path": path, "error": "\(error)"]
        print(String(data: try JSONSerialization.data(withJSONObject: obj), encoding: .utf8)!)
    }
    fflush(stdout)
}
