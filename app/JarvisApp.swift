// Jarvis for Mac: the app.
//
// It lives in the menu bar and runs Jarvis (Python, inside this app) as its child, so every macOS
// permission belongs to this one app and is asked for, and shown, as "Jarvis".
//
// The first time it opens, a setup window walks through everything, one screen at a time:
// what Jarvis is, downloading the free AI models, each permission (always saying why, before asking),
// the fn key, and how Jarvis should address you. It can be opened again from the menu at any time.
//
// Where things live:
//   Jarvis.app/Contents/Resources/jarvis     the code and its prebuilt helpers (read-only)
//   Jarvis.app/Contents/Resources/bin/uv     installs Jarvis's own Python
//   ~/Library/Application Support/Jarvis for Mac   models, Python, your settings and memory, logs

import AppKit
import AVFoundation
import ApplicationServices
import Contacts
import CoreGraphics
import EventKit
import ServiceManagement
import SwiftUI

// MARK: - Places

enum Place {
    static let resources = Bundle.main.resourceURL!
    static let code = resources.appendingPathComponent("jarvis")
    static let uv = resources.appendingPathComponent("bin/uv")
    static let home: URL = {
        if let h = ProcessInfo.processInfo.environment["JARVIS_HOME"], !h.isEmpty { return URL(fileURLWithPath: h) }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Jarvis for Mac")
    }()
    static let python = home.appendingPathComponent("venv/bin/python")
    static let data = home.appendingPathComponent("data")
    static let logs = home.appendingPathComponent("logs")
    static let setupDone = data.appendingPathComponent("setup.json")
    static var repo: String { Bundle.main.object(forInfoDictionaryKey: "JarvisRepoURL") as? String ?? "" }
    static var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "" }

    static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["JARVIS_HOME"] = home.path
        env["JARVIS_HELPERS"] = code.appendingPathComponent("helpers").path
        env["JARVIS_OVERLAY"] = code.appendingPathComponent("overlay").path
        env["JARVIS_UV"] = uv.path
        env["JARVIS_APP"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env
    }
}

// MARK: - Settings shared with Jarvis (data/settings.json)

enum Prefs {
    static var file: URL { Place.data.appendingPathComponent("settings.json") }
    static func load() -> [String: Any] {
        guard let d = try? Data(contentsOf: file), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
        return o
    }
    static func save(_ changes: [String: Any]) {
        var o = load()
        for (k, v) in changes { o[k] = v }
        try? FileManager.default.createDirectory(at: Place.data, withIntermediateDirectories: true)
        if let d = try? JSONSerialization.data(withJSONObject: o, options: [.prettyPrinted, .sortedKeys]) { try? d.write(to: file) }
    }
}

// MARK: - Permissions

enum Perm: String, CaseIterable, Identifiable {
    case microphone, accessibility, contacts, calendars, reminders, screen, fullDisk
    var id: String { rawValue }

    var title: String {
        switch self {
        case .microphone: return "Microphone"
        case .accessibility: return "Accessibility"
        case .contacts: return "Contacts"
        case .calendars: return "Calendars"
        case .reminders: return "Reminders"
        case .screen: return "Screen Recording"
        case .fullDisk: return "Full Disk Access"
        }
    }
    var symbol: String {
        switch self {
        case .microphone: return "mic.fill"
        case .accessibility: return "accessibility"
        case .contacts: return "person.crop.circle"
        case .calendars: return "calendar"
        case .reminders: return "checklist"
        case .screen: return "rectangle.dashed.badge.record"
        case .fullDisk: return "internaldrive"
        }
    }
    var required: Bool { self == .microphone }
    var why: String {
        switch self {
        case .microphone:
            return "To hear “Hey Jarvis”, what you ask, and what you dictate with the fn key. Sound is processed on this Mac and is never recorded to disk or sent anywhere."
        case .accessibility:
            return "To type your fn dictation into the box you're in, notice the fn key, and press buttons in WhatsApp or Music when you ask. Jarvis never presses Send without your spoken “yes”, and never reads what's in a text box."
        case .contacts:
            return "To find a person's number when you say “message Mum”. Jarvis only looks up the name you said."
        case .calendars:
            return "To answer “what's on tomorrow?” and add events when you ask."
        case .reminders:
            return "To add and read reminders when you ask (“remind me to call the bank at 9”)."
        case .screen:
            return "Only for “read my screen” or “what's on my screen?”. Jarvis looks once, when you ask, and throws the picture away straight after."
        case .fullDisk:
            return "Only to read WhatsApp's message list on this Mac when you ask “any new messages?” or “what did the group say?”. Skip it and everything else still works."
        }
    }

    var granted: Bool {
        if Snap.on { return false }          // pictures for the README show a fresh Mac, not this one
        switch self {
        case .microphone: return AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        case .accessibility: return AXIsProcessTrusted()
        case .contacts: return CNContactStore.authorizationStatus(for: .contacts) == .authorized
        case .calendars: return EKEventStore.authorizationStatus(for: .event) == .fullAccess
        case .reminders: return EKEventStore.authorizationStatus(for: .reminder) == .fullAccess
        case .screen: return CGPreflightScreenCaptureAccess()
        case .fullDisk:
            // A file only Full Disk Access can open. It's opened and closed, never read.
            let probe = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/com.apple.TCC/TCC.db")
            if let h = FileHandle(forReadingAtPath: probe.path) { try? h.close(); return true }
            return false
        }
    }

    var pane: String {
        switch self {
        case .microphone: return "Privacy_Microphone"
        case .accessibility: return "Privacy_Accessibility"
        case .contacts: return "Privacy_Contacts"
        case .calendars: return "Privacy_Calendars"
        case .reminders: return "Privacy_Reminders"
        case .screen: return "Privacy_ScreenCapture"
        case .fullDisk: return "Privacy_AllFiles"
        }
    }

    func openSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") { NSWorkspace.shared.open(url) }
    }

    func request(_ done: @escaping () -> Void) {
        let back = { DispatchQueue.main.async(execute: done) }
        switch self {
        case .microphone:
            if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
                AVCaptureDevice.requestAccess(for: .audio) { _ in back() }
            } else { openSettings(); back() }
        case .accessibility:
            let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            if !AXIsProcessTrustedWithOptions(opts) { openSettings() }
            back()
        case .contacts:
            if CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
                CNContactStore().requestAccess(for: .contacts) { _, _ in back() }
            } else { openSettings(); back() }
        case .calendars:
            if EKEventStore.authorizationStatus(for: .event) == .notDetermined {
                EKEventStore().requestFullAccessToEvents { _, _ in back() }
            } else { openSettings(); back() }
        case .reminders:
            if EKEventStore.authorizationStatus(for: .reminder) == .notDetermined {
                EKEventStore().requestFullAccessToReminders { _, _ in back() }
            } else { openSettings(); back() }
        case .screen:
            if !CGRequestScreenCaptureAccess() { openSettings() }
            back()
        case .fullDisk:
            openSettings(); back()
        }
    }
}

// MARK: - Running the installer (setup/install.sh)

final class Installer: ObservableObject {
    @Published var running = false
    @Published var step = ""
    @Published var progress: Double = 0           // 0…1 for the current part
    @Published var failure = ""
    @Published var log: [String] = []
    private var proc: Process?

    /// Runs one installer step. `finished(true)` when it ends well.
    func run(_ step: String, extra: [String: String] = [:], finished: @escaping (Bool) -> Void) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = [Place.code.appendingPathComponent("setup/install.sh").path, step]
        var env = Place.environment()
        env["JARVIS_SETUP_JSON"] = "1"
        for (k, v) in extra { env[k] = v }
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        var buffer = Data()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = h.availableData
            if chunk.isEmpty { return }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 10) {
                let line = String(decoding: buffer[..<nl], as: UTF8.self)
                buffer.removeSubrange(...nl)
                DispatchQueue.main.async { self?.handle(line) }
            }
        }
        p.terminationHandler = { [weak self] proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                self?.running = false
                finished(proc.terminationStatus == 0)
            }
        }
        failure = ""
        running = true
        progress = 0
        do { try p.run(); proc = p } catch {
            running = false
            failure = "The installer couldn't start."
            finished(false)
        }
    }

    func cancel() { proc?.terminate() }

    private func handle(_ line: String) {
        if line.hasPrefix("@step ") { step = String(line.dropFirst(6)); progress = 0 }
        else if line.hasPrefix("@progress ") { progress = (Double(line.dropFirst(10)) ?? 0) / 100 }
        else if line.hasPrefix("@fail ") { failure = String(line.dropFirst(6)) }
        else if line.hasPrefix("@ok") { progress = 1 }
        else if line.hasPrefix("{"), let d = line.data(using: .utf8),
                let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            if let done = o["all_done"] as? Double, let total = o["all_total"] as? Double, total > 0 {
                progress = done / total
                step = String(format: "Downloading the free AI models: %.1f of %.1f GB", done / 1e9, total / 1e9)
            }
            if let t = o["text"] as? String, (o["event"] as? String) != "progress" { log.append(t) }
        } else if !line.isEmpty {
            log.append(line)
        }
        if log.count > 200 { log.removeFirst(log.count - 200) }
    }
}

// MARK: - Setup window

final class Setup: ObservableObject {
    enum Page: Int, CaseIterable { case welcome, download, permissions, fnKey, you, done }
    @Published var page: Page = .welcome
    @Published var downloadsDone = false
    @Published var needsOllama = false
    @Published var withVision = false
    @Published var refresh = 0                     // re-check permissions
    @Published var address = "Sir"
    @Published var customName = ""
    @Published var countryCode = Setup.guessCountryCode()
    let installer = Installer()
    var onFinish: () -> Void = {}

    static func guessCountryCode() -> String {
        let codes = ["IN": "91", "US": "1", "CA": "1", "GB": "44", "AU": "61", "NZ": "64", "IE": "353", "DE": "49",
                     "FR": "33", "ES": "34", "IT": "39", "NL": "31", "AE": "971", "SA": "966", "SG": "65", "MY": "60",
                     "PK": "92", "BD": "880", "LK": "94", "NP": "977", "ZA": "27", "NG": "234", "KE": "254", "BR": "55",
                     "MX": "52", "JP": "81", "KR": "82", "PH": "63", "ID": "62", "SE": "46", "NO": "47", "DK": "45",
                     "CH": "41", "AT": "43", "BE": "32", "PT": "351", "PL": "48", "TR": "90", "IL": "972", "EG": "20"]
        return codes[Locale.current.region?.identifier ?? ""] ?? ""
    }

    func next() { if let n = Page(rawValue: page.rawValue + 1) { page = n } }
    func back() { if let p = Page(rawValue: page.rawValue - 1) { page = p } }

    /// Python, packages and models, then the brain through Ollama.
    func download() {
        installer.log = []
        installer.run("check") { ok in
            guard ok else { return }
            self.installer.run("python") { ok in
                guard ok else { return }
                self.installer.run("models") { ok in
                    guard ok else { return }
                    self.brain()
                }
            }
        }
    }

    func brain() {
        installer.run("ollama") { ok in
            if !ok { self.needsOllama = true; self.installer.failure = ""; return }
            self.needsOllama = false
            self.installer.run("brain", extra: self.withVision ? ["JARVIS_WITH_VISION": "1"] : [:]) { ok in
                if ok { self.downloadsDone = true }
            }
        }
    }

    func installOllama() {
        installer.run("install-ollama") { ok in if ok { self.brain() } }
    }

    func saveYou() {
        let who = address == "name" ? customName.trimmingCharacters(in: .whitespaces) : address
        var changes: [String: Any] = ["address": who.isEmpty ? "Sir" : who]
        let cc = countryCode.filter(\.isNumber)
        if !cc.isEmpty { changes["country_code"] = cc }
        Prefs.save(changes)
    }

    func finish() {
        saveYou()
        try? FileManager.default.createDirectory(at: Place.data, withIntermediateDirectories: true)
        let mark: [String: Any] = ["complete": true, "version": Place.version, "when": ISO8601DateFormatter().string(from: Date())]
        if let d = try? JSONSerialization.data(withJSONObject: mark) { try? d.write(to: Place.setupDone) }
        onFinish()
    }
}

let accent = Color(red: 0.30, green: 0.86, blue: 1.00)
enum Snap { static var on = false }

struct Bar: View {
    var value: Double
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                Capsule().fill(accent).frame(width: max(8, g.size.width * min(1, max(0, value))))
            }
        }
        .frame(height: 8)
        .accessibilityElement()
        .accessibilityLabel("Progress")
        .accessibilityValue("\(Int(value * 100)) percent")
    }
}

struct SetupView: View {
    @ObservedObject var s: Setup
    @ObservedObject var installer: Installer

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                ForEach(Setup.Page.allCases, id: \.rawValue) { p in
                    Capsule().fill(p.rawValue <= s.page.rawValue ? accent : Color.secondary.opacity(0.25))
                        .frame(height: 4)
                }
            }
            .padding(.horizontal, 28).padding(.top, 18)
            .accessibilityHidden(true)
            Group {
                switch s.page {
                case .welcome: welcome
                case .download: download
                case .permissions: permissions
                case .fnKey: fnKey
                case .you: you
                case .done: done
                }
            }
            .padding(28)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(width: 640, height: 600)
    }

    func header(_ title: String, _ sub: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 26, weight: .bold, design: .rounded))
            Text(sub).font(.system(size: 14)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    func bullet(_ symbol: String, _ text: String, tint: Color = accent) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(tint).frame(width: 18)
            Text(text).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
        }
    }

    func buttons(back: Bool = true, nextTitle: String = "Continue", nextEnabled: Bool = true, next: @escaping () -> Void) -> some View {
        HStack {
            if back { Button("Back") { s.back() }.keyboardShortcut(.cancelAction) }
            Spacer()
            Button(nextTitle, action: next).keyboardShortcut(.defaultAction).disabled(!nextEnabled)
        }
    }

    // 1. What Jarvis is
    var welcome: some View {
        VStack(alignment: .leading, spacing: 16) {
            header("Welcome to Jarvis for Mac", "A free voice assistant that lives on your Mac. Say “Hey Jarvis”, or hold fn to type with your voice.")
            VStack(alignment: .leading, spacing: 9) {
                bullet("waveform", "Talks back in a natural British voice, and can be interrupted.")
                bullet("keyboard", "Hold fn, speak, let go: cleaned-up text is typed wherever your cursor is.")
                bullet("message", "Reads and sends WhatsApp and iMessage, but always reads it back and waits for your “yes”.")
                bullet("music.note", "Plays and controls Apple Music, sets timers, reminders and events, answers questions.")
                bullet("brain", "Remembers what you tell him, and can learn your voice so he only listens to you.")
            }
            Divider()
            VStack(alignment: .leading, spacing: 9) {
                bullet("lock.fill", "Everything runs on this Mac: no account, no cloud, no subscription, no tracking.", tint: .green)
                bullet("hand.raised.fill", "He never deletes anything, never sends without asking, and logs who you messaged, never what.", tint: .green)
                bullet("arrow.down.circle", "Setup downloads about 14 GB of free AI models, once. After that, most of Jarvis works offline.", tint: .orange)
            }
            Spacer()
            buttons(back: false, nextTitle: "Get started") { s.next() }
        }
    }

    // 2. Downloads
    var download: some View {
        VStack(alignment: .leading, spacing: 14) {
            header("Download the free AI models", "These are open models made by others and run entirely on this Mac. Each file is checked against a fingerprint before it's used; a file that doesn't match is deleted.")
            VStack(alignment: .leading, spacing: 7) {
                bullet("ear", "Hearing: OpenAI Whisper (2.2 GB)")
                bullet("speaker.wave.2", "Voice: Kokoro-82M (0.35 GB)")
                bullet("waveform.badge.mic", "Wake word, speech detection, voice recognition: openWakeWord, Silero, NVIDIA TitaNet (0.1 GB)")
                bullet("cpu", "Brain: Qwen3 8B, and Qwen3 4B for dictation, through the free Ollama app (7.7 GB)")
                Toggle("Also download the screen-reading model, Qwen3.5 4B (3.4 GB more)", isOn: $s.withVision)
                    .font(.system(size: 13)).padding(.leading, 28).disabled(installer.running || s.downloadsDone)
            }
            if s.needsOllama && !installer.running {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Jarvis's brain runs in Ollama, a free app from ollama.com.").font(.system(size: 13, weight: .semibold))
                    Text("Jarvis can download it for you from ollama.com (about 230 MB) and installs it only if it is signed by Ollama and accepted by macOS. Or install it yourself from ollama.com, then press Check again.")
                        .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button("Download and install Ollama") { s.installOllama() }
                        Button("Check again") { s.brain() }
                        Button("Open ollama.com") { NSWorkspace.shared.open(URL(string: "https://ollama.com/download")!) }
                    }
                }
                .padding(12).background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.12)))
            }
            if installer.running || s.downloadsDone || !installer.failure.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text(s.downloadsDone ? "Everything is downloaded and checked." : installer.step)
                        .font(.system(size: 13, weight: .medium))
                    if !s.downloadsDone { Bar(value: installer.progress) }
                    if !installer.failure.isEmpty {
                        Text("Stopped: \(installer.failure). Press Download to try again; finished parts are kept.")
                            .font(.system(size: 12)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            Spacer()
            HStack {
                Button("Back") { s.back() }.disabled(installer.running)
                Spacer()
                if !s.downloadsDone {
                    Button(installer.running ? "Downloading…" : "Download") { s.download() }
                        .keyboardShortcut(.defaultAction).disabled(installer.running)
                } else {
                    Button("Continue") { s.next() }.keyboardShortcut(.defaultAction)
                }
            }
        }
    }

    // 3. Permissions, each with its reason first
    var permissions: some View {
        VStack(alignment: .leading, spacing: 12) {
            header("Permissions, and why", "macOS asks you to allow each one. Only the microphone is needed; skip anything else and that one feature stays off. You can change any of these later in System Settings.")
            if Snap.on { permissionList } else { ScrollView { permissionList } }
            HStack {
                Button("Back") { s.back() }
                Button("Check again") { s.refresh += 1 }
                Spacer()
                Button("Continue") { s.next() }.keyboardShortcut(.defaultAction).disabled(!Perm.microphone.granted)
            }
        }
        .onReceive(Timer.publish(every: 2, on: .main, in: .common).autoconnect()) { _ in s.refresh += 1 }
    }

    var permissionList: some View {
                VStack(spacing: 10) {
                    ForEach(Perm.allCases) { p in
                        let ok = p.granted
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: p.symbol).font(.system(size: 18)).foregroundStyle(accent).frame(width: 26)
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(p.title).font(.system(size: 14, weight: .semibold))
                                    Text(p.required ? "Needed" : "Optional").font(.system(size: 10, weight: .semibold))
                                        .padding(.horizontal, 6).padding(.vertical, 2)
                                        .background(Capsule().fill((p.required ? Color.orange : Color.secondary).opacity(0.18)))
                                }
                                Text(p.why).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 8)
                            if ok {
                                Label("Allowed", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.system(size: 12, weight: .medium))
                            } else {
                                Button("Allow…") { p.request { s.refresh += 1 } }
                            }
                        }
                        .padding(12)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Color.secondary.opacity(0.07)))
                        .id("\(p.rawValue)-\(s.refresh)")
                    }
                    Text("Later, the first time you ask Jarvis to use Music, WhatsApp or Messages, macOS will also ask whether Jarvis may control that app. Allow it if you want that feature.")
                        .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
    }

    // 4. The fn key
    var fnKey: some View {
        VStack(alignment: .leading, spacing: 14) {
            header("Type with your voice: the fn key", "Hold fn, speak, let go. Jarvis types it, cleaned up (no “um”s, grammar fixed, lists as points), wherever your cursor is. Tap fn twice for hands-free.")
            VStack(alignment: .leading, spacing: 9) {
                bullet("1.circle", "Open System Settings → Keyboard.")
                bullet("2.circle", "Set “Press 🌐 key to” to “Do Nothing”. Otherwise macOS also opens its emoji picker when you let go of fn.")
                bullet("3.circle", "That's all. Jarvis only notices fn being held; it never records which other keys you press.")
            }
            Button("Open Keyboard settings") {
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Keyboard-Settings.extension")!)
            }
            Spacer()
            buttons { s.next() }
        }
    }

    // 5. How Jarvis should address you
    var you: some View {
        VStack(alignment: .leading, spacing: 16) {
            header("A little about you", "Saved only on this Mac, in Jarvis's settings. Change it any time by saying “call me …” or “my country code is …”.")
            VStack(alignment: .leading, spacing: 8) {
                Text("How should Jarvis address you?").font(.system(size: 14, weight: .semibold))
                Picker("", selection: $s.address) {
                    Text("Sir").tag("Sir")
                    Text("Ma'am").tag("Ma'am")
                    Text("By my name").tag("name")
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 320)
                if s.address == "name" {
                    TextField("Your first name", text: $s.customName).frame(width: 240)
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Your country calling code").font(.system(size: 14, weight: .semibold))
                Text("Added to phone numbers you say without one (for example 44 for the UK, 1 for the US, 91 for India).")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                HStack(spacing: 4) {
                    Text("+")
                    TextField("44", text: $s.countryCode).frame(width: 70)
                }
            }
            Spacer()
            buttons(nextEnabled: s.address != "name" || !s.customName.trimmingCharacters(in: .whitespaces).isEmpty) {
                s.saveYou(); s.next()
            }
        }
    }

    // 6. Ready
    var done: some View {
        VStack(alignment: .leading, spacing: 14) {
            header("Jarvis is ready", "He lives in the menu bar (the ring icon). Everything below can be changed from there.")
            VStack(alignment: .leading, spacing: 9) {
                bullet("quote.bubble", "Say “Hey Jarvis”, then ask: “what's the weather tomorrow?”, “play some music”, “set a timer for ten minutes”.")
                bullet("keyboard", "Hold fn anywhere and speak to type.")
                bullet("brain", "The first time, he'll tell you about his memory and offer to learn your voice (about a minute).")
                bullet("questionmark.circle", "Say “what can you do?” for the full list.")
            }
            Spacer()
            buttons(nextTitle: "Start Jarvis") { s.finish() }
        }
    }
}

final class SetupWindow: NSWindow {
    override var canBecomeKey: Bool { true }
}

// MARK: - Menu bar app

final class Controller: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var item: NSStatusItem!
    var child: Process?
    var statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    var quitting = false
    var restarts: [Date] = []
    var window: NSWindow?
    var setup: Setup?

    var setUp: Bool {
        FileManager.default.fileExists(atPath: Place.setupDone.path) && FileManager.default.isExecutableFile(atPath: Place.python.path)
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let b = item.button {
            b.image = NSImage(systemSymbolName: "circle.circle", accessibilityDescription: "Jarvis")
            b.toolTip = "Jarvis"
        }
        let menu = NSMenu()
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        let login = NSMenuItem(title: "Start at login", action: #selector(toggleLogin(_:)), keyEquivalent: "")
        login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        add(menu, "Setup and permissions…", #selector(openSetup))
        add(menu, "What Jarvis did (log)", #selector(showLog))
        add(menu, "Open Jarvis's folder", #selector(showFolder))
        add(menu, "Restart Jarvis", #selector(restart))
        menu.addItem(.separator())
        add(menu, "About Jarvis for Mac", #selector(about))
        add(menu, "Quit Jarvis", #selector(quit), key: "q")
        item.menu = menu
        if CommandLine.arguments.contains("--selftest") || setUp {
            start()
        } else {
            statusLine.title = "Setting up…"
            openSetup()
        }
    }

    func add(_ menu: NSMenu, _ title: String, _ sel: Selector, key: String = "") {
        let m = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        m.target = self
        menu.addItem(m)
    }

    @objc func openSetup() {
        if let w = window { NSApp.activate(ignoringOtherApps: true); w.makeKeyAndOrderFront(nil); return }
        let s = Setup()
        if setUp { s.downloadsDone = true }
        s.onFinish = { [weak self] in
            self?.window?.close()
            self?.restart()
        }
        setup = s
        let w = SetupWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 600), styleMask: [.titled, .closable, .miniaturizable],
                            backing: .buffered, defer: false)
        w.title = "Jarvis for Mac: Setup"
        w.contentView = NSHostingView(rootView: SetupView(s: s, installer: s.installer))
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        window = w
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ n: Notification) {
        setup?.installer.cancel()
        window = nil
        setup = nil
        NSApp.setActivationPolicy(.accessory)
        if !setUp { statusLine.title = "Setup isn't finished (Setup and permissions…)" }
    }

    func start() {
        guard FileManager.default.isExecutableFile(atPath: Place.python.path) else {
            statusLine.title = "Setup isn't finished (Setup and permissions…)"
            if CommandLine.arguments.contains("--selftest") { NSApp.terminate(nil) }
            return
        }
        let p = Process()
        p.executableURL = Place.python
        let selftest = CommandLine.arguments.contains("--selftest")
        p.arguments = [Place.code.appendingPathComponent("jarvis.py").path] + (selftest ? ["check"] : [])
        p.currentDirectoryURL = Place.home
        p.environment = Place.environment()
        try? FileManager.default.createDirectory(at: Place.logs, withIntermediateDirectories: true)
        let out = Place.logs.appendingPathComponent("jarvis.out")
        // Keep the console log small: start it afresh once it passes 2 MB.
        if let size = (try? FileManager.default.attributesOfItem(atPath: out.path))?[.size] as? Int, size > 2_000_000 {
            try? FileManager.default.removeItem(at: out)
        }
        if !FileManager.default.fileExists(atPath: out.path) { FileManager.default.createFile(atPath: out.path, contents: nil) }
        if let h = try? FileHandle(forWritingTo: out) {
            h.seekToEndOfFile()
            p.standardOutput = h
            p.standardError = h
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.childEnded(proc.terminationStatus) }
        }
        do {
            try p.run()
            child = p
            statusLine.title = "Listening for “Hey Jarvis” · hold fn to type"
        } catch {
            statusLine.title = "Jarvis couldn't start (see the log)"
        }
    }

    func childEnded(_ status: Int32) {
        child = nil
        if quitting { NSApp.terminate(nil); return }
        if status == 0 || CommandLine.arguments.contains("--selftest") {
            NSApp.terminate(nil)
            return
        }
        restarts = restarts.filter { $0.timeIntervalSinceNow > -120 } + [Date()]
        if restarts.count <= 3 {
            statusLine.title = "Restarting after a problem…"
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.start() }
        } else {
            statusLine.title = "Jarvis stopped after repeated problems (see the log)"
        }
    }

    @objc func toggleLogin(_ sender: NSMenuItem) {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister(); sender.state = .off
            } else {
                try SMAppService.mainApp.register(); sender.state = .on
            }
        } catch {
            let a = NSAlert()
            a.messageText = "macOS didn't allow that"
            a.informativeText = "Move Jarvis into your Applications folder first, or add it under System Settings › General › Login Items."
            a.runModal()
        }
    }

    @objc func showLog() {
        let f = Place.logs.appendingPathComponent("actions.log")
        if FileManager.default.fileExists(atPath: f.path) { NSWorkspace.shared.open(f) } else { showFolder() }
    }

    @objc func showFolder() {
        try? FileManager.default.createDirectory(at: Place.home, withIntermediateDirectories: true)
        NSWorkspace.shared.open(Place.home)
    }

    @objc func about() {
        let a = NSAlert()
        a.messageText = "Jarvis for Mac \(Place.version)"
        a.informativeText = "Free and open source (MIT). Runs entirely on this Mac.\n\nYour settings, memory and models are in:\n~/Library/Application Support/Jarvis for Mac\nDelete that folder and this app to remove Jarvis completely."
        if !Place.repo.isEmpty { a.addButton(withTitle: "OK"); a.addButton(withTitle: "Open the GitHub page") }
        NSApp.activate(ignoringOtherApps: true)
        if a.runModal() == .alertSecondButtonReturn, let u = URL(string: Place.repo) { NSWorkspace.shared.open(u) }
    }

    @objc func restart() {
        if let c = child, c.isRunning {
            c.terminationHandler = nil
            c.terminate()
            c.waitUntilExit()
        }
        child = nil
        start()
    }

    @objc func quit() {
        quitting = true
        if let c = child, c.isRunning {
            c.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { NSApp.terminate(nil) }
        } else {
            NSApp.terminate(nil)
        }
    }

    func applicationWillTerminate(_ n: Notification) {
        if let c = child, c.isRunning { c.terminate() }
    }
}

// Snapshot mode, to check how setup looks without clicking through it:
//   Jarvis --snapshot out.png <page 0-5>
let cli = CommandLine.arguments
if cli.count >= 4, cli[1] == "--snapshot" {
    MainActor.assumeIsolated {
        Snap.on = true
        let s = Setup()
        s.page = Setup.Page(rawValue: Int(cli[3]) ?? 0) ?? .welcome
        if cli.count > 4 { s.installer.running = true; s.installer.step = "Downloading the free AI models: 1.2 of 2.7 GB"; s.installer.progress = 0.44 }
        let r = ImageRenderer(content: SetupView(s: s, installer: s.installer).background(Color(nsColor: .windowBackgroundColor)))
        r.scale = 2
        if let img = r.nsImage, let t = img.tiffRepresentation, let rep = NSBitmapImageRep(data: t),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: cli[2])); print("wrote \(cli[2])")
        }
    }
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = Controller()
app.delegate = controller
app.run()
