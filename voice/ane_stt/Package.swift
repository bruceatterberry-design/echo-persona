// swift-tools-version:5.10
// Echo's speech recognition on the Apple Neural Engine (FluidAudio + Parakeet TDT Core ML). 2026-11-02, proof stage.
import PackageDescription

let package = Package(
    name: "ane-stt",
    platforms: [.macOS(.v14)],
    dependencies: [.package(url: "https://github.com/FluidInference/FluidAudio.git", from: "0.7.0")],
    targets: [.executableTarget(name: "ane-stt", dependencies: [.product(name: "FluidAudio", package: "FluidAudio")])]
)
