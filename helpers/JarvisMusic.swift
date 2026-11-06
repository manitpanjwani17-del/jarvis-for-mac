// Starts a song from the Apple Music catalogue (not just your library) in the Music app.
//
//   JarvisMusic play-catalog <music.apple.com link> <song title>  -> {"played": true, "how": "..."} or {"error": "..."}
//
// Apple's scripting can only start songs that are in your library. For anything else, this opens
// the song's page in Music, finds the row with that song's title, and presses it, trying a few
// ways (its play button, the row itself, select + Return). After each try it asks Music what is
// actually playing, and stops as soon as the right song is on. If nothing works, it says so.
// It only looks at the Music app's window, and prints only how it succeeded or an error code.

import AppKit
import ApplicationServices
import Foundation

func emit(_ o: Any) {
    if let d = try? JSONSerialization.data(withJSONObject: o), let s = String(data: d, encoding: .utf8) { print(s) }
}
func fail(_ c: String) -> Never { emit(["error": c]); exit(1) }

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}
func kids(_ el: AXUIElement) -> [AXUIElement] { (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] }
func role(_ el: AXUIElement) -> String { attr(el, kAXRoleAttribute) as? String ?? "" }
func label(_ el: AXUIElement) -> String {
    for a in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
        if let s = attr(el, a) as? String, !s.isEmpty { return s }
    }
    return ""
}
func parent(_ el: AXUIElement) -> AXUIElement? {
    guard let p = attr(el, kAXParentAttribute), CFGetTypeID(p) == AXUIElementGetTypeID() else { return nil }
    return (p as! AXUIElement)
}

func nowPlaying() -> String {
    let src = "tell application \"Music\" to if player state is playing then return name of current track"
    var err: NSDictionary?
    return NSAppleScript(source: src)?.executeAndReturnError(&err).stringValue ?? ""
}

func same(_ a: String, _ b: String) -> Bool {
    let n = { (s: String) in s.lowercased().folding(options: .diacriticInsensitive, locale: nil)
        .components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }.joined(separator: " ") }
    let x = n(a), y = n(b)
    return !x.isEmpty && !y.isEmpty && (x.hasPrefix(y) || y.hasPrefix(x))
}

func key(_ code: CGKeyCode) {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)?.post(tap: .cghidEventTap)
        usleep(30_000)
    }
}

let args = CommandLine.arguments
guard args.count == 4, args[1] == "play-catalog" else { fail("usage") }
let link = args[2], title = args[3]
guard link.hasPrefix("https://music.apple.com/") else { fail("bad-link") }
guard AXIsProcessTrusted() else { fail("no-accessibility") }
if same(nowPlaying(), title) { emit(["played": true, "how": "already"]); exit(0) }

let previous = NSWorkspace.shared.frontmostApplication
defer { if let p = previous, p.bundleIdentifier != "com.apple.Music" { usleep(300_000); p.activate() } }

guard let url = URL(string: link.replacingOccurrences(of: "https://", with: "music://")) else { fail("bad-link") }
NSWorkspace.shared.open(url)

// Wait for Music and for a row showing the song's title.
var app: NSRunningApplication?
var rows: [AXUIElement] = []
let until = Date().addingTimeInterval(10)
while Date() < until && rows.isEmpty {
    usleep(400_000)
    app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Music").first
    guard let a = app else { continue }
    let root = AXUIElementCreateApplication(a.processIdentifier)
    var queue = (attr(root, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    var seen = 0
    while !queue.isEmpty && seen < 8000 {
        let el = queue.removeFirst(); seen += 1
        if role(el) == "AXStaticText" || role(el) == "AXCell" || role(el) == "AXButton", same(label(el), title) {
            rows.append(el)
        }
        queue.append(contentsOf: kids(el))
    }
}
guard let music = app else { fail("music-missing") }
guard !rows.isEmpty else { fail("song-not-shown") }
let debug = ProcessInfo.processInfo.environment["JARVIS_MUSIC_DEBUG"] != nil
var tried: [String] = []

func tryIt(_ how: String, _ action: () -> Void) {
    tried.append(how)
    action()
    for _ in 0..<8 {
        usleep(300_000)
        if same(nowPlaying(), title) { emit(["played": true, "how": how]); exit(0) }
    }
}

func frame(_ el: AXUIElement) -> CGRect {
    var p = CGPoint.zero, z = CGSize.zero
    if let v = attr(el, kAXPositionAttribute), CFGetTypeID(v) == AXValueGetTypeID() { AXValueGetValue(v as! AXValue, .cgPoint, &p) }
    if let v = attr(el, kAXSizeAttribute), CFGetTypeID(v) == AXValueGetTypeID() { AXValueGetValue(v as! AXValue, .cgSize, &z) }
    return CGRect(origin: p, size: z)
}

func doubleClick(at point: CGPoint) {
    let back = CGEvent(source: nil)?.location ?? point
    for n in [1, 2] {
        for type in [CGEventType.leftMouseDown, .leftMouseUp] {
            if let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: .left) {
                e.setIntegerValueField(.mouseEventClickState, value: Int64(n))
                e.post(tap: .cghidEventTap)
            }
            usleep(20_000)
        }
    }
    usleep(100_000)
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: back, mouseButton: .left)?.post(tap: .cghidEventTap)
}

for text in rows {
    // Up to the table row that holds this title.
    var row: AXUIElement? = text
    for _ in 0..<6 {
        guard let r = row else { break }
        if role(r) == "AXRow" { break }
        row = parent(r)
    }
    guard let r = row, role(r) == "AXRow" else { continue }
    // 1. A play button anywhere in the row.
    var q = [r]
    while !q.isEmpty {
        let el = q.removeFirst()
        if role(el) == "AXButton", label(el).lowercased().contains("play") {
            tryIt("play button") { AXUIElementPerformAction(el, kAXPressAction as CFString) }
        }
        q.append(contentsOf: kids(el))
    }
    // 2. Whatever the row itself offers.
    var names: CFArray?
    AXUIElementCopyActionNames(r, &names)
    for a in (names as? [String]) ?? [] where !["AXShowMenu", "AXScrollToVisible"].contains(a) {
        tryIt("row action") { AXUIElementPerformAction(r, a as CFString) }
    }
    // 3. Select the row in its table and press Return.
    tryIt("select and Return") {
        music.activate()
        usleep(250_000)
        if let table = parent(r) { AXUIElementSetAttributeValue(table, kAXFocusedAttribute as CFString, kCFBooleanTrue) }
        AXUIElementSetAttributeValue(r, kAXSelectedAttribute as CFString, kCFBooleanTrue)
        usleep(200_000)
        key(36)
    }
    // 4. Double-click the row, as you would by hand (the pointer goes back where it was).
    let f = frame(r)
    if f.width > 0 {
        tryIt("double-click") {
            music.activate()
            usleep(300_000)
            doubleClick(at: CGPoint(x: f.minX + min(f.width / 2, 220), y: f.midY))
        }
    }
}
if debug {
    // Shapes only (roles), never text: what the rows that showed the title look like.
    emit(["error": "did-not-start", "rows": rows.count, "tried": tried,
          "row_roles": rows.prefix(6).map { r -> String in
              var chain: [String] = []; var e: AXUIElement? = r
              for _ in 0..<4 { if let x = e { chain.append(role(x)); e = parent(x) } }
              return chain.joined(separator: "<")
          }])
    exit(1)
}
fail("did-not-start")
