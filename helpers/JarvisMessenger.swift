// Jarvis's messaging helper.
//
//   JarvisMessenger contacts <words…>          -> JSON list of matching contacts with phone numbers
//   JarvisMessenger locked                     -> {"locked": true|false}   (is the screen locked?)
//   JarvisMessenger trusted [--prompt]         -> {"trusted": true|false}  (Accessibility allowed?)
//   JarvisMessenger wa-send <digits> <text>    -> {"sent": true} or {"error": "..."}
//   JarvisMessenger wa-send-chat <name> <text> -> {"sent": true} or {"error": "..."}  (groups; people with no number)
//   JarvisMessenger wa-layout                  -> the window's shape: roles and sizes only, never any text
//
// Privacy: this helper never prints a message, a chat name or anything else it sees in
// WhatsApp. wa-send only compares the message box with the text Jarvis was told to send and
// reports whether it went. Contacts are read only when Jarvis is asked to message someone.
//
// wa-send, step by step:
//   1. Opens WhatsApp's own link whatsapp://send?phone=<digits>&text=<text>. WhatsApp opens the
//      chat for exactly that number with the text in its message box.
//   2. Waits (up to 8 s) until a text box in WhatsApp holds exactly that text. If it never does
//      (the chat had another draft, the number isn't on WhatsApp, WhatsApp changed), it stops
//      without pressing anything.
//   3. Presses Return, then checks the box emptied, which means WhatsApp sent it.
//   4. Puts back whichever app you were using.

import AppKit
import ApplicationServices
import Contacts
import Foundation

func emit(_ obj: Any) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        print(s)
    }
}

func fail(_ code: String) -> Never {
    emit(["error": code])
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage") }

// MARK: - Contacts

func contacts(_ words: [String]) {
    let store = CNContactStore()
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    store.requestAccess(for: .contacts) { ok, _ in granted = ok; sem.signal() }
    _ = sem.wait(timeout: .now() + 60)
    guard granted else { fail("no-permission") }

    let keys: [CNKeyDescriptor] = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactMiddleNameKey,
                                   CNContactNicknameKey, CNContactOrganizationNameKey,
                                   CNContactPhoneNumbersKey] as [CNKeyDescriptor]
    let query = words.map { $0.lowercased() }.filter { !$0.isEmpty }
    var out: [[String: Any]] = []
    let req = CNContactFetchRequest(keysToFetch: keys)
    try? store.enumerateContacts(with: req) { c, stop in
        let full = [c.givenName, c.middleName, c.familyName].filter { !$0.isEmpty }.joined(separator: " ")
        let display = full.isEmpty ? (c.nickname.isEmpty ? c.organizationName : c.nickname) : full
        let tokens = (display + " " + c.nickname).lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }
        // Every word you said must start one of the contact's name words ("mum" -> "Mummy").
        let hit = !query.isEmpty && query.allSatisfy { q in tokens.contains { $0.hasPrefix(q) } }
        if hit && !c.phoneNumbers.isEmpty {
            out.append(["name": display, "nickname": c.nickname,
                        "phones": c.phoneNumbers.map { p -> [String: String] in
                            ["label": CNLabeledValue<NSString>.localizedString(forLabel: p.label ?? ""),
                             "number": p.value.stringValue]
                        }])
        }
        if out.count >= 25 { stop.pointee = true }
    }
    emit(out)
}

// MARK: - Screen and permission

func screenLocked() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (d["CGSSessionScreenIsLocked"] as? Bool) ?? ((d["CGSSessionScreenIsLocked"] as? Int) == 1)
}

// MARK: - Accessibility helpers

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func children(_ el: AXUIElement) -> [AXUIElement] {
    (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

func normalised(_ s: String) -> String {
    s.replacingOccurrences(of: "\u{00A0}", with: " ")
        .replacingOccurrences(of: "\u{2019}", with: "'")
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

/// Finds a text box in the app whose text is exactly `expected`. Looks only at text boxes,
/// and only compares; nothing it reads leaves this function.
func findBox(in app: AXUIElement, holding expected: String) -> AXUIElement? {
    let want = normalised(expected)
    if let focused = attr(app, kAXFocusedUIElementAttribute), CFGetTypeID(focused) == AXUIElementGetTypeID() {
        let f = focused as! AXUIElement
        if let v = attr(f, kAXValueAttribute) as? String, normalised(v) == want { return f }
    }
    var queue: [AXUIElement] = (attr(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    var seen = 0
    while !queue.isEmpty && seen < 4000 {
        let el = queue.removeFirst()
        seen += 1
        let role = attr(el, kAXRoleAttribute) as? String ?? ""
        if role == kAXTextAreaRole || role == kAXTextFieldRole {
            if let v = attr(el, kAXValueAttribute) as? String, normalised(v) == want { return el }
            continue
        }
        queue.append(contentsOf: children(el))
    }
    return nil
}

func pressReturn(pid: pid_t) {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        if let e = CGEvent(keyboardEventSource: src, virtualKey: 36, keyDown: down) {
            e.postToPid(pid)
        }
        usleep(30_000)
    }
}

func waSend(digits: String, text: String) {
    guard digits.count >= 8, digits.allSatisfy(\.isNumber) else { fail("bad-number") }
    guard !text.isEmpty, text.count <= 2000 else { fail("bad-text") }
    guard !screenLocked() else { fail("locked") }
    guard AXIsProcessTrusted() else { fail("no-accessibility") }

    let previous = NSWorkspace.shared.frontmostApplication
    var comps = URLComponents()
    comps.scheme = "whatsapp"
    comps.host = "send"
    comps.queryItems = [URLQueryItem(name: "phone", value: digits), URLQueryItem(name: "text", value: text)]
    guard let url = comps.url else { fail("bad-link") }
    // Encode "+" explicitly so it isn't read as a space.
    let fixed = URL(string: url.absoluteString.replacingOccurrences(of: "+", with: "%2B")) ?? url
    guard NSWorkspace.shared.open(fixed) else { fail("whatsapp-missing") }

    // Wait for WhatsApp, then for the message box to hold exactly our text.
    var wa: NSRunningApplication?
    var box: AXUIElement?
    let deadline = Date().addingTimeInterval(8)
    while Date() < deadline {
        wa = NSRunningApplication.runningApplications(withBundleIdentifier: "net.whatsapp.WhatsApp").first
        if let wa = wa {
            box = findBox(in: AXUIElementCreateApplication(wa.processIdentifier), holding: text)
            if box != nil { break }
        }
        usleep(250_000)
    }
    guard let app = wa else { fail("whatsapp-missing") }
    guard let composer = box else { fail("text-not-in-box") }   // nothing pressed

    // Make sure the keypress lands in that box, then send.
    app.activate()
    AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(250_000)
    pressReturn(pid: app.processIdentifier)

    // Sent means the box is empty again.
    var sent = false
    let until = Date().addingTimeInterval(3)
    while Date() < until {
        usleep(200_000)
        let v = attr(composer, kAXValueAttribute) as? String ?? ""
        if normalised(v).isEmpty || normalised(v) != normalised(text) { sent = normalised(v).isEmpty; break }
    }
    if let prev = previous, prev.bundleIdentifier != "net.whatsapp.WhatsApp" {
        usleep(400_000)
        prev.activate()
    }
    if sent { emit(["sent": true]) } else { fail("not-confirmed") }
}

// MARK: - Sending to a chat by name (groups, and people WhatsApp shows without a number)
//
// WhatsApp has no link that opens a group, so this goes through WhatsApp's own search, with a
// check at every step. It stops, without sending, at the first thing that doesn't match:
//   1. WhatsApp must be in front, the search field must be found.
//   2. Exactly the chat's name is typed; a result whose name is exactly that must appear.
//   3. After opening it, the chat's title at the top must show that same name.
//   4. The message box must be empty (no old draft), then hold exactly the approved words.
//   5. Return is pressed; the box must empty, which means WhatsApp sent it.
// Names and the message box are compared here and never printed.

func role(_ el: AXUIElement) -> String { attr(el, kAXRoleAttribute) as? String ?? "" }
func subrole(_ el: AXUIElement) -> String { attr(el, kAXSubroleAttribute) as? String ?? "" }

func label(_ el: AXUIElement) -> String {
    for a in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
        if let s = attr(el, a) as? String, !s.isEmpty { return s }
    }
    return ""
}

func frame(_ el: AXUIElement) -> CGRect {
    var p = CGPoint.zero, z = CGSize.zero
    if let v = attr(el, kAXPositionAttribute), CFGetTypeID(v) == AXValueGetTypeID() { AXValueGetValue(v as! AXValue, .cgPoint, &p) }
    if let v = attr(el, kAXSizeAttribute), CFGetTypeID(v) == AXValueGetTypeID() { AXValueGetValue(v as! AXValue, .cgSize, &z) }
    return CGRect(origin: p, size: z)
}

func walk(_ root: AXUIElement, limit: Int = 6000, _ visit: (AXUIElement) -> Bool) {
    var queue = [root]
    var seen = 0
    while !queue.isEmpty && seen < limit {
        let el = queue.removeFirst()
        seen += 1
        if visit(el) { return }
        queue.append(contentsOf: children(el))
    }
}

func sameName(_ a: String, _ b: String) -> Bool {
    let n = { (s: String) in normalised(s).lowercased().folding(options: [.diacriticInsensitive], locale: nil) }
    return !a.isEmpty && n(a) == n(b)
}

func frontIsWhatsApp() -> Bool {
    NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "net.whatsapp.WhatsApp"
}

func key(_ code: CGKeyCode, flags: CGEventFlags = []) {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        if let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down) {
            e.flags = flags
            e.post(tap: .cghidEventTap)
        }
        usleep(25_000)
    }
}

func typeText(_ text: String) {
    let src = CGEventSource(stateID: .hidSystemState)
    let units = Array(text.utf16)
    var i = 0
    while i < units.count {
        let chunk = Array(units[i..<min(i + 16, units.count)])
        for down in [true, false] {
            if let e = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: down) {
                e.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: chunk)
                e.post(tap: .cghidEventTap)
            }
        }
        usleep(18_000)
        i += 16
    }
}

func waWindow(_ pid: pid_t) -> AXUIElement? {
    let app = AXUIElementCreateApplication(pid)
    if let w = attr(app, kAXMainWindowAttribute), CFGetTypeID(w) == AXUIElementGetTypeID() { return (w as! AXUIElement) }
    return ((attr(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []).first
}

func waSendChat(name: String, text: String) {
    guard !name.isEmpty, name.count <= 120 else { fail("bad-name") }
    guard !text.isEmpty, text.count <= 2000 else { fail("bad-text") }
    guard !screenLocked() else { fail("locked") }
    guard AXIsProcessTrusted() else { fail("no-accessibility") }
    let previous = NSWorkspace.shared.frontmostApplication
    defer {
        if let prev = previous, prev.bundleIdentifier != "net.whatsapp.WhatsApp" { usleep(400_000); prev.activate() }
    }

    // WhatsApp in front, with its window.
    if NSRunningApplication.runningApplications(withBundleIdentifier: "net.whatsapp.WhatsApp").isEmpty {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "net.whatsapp.WhatsApp") else { fail("whatsapp-missing") }
        NSWorkspace.shared.open(url)
    }
    var wa: NSRunningApplication?
    var win: AXUIElement?
    let t0 = Date().addingTimeInterval(10)
    while Date() < t0 {
        wa = NSRunningApplication.runningApplications(withBundleIdentifier: "net.whatsapp.WhatsApp").first
        if let wa = wa { wa.activate(); win = waWindow(wa.processIdentifier) }
        if win != nil && frontIsWhatsApp() { break }
        usleep(250_000)
    }
    guard let window = win, frontIsWhatsApp() else { fail("whatsapp-not-in-front") }
    let wf = frame(window)

    // 1. The search field.
    var search: AXUIElement?
    walk(window) { el in
        if subrole(el) == "AXSearchField" || role(el) == "AXSearchField" { search = el; return true }
        return false
    }
    guard let field = search else { fail("no-search-field") }
    let sidebarRight = frame(field).maxX + 24
    AXUIElementPerformAction(field, kAXPressAction as CFString)
    AXUIElementSetAttributeValue(field, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(350_000)
    guard frontIsWhatsApp() else { fail("whatsapp-not-in-front") }
    key(0, flags: .maskCommand)      // select anything already in the search box…
    key(51)                          // …and clear it
    typeText(name)

    // 2. A result with exactly that name, in the list on the left.
    var hit: AXUIElement?
    let t1 = Date().addingTimeInterval(4)
    while Date() < t1 && hit == nil {
        usleep(300_000)
        var found: [AXUIElement] = []
        walk(window) { el in
            if role(el) == kAXButtonRole, frame(el).midX < sidebarRight, frame(el).minY > frame(field).maxY - 2,
               sameName(label(el), name) { found.append(el) }
            return false
        }
        hit = found.min { frame($0).minY < frame($1).minY }   // the top one
    }
    guard let row = hit else { fail("chat-not-found") }
    AXUIElementPerformAction(row, kAXPressAction as CFString)

    // 3. The opened chat's title must be that name.
    var verified = false
    let t2 = Date().addingTimeInterval(4)
    while Date() < t2 && !verified {
        usleep(300_000)
        walk(window) { el in
            let f = frame(el)
            if f.minX >= sidebarRight - 2, f.minY < wf.minY + 110, sameName(label(el), name) { verified = true; return true }
            return false
        }
    }
    guard verified else { fail("chat-not-verified") }

    // 4. The message box: right of the list, nearest the bottom, and empty.
    var box: AXUIElement?
    walk(window) { el in
        let r = role(el)
        if (r == kAXTextAreaRole || r == kAXTextFieldRole) && subrole(el) != "AXSearchField" && frame(el).minX >= sidebarRight - 2 {
            if box == nil || frame(el).minY > frame(box!).minY { box = el }
        }
        return false
    }
    guard let composer = box else { fail("no-message-box") }
    let before = attr(composer, kAXValueAttribute) as? String ?? ""
    guard normalised(before).isEmpty else { fail("draft-present") }
    AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(250_000)
    guard frontIsWhatsApp() else { fail("whatsapp-not-in-front") }
    typeText(text.replacingOccurrences(of: "\n", with: " "))
    usleep(400_000)
    let typed = attr(composer, kAXValueAttribute) as? String ?? ""
    guard normalised(typed) == normalised(text.replacingOccurrences(of: "\n", with: " ")) else {
        // Take back what was typed so nothing half-written is left behind, and stop.
        if frontIsWhatsApp() { key(0, flags: .maskCommand); key(51) }
        fail("text-not-in-box")
    }

    // 5. Send, and confirm it left the box.
    guard frontIsWhatsApp() else { fail("whatsapp-not-in-front") }
    key(36)
    var sent = false
    let t3 = Date().addingTimeInterval(3)
    while Date() < t3 {
        usleep(200_000)
        if normalised(attr(composer, kAXValueAttribute) as? String ?? "").isEmpty { sent = true; break }
    }
    if sent { emit(["sent": true]) } else { fail("not-confirmed") }
}

/// The shape of WhatsApp's window, for diagnosing a change: roles, subroles and sizes only.
/// It never prints a title, a name or any text. Only run if you choose to.
func waLayout() {
    guard AXIsProcessTrusted() else { fail("no-accessibility") }
    guard let wa = NSRunningApplication.runningApplications(withBundleIdentifier: "net.whatsapp.WhatsApp").first,
          let window = waWindow(wa.processIdentifier) else { fail("whatsapp-missing") }
    var lines: [String] = []
    func visit(_ el: AXUIElement, _ depth: Int) {
        guard depth < 14, lines.count < 400 else { return }
        let f = frame(el)
        lines.append(String(repeating: "  ", count: depth) + "\(role(el)) \(subrole(el)) \(Int(f.minX)),\(Int(f.minY)) \(Int(f.width))x\(Int(f.height))")
        for c in children(el) { visit(c, depth + 1) }
    }
    visit(window, 0)
    print(lines.joined(separator: "\n"))
}

switch args[1] {
case "contacts":
    contacts(Array(args.dropFirst(2)))
case "locked":
    emit(["locked": screenLocked()])
case "trusted":
    if args.count > 2 && args[2] == "--prompt" {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        emit(["trusted": AXIsProcessTrustedWithOptions(opts)])
    } else {
        emit(["trusted": AXIsProcessTrusted()])
    }
case "wa-send":
    guard args.count == 4 else { fail("usage") }
    waSend(digits: args[2], text: args[3])
case "wa-send-chat":
    guard args.count == 4 else { fail("usage") }
    waSendChat(name: args[2], text: args[3])
case "wa-layout":
    waLayout()
default:
    fail("usage")
}
