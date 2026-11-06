// Jarvis's eyes, used only when you ask ("read my screen", "what's on my screen?").
//
//   JarvisScreen ocr <window|screen>             -> {"app": "...", "lines": ["…", …]}
//   JarvisScreen capture <path> <window|screen>  -> {"app": "...", "path": "<path>"}  (a PNG for the vision model)
//   JarvisScreen allowed                         -> {"allowed": true|false}  (Screen Recording permission)
//
// "window" is the window you're using (the frontmost app's front window); "screen" is the whole
// main display. Text is read with Apple's own Vision framework, on this Mac. The `ocr` capture is
// deleted before this program ends; `capture` writes only where Jarvis asks, and Jarvis deletes it
// as soon as it has been looked at. Nothing is kept and nothing leaves the Mac.

import AppKit
import CoreGraphics
import Foundation
import Vision

func emit(_ obj: Any) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { print(s) }
}

func fail(_ code: String) -> Never {
    emit(["error": code])
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage") }

/// The window you're using: the frontmost app's frontmost normal window (Jarvis's own HUD excluded).
func frontWindow() -> (id: CGWindowID, app: String)? {
    guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] ?? []
    for w in list {
        guard let pid = w[kCGWindowOwnerPID as String] as? pid_t, pid == app.processIdentifier,
              (w[kCGWindowLayer as String] as? Int) == 0,
              let num = w[kCGWindowNumber as String] as? Int else { continue }
        if let b = w[kCGWindowBounds as String] as? [String: CGFloat], (b["Width"] ?? 0) < 120 || (b["Height"] ?? 0) < 80 {
            continue
        }
        return (CGWindowID(num), app.localizedName ?? "the app")
    }
    return nil
}

func capture(to path: String, area: String) -> String {
    var cmd = ["-x", "-o"]                         // no sound, no window shadow
    var appName = "the screen"
    if area == "window", let w = frontWindow() {
        cmd += ["-l", String(w.id)]
        appName = w.app
    } else {
        cmd += ["-m"]                              // main display only
    }
    cmd.append(path)
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    p.arguments = cmd
    p.standardError = FileHandle.nullDevice
    try? p.run()
    p.waitUntilExit()
    guard p.terminationStatus == 0, FileManager.default.fileExists(atPath: path) else { fail("capture-failed") }
    return appName
}

func readText(_ path: String) -> [String] {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return [] }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    let obs = (req.results ?? []).filter { ($0.topCandidates(1).first?.confidence ?? 0) > 0.3 }
    // Reading order: top to bottom, then left to right on the same line.
    let sorted = obs.sorted { a, b in
        let ay = 1 - a.boundingBox.midY, by = 1 - b.boundingBox.midY
        if abs(ay - by) > 0.012 { return ay < by }
        return a.boundingBox.minX < b.boundingBox.minX
    }
    return sorted.compactMap { $0.topCandidates(1).first?.string }
}

switch args[1] {
case "allowed":
    emit(["allowed": CGPreflightScreenCaptureAccess()])

case "ocr":
    let area = args.count > 2 ? args[2] : "window"
    guard CGPreflightScreenCaptureAccess() else { _ = CGRequestScreenCaptureAccess(); fail("no-permission") }
    let tmp = NSTemporaryDirectory() + "jarvis-screen-\(UUID().uuidString).png"
    defer { try? FileManager.default.removeItem(atPath: tmp) }
    let app = capture(to: tmp, area: area)
    let lines = readText(tmp)
    try? FileManager.default.removeItem(atPath: tmp)
    emit(["app": app, "lines": lines])

case "ocr-file":     // for tests: read the text in an existing picture
    guard args.count >= 3 else { fail("usage") }
    emit(["app": "file", "lines": readText(args[2])])

case "capture":
    guard args.count >= 3 else { fail("usage") }
    guard CGPreflightScreenCaptureAccess() else { _ = CGRequestScreenCaptureAccess(); fail("no-permission") }
    let app = capture(to: args[2], area: args.count > 3 ? args[3] : "window")
    emit(["app": app, "path": args[2]])

default:
    fail("usage")
}
