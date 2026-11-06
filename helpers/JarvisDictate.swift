// fn dictation for Jarvis: the fn key, the text box you're in, pasting, and the little dictation UI.
//
// Jarvis (Python) starts this once and keeps it running. It writes JSON lines to stdout:
//   {"fn":"down"}       fn pressed (recording starts straight away, so no word is lost)
//   {"fn":"up"}         fn let go after holding it: finish and type the text
//   {"fn":"tap"}        fn only tapped: forget the recording
//   {"fn":"handsfree"}  second tap within 0.45 s: keep recording until fn is pressed again
//   {"fn":"stop"}       fn pressed during hands-free: finish and type the text
//   {"fn":"cancel"}     another key pressed with fn (fn+F5, fn+arrow…), or Esc in hands-free: forget it
//   {"error":"no-input-monitoring"} / {"ready":true}
// and reads JSON commands on stdin:
//   {"cmd":"pill","state":"listening|handsfree|working|done|nothing|hidden","text":"…"}
//   {"level":0.4}
//   {"cmd":"target"}             -> {"target":{"editable":bool,"secure":bool,"app":"Notes"}}
//   {"cmd":"paste","text":"…"}   -> {"pasted":true}   (your clipboard is put back afterwards)
//   {"cmd":"card","text":"…"}    -> {"card":true}     (copied, and shown with a Copy button)
//   {"cmd":"status"}             -> {"status":{"tap":bool,"accessibility":bool,"globe":int}}
//
// Privacy: it never reads what is in a text box (only what kind of box it is), never records which
// other keys you press (only that one was pressed while fn was held), and never presses Return.

import AppKit
import ApplicationServices
import Foundation
import SwiftUI

// MARK: - Output

let outLock = NSLock()
func emit(_ o: [String: Any]) {
    guard let d = try? JSONSerialization.data(withJSONObject: o), let s = String(data: d, encoding: .utf8) else { return }
    outLock.lock(); print(s); fflush(stdout); outLock.unlock()
}

// MARK: - The fn key

final class FnWatcher {
    var tap: CFMachPort?
    var fnHeld = false
    var downAt = Date.distantPast
    var otherKey = false
    var lastTap = Date.distantPast
    var handsFree = false
    var swallowRelease = false
    var onEsc: (() -> Void)?

    func start() -> Bool {
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
        let me = Unmanaged.passUnretained(self).toOpaque()
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                                        eventsOfInterest: CGEventMask(mask),
                                        callback: { _, type, event, info in
                                            let w = Unmanaged<FnWatcher>.fromOpaque(info!).takeUnretainedValue()
                                            w.handle(type, event)
                                            return Unmanaged.passUnretained(event)
                                        }, userInfo: me) else { return false }
        tap = t
        let src = CFMachPortCreateRunLoopSource(nil, t, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        return true
    }

    func handle(_ type: CGEventType, _ event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let t = tap { CGEvent.tapEnable(tap: t, enable: true) }     // macOS switched it off: back on
            return
        }
        if type == .keyDown {
            let code = event.getIntegerValueField(.keyboardEventKeycode)
            if code == 53 { onEsc?() }                                       // Esc
            if fnHeld && !otherKey {
                otherKey = true                                              // fn+F5, fn+arrow…: not dictation
                emit(["fn": "cancel"])
            } else if handsFree && code == 53 {
                handsFree = false
                emit(["fn": "cancel"])
            }
            return
        }
        guard type == .flagsChanged else { return }
        let flags = event.flags
        let fn = flags.contains(.maskSecondaryFn)
        let others = !flags.intersection([.maskCommand, .maskControl, .maskAlternate, .maskShift]).isEmpty
        if fn && others && fnHeld && !otherKey {
            otherKey = true                                                  // fn with ⌘, ⌃, ⌥ or ⇧
            emit(["fn": "cancel"])
        }
        if fn == fnHeld { return }
        fnHeld = fn
        let now = Date()
        if fn {
            if handsFree {                                                   // tap again to finish hands-free
                handsFree = false
                swallowRelease = true
                emit(["fn": "stop"])
                return
            }
            downAt = now
            otherKey = others
            if !others { emit(["fn": "down"]) }
        } else {
            if swallowRelease { swallowRelease = false; return }
            if otherKey { return }
            let held = now.timeIntervalSince(downAt)
            if held >= 0.3 {
                emit(["fn": "up", "held": (held * 100).rounded() / 100])
            } else if now.timeIntervalSince(lastTap) < 0.45 {
                handsFree = true
                lastTap = .distantPast
                emit(["fn": "handsfree"])
            } else {
                lastTap = now
                emit(["fn": "tap"])
            }
        }
    }
}

// MARK: - The text box you're in (its kind only, never its contents)

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

var madeAccessible = Set<pid_t>()

func focusedElement(_ front: NSRunningApplication?) -> AXUIElement? {
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 0.4)
    if let f = attr(system, kAXFocusedUIElementAttribute), CFGetTypeID(f) == AXUIElementGetTypeID() { return (f as! AXUIElement) }
    if let pid = front?.processIdentifier {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.4)
        if let f = attr(app, kAXFocusedUIElementAttribute), CFGetTypeID(f) == AXUIElementGetTypeID() { return (f as! AXUIElement) }
    }
    return nil
}

/// What kind of box this is: "secure", "editable" or "" (not a place to type). Never its contents.
func kind(_ el: AXUIElement) -> String {
    let role = attr(el, kAXRoleAttribute) as? String ?? ""
    let sub = attr(el, kAXSubroleAttribute) as? String ?? ""
    if role == "AXSecureTextField" || sub == "AXSecureTextField" { return "secure" }
    let textRoles: Set<String> = ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"]
    if textRoles.contains(role) { return "editable" }
    var settable: DarwinBoolean = false
    AXUIElementIsAttributeSettable(el, kAXValueAttribute as CFString, &settable)
    let hasCaret = attr(el, kAXSelectedTextRangeAttribute) != nil          // a position, not text
    if settable.boolValue && hasCaret { return "editable" }
    // Web pages and Electron apps (Claude, Slack, WhatsApp, Notion…): a box built from a web page's
    // "contenteditable" says so through these.
    if attr(el, "AXEditableAncestor") != nil || attr(el, "AXHighestEditableAncestor") != nil { return "editable" }
    if (attr(el, "AXEditable") as? Bool) == true { return "editable" }
    return ""
}

func target() -> [String: Any] {
    let front = NSWorkspace.shared.frontmostApplication
    let appName = front?.localizedName ?? ""
    var result: [String: Any] = ["editable": false, "secure": false, "app": appName]
    guard AXIsProcessTrusted() else { result["why"] = "no-accessibility"; return result }
    // Chrome and Electron apps (WhatsApp, Slack, Claude, VS Code…) only describe their text boxes once asked,
    // and take a moment to do it: look, and if there's nothing yet, look again.
    var justAsked = false
    if let pid = front?.processIdentifier, !madeAccessible.contains(pid) {
        madeAccessible.insert(pid)
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        justAsked = true
    }
    var found = ""
    var role = ""
    for attempt in 0..<(justAsked ? 6 : 2) {
        if attempt > 0 { usleep(120_000) }
        guard let el = focusedElement(front) else { continue }
        role = attr(el, kAXRoleAttribute) as? String ?? ""
        found = kind(el)
        if !found.isEmpty { break }
    }
    result["role"] = role
    if found == "secure" { result["secure"] = true }
    if found == "editable" { result["editable"] = true }
    if found.isEmpty { result["why"] = role.isEmpty ? "nothing-focused" : "not-a-text-box" }
    return result
}

// MARK: - Pasting, and putting your clipboard back

let transient = NSPasteboard.PasteboardType("org.nspasteboard.TransientType")
let concealed = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")

func pressCommandV() {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        if let e = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: down) {   // V
            e.flags = .maskCommand
            e.post(tap: .cghidEventTap)
        }
        usleep(12_000)
    }
}

func paste(_ text: String) {
    let pb = NSPasteboard.general
    let saved: [[(NSPasteboard.PasteboardType, Data)]] = (pb.pasteboardItems ?? []).map { item in
        item.types.compactMap { t in item.data(forType: t).map { (t, $0) } }
    }
    pb.clearContents()
    let item = NSPasteboardItem()
    item.setString(text, forType: .string)
    item.setData(Data(), forType: transient)            // clipboard managers: don't keep this
    item.setData(Data(), forType: concealed)
    pb.writeObjects([item])
    let mine = pb.changeCount
    pressCommandV()
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
        guard pb.changeCount == mine else { return }     // you copied something since: leave it
        pb.clearContents()
        let items: [NSPasteboardItem] = saved.map { types in
            let it = NSPasteboardItem()
            for (t, d) in types { it.setData(d, forType: t) }
            return it
        }
        if !items.isEmpty { pb.writeObjects(items) }
    }
}

func copyKeeping(_ text: String) {
    let pb = NSPasteboard.general
    pb.clearContents()
    pb.setString(text, forType: .string)
}

// MARK: - The pill and the card

final class DictState: ObservableObject {
    @Published var pill = "hidden"
    @Published var pillText = ""
    @Published var levels: [Double] = Array(repeating: 0, count: 22)
    @Published var cardText = ""
    @Published var copied = false
}

let cyan = Color(red: 0.30, green: 0.86, blue: 1.00)

struct PillView: View {
    @ObservedObject var m: DictState
    let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion

    var body: some View {
        VStack(spacing: 6) {
            Spacer(minLength: 0)
            if m.pill != "hidden" {
                HStack(spacing: 10) {
                    icon.frame(width: 18, height: 18)
                    content
                }
                .padding(.horizontal, 16)
                .frame(height: 38)
                .background(Capsule().fill(Color.black.opacity(0.82)))
                .overlay(Capsule().strokeBorder(cyan.opacity(0.35), lineWidth: 1))
                .shadow(color: .black.opacity(0.35), radius: 10, y: 3)
                .transition(.opacity.combined(with: .scale(scale: 0.9)))
                if m.pill == "handsfree" {
                    Text("Hands-free · tap fn to finish · Esc to cancel")
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundStyle(.white.opacity(0.85))
                        .padding(.horizontal, 10).padding(.vertical, 3)
                        .background(Capsule().fill(Color.black.opacity(0.6)))
                }
            }
        }
        .frame(width: 360, height: 90)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: m.pill)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }

    var label: String {
        switch m.pill {
        case "listening", "handsfree": return "Jarvis dictation: listening"
        case "working": return "Jarvis dictation: writing"
        case "done": return m.pillText.isEmpty ? "Typed" : m.pillText
        default: return m.pillText
        }
    }

    @ViewBuilder var icon: some View {
        switch m.pill {
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
        case "nothing", "error": Image(systemName: "exclamationmark.circle").foregroundStyle(Color.orange)
        case "working": Image(systemName: "sparkles").foregroundStyle(cyan)
        default: Image(systemName: "mic.fill").foregroundStyle(cyan)
        }
    }

    @ViewBuilder var content: some View {
        switch m.pill {
        case "listening", "handsfree":
            HStack(alignment: .center, spacing: 3) {
                ForEach(0..<m.levels.count, id: \.self) { i in
                    Capsule().fill(cyan.opacity(0.55 + 0.45 * m.levels[i]))
                        .frame(width: 3, height: 4 + 22 * m.levels[i])
                }
            }
            .frame(height: 26)
        case "working":
            TimelineView(.animation(minimumInterval: 0.05, paused: reduceMotion)) { tl in
                let t = tl.date.timeIntervalSinceReferenceDate
                HStack(spacing: 5) {
                    ForEach(0..<3) { i in
                        Circle().fill(cyan)
                            .frame(width: 6, height: 6)
                            .opacity(reduceMotion ? 0.8 : 0.35 + 0.65 * max(0, sin(t * 6 - Double(i) * 0.7)))
                    }
                    Text("Writing").font(.system(size: 13, weight: .medium, design: .rounded)).foregroundStyle(.white)
                }
            }
        default:
            Text(m.pillText.isEmpty ? "Typed" : m.pillText)
                .font(.system(size: 13, weight: .medium, design: .rounded))
                .foregroundStyle(.white)
                .lineLimit(1)
        }
    }
}

struct CardView: View {
    @ObservedObject var m: DictState
    var close: () -> Void
    var copy: () -> Void

    func body(_ t: String) -> some View {
        Text(t)
            .font(.system(size: 14))
            .foregroundStyle(.white)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: "doc.on.clipboard").foregroundStyle(cyan)
                Text(m.copied ? "Copied again" : "No text box selected · copied, press ⌘V anywhere")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white.opacity(0.85))
                Spacer()
                Button(action: close) {
                    Image(systemName: "xmark").font(.system(size: 11, weight: .bold)).foregroundStyle(.white.opacity(0.8))
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Close")
            }
            if m.cardText.count > 520 || m.cardText.filter({ $0 == "\n" }).count > 9 {
                ScrollView { body(m.cardText) }.frame(maxHeight: 220)
            } else {
                body(m.cardText)
            }
            HStack {
                Spacer()
                Button(action: copy) {
                    Label(m.copied ? "Copied" : "Copy", systemImage: m.copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 13, weight: .semibold, design: .rounded))
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(Capsule().fill(cyan.opacity(0.9)))
                        .foregroundStyle(.black)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(16)
        .frame(width: 400)
        .background(RoundedRectangle(cornerRadius: 16).fill(Color.black.opacity(0.88)))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(cyan.opacity(0.35), lineWidth: 1))
    }
}

final class Panel: NSPanel {
    override var canBecomeKey: Bool { false }     // never takes the keyboard from the app you're typing in
    override var canBecomeMain: Bool { false }
}

func makePanel(_ size: NSSize, clicks: Bool) -> Panel {
    let p = Panel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel],
                  backing: .buffered, defer: false)
    p.isOpaque = false
    p.backgroundColor = .clear
    p.hasShadow = false
    p.level = .statusBar
    p.ignoresMouseEvents = !clicks
    p.becomesKeyOnlyIfNeeded = true
    p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    return p
}

func screenWithMouse() -> NSRect {
    let mouse = NSEvent.mouseLocation
    let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
    return screen?.visibleFrame ?? .zero
}

// MARK: - App

final class Helper: NSObject, NSApplicationDelegate {
    let m = DictState()
    let watcher = FnWatcher()
    var pill: Panel!
    var card: Panel!
    var cardTimer: Timer?
    var hideTimer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        pill = makePanel(NSSize(width: 360, height: 90), clicks: false)
        pill.contentView = NSHostingView(rootView: PillView(m: m))
        card = makePanel(NSSize(width: 400, height: 320), clicks: true)
        let host = NSHostingView(rootView: CardView(m: m, close: { [weak self] in self?.closeCard() },
                                                    copy: { [weak self] in self?.copyAgain() }))
        card.contentView = host
        watcher.onEsc = { [weak self] in DispatchQueue.main.async { self?.closeCard() } }
        startTap()
        readInput()
    }

    func startTap() {
        if watcher.start() {
            emit(["ready": true])
            return
        }
        CGRequestListenEventAccess()                   // asks once, and lists Jarvis under Input Monitoring
        emit(["error": "no-input-monitoring"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let s = self, s.watcher.tap == nil else { return }
            if s.watcher.start() { emit(["ready": true]) } else { s.startTapLater() }
        }
    }

    func startTapLater() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let s = self, s.watcher.tap == nil else { return }
            if s.watcher.start() { emit(["ready": true]) } else { s.startTapLater() }
        }
    }

    func readInput() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while let line = readLine(strippingNewline: true) {
                guard let d = line.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
                DispatchQueue.main.async { self?.apply(obj) }
            }
            DispatchQueue.main.async { NSApp.terminate(nil) }     // Jarvis stopped: so do we
        }
    }

    func showPill(_ state: String, _ text: String) {
        hideTimer?.invalidate()
        if state == "hidden" {
            m.pill = "hidden"
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                if self?.m.pill == "hidden" { self?.pill.orderOut(nil) }
            }
            return
        }
        if m.pill == "hidden" {
            let vf = screenWithMouse()
            pill.setFrameOrigin(NSPoint(x: vf.midX - pill.frame.width / 2, y: vf.minY + 18))
            m.levels = Array(repeating: 0, count: m.levels.count)
        }
        m.pill = state
        m.pillText = text
        pill.orderFrontRegardless()
        if ["done", "nothing", "error"].contains(state) {
            hideTimer = Timer.scheduledTimer(withTimeInterval: state == "done" ? 1.0 : 2.2, repeats: false) { [weak self] _ in
                self?.showPill("hidden", "")
            }
        }
    }

    func showCard(_ text: String) {
        m.cardText = text
        m.copied = false
        card.contentView?.layoutSubtreeIfNeeded()
        let h = min(340, max(110, card.contentView?.fittingSize.height ?? 200))
        let vf = screenWithMouse()
        card.setFrame(NSRect(x: vf.midX - 200, y: vf.minY + 70, width: 400, height: h), display: true)
        card.orderFrontRegardless()
        cardTimer?.invalidate()
        cardTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in self?.closeCard() }
    }

    func closeCard() {
        cardTimer?.invalidate()
        card.orderOut(nil)
        m.cardText = ""
    }

    func copyAgain() {
        copyKeeping(m.cardText)
        m.copied = true
    }

    func apply(_ o: [String: Any]) {
        if let l = o["level"] as? Double {
            m.levels.removeFirst()
            m.levels.append(max(0, min(1, l)))
        }
        guard let cmd = o["cmd"] as? String else { return }
        switch cmd {
        case "pill":
            showPill(o["state"] as? String ?? "hidden", o["text"] as? String ?? "")
        case "target":
            emit(["target": target()])
        case "paste":
            paste(o["text"] as? String ?? "")
            emit(["pasted": true])
        case "card":
            let t = o["text"] as? String ?? ""
            copyKeeping(t)
            showCard(t)
            emit(["card": true])
        case "status":
            let globe = UserDefaults(suiteName: "com.apple.HIToolbox")?.object(forKey: "AppleFnUsageType") as? Int ?? -1
            emit(["status": ["tap": watcher.tap != nil, "accessibility": AXIsProcessTrusted(), "globe": globe]])
        default:
            break
        }
    }
}

// Snapshot mode, to check the look without recording the screen:
//   JarvisDictate --snapshot out.png pill <state> [text]  |  --snapshot out.png card <text>
let args = CommandLine.arguments
if args.count >= 4, args[1] == "--snapshot" {
    MainActor.assumeIsolated {
        let m = DictState()
        let view: AnyView
        if args[3] == "card" {
            m.cardText = args.count > 4 ? args[4].replacingOccurrences(of: "\\n", with: "\n") : ""
            view = AnyView(CardView(m: m, close: {}, copy: {}))
        } else {
            m.pill = args.count > 4 ? args[4] : "listening"
            m.pillText = args.count > 5 ? args[5] : ""
            m.levels = (0..<22).map { i in 0.25 + 0.7 * abs(sin(Double(i) * 0.7)) }
            view = AnyView(PillView(m: m))
        }
        let r = ImageRenderer(content: view.padding(20).background(Color(white: 0.75)))
        r.scale = 2
        if let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: args[2]))
            print("wrote \(args[2])")
        }
    }
    exit(0)
}

// Test mode: feeds made-up key events to the fn logic (no real keyboard) and prints what it decided.
//   JarvisDictate --test-fn
if args.count >= 2, args[1] == "--test-fn" {
    let w = FnWatcher()
    func flags(_ fn: Bool, _ extra: CGEventFlags = []) {
        guard let e = CGEvent(source: nil) else { return }
        e.type = .flagsChanged
        e.flags = fn ? extra.union(.maskSecondaryFn) : extra
        w.handle(.flagsChanged, e)
    }
    func key(_ code: CGKeyCode) {
        guard let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true) else { return }
        w.handle(.keyDown, e)
    }
    func wait(_ s: Double) { usleep(useconds_t(s * 1_000_000)) }
    func step(_ name: String, _ body: () -> Void) { emit(["case": name]); body(); wait(0.6) }
    step("hold") { flags(true); wait(0.5); flags(false) }
    step("tap") { flags(true); wait(0.08); flags(false) }
    step("double tap, then tap to finish") {
        flags(true); wait(0.08); flags(false); wait(0.15); flags(true); wait(0.08); flags(false)
        wait(0.8); flags(true); wait(0.08); flags(false)
    }
    step("fn+F5") { flags(true); wait(0.1); key(96); wait(0.3); flags(false) }
    step("fn+shift") { flags(true); wait(0.1); flags(true, .maskShift); wait(0.3); flags(false) }
    step("hands-free, Esc") {
        flags(true); wait(0.08); flags(false); wait(0.15); flags(true); wait(0.08); flags(false)
        wait(0.3); key(53)
    }
    step("two slow taps") { flags(true); wait(0.08); flags(false); wait(0.7); flags(true); wait(0.08); flags(false) }
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let helper = Helper()
app.delegate = helper
app.run()
