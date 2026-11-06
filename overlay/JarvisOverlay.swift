// Jarvis's on-screen presence: a floating HUD ring that appears when Jarvis is woken.
//
// This is a small helper program. The Python core starts it and writes one JSON
// object per line to its standard input, for example:
//   {"state":"listening","level":0.4,"text":"Yes, Sir?"}
// States: hidden, waking, listening, thinking, speaking, confirm, error.
// The window never takes focus and never catches clicks, so it cannot get in the
// way of whatever you are doing. It closes itself when its input ends.

import AppKit
import SwiftUI

// MARK: - Shared state

final class HUDState: ObservableObject {
    @Published var state: String = "hidden"
    @Published var text: String = ""
    @Published var subtext: String = ""
    @Published var shownAt: Date = .distantPast
    var targetLevel: Double = 0
    var level: Double = 0          // smoothed, read by the renderer every frame

    var visible: Bool { state != "hidden" }

    /// The colour of each state. Not colour alone: the caption also names the state.
    var tint: Color {
        switch state {
        case "confirm": return Color(red: 1.00, green: 0.72, blue: 0.25)   // amber: waiting for your word
        case "error":   return Color(red: 1.00, green: 0.33, blue: 0.30)
        case "thinking": return Color(red: 0.55, green: 0.80, blue: 1.00)
        default:        return Color(red: 0.30, green: 0.86, blue: 1.00)   // cyan
        }
    }
}

// MARK: - The ring

struct HUDView: View {
    @ObservedObject var model: HUDState
    let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion

    var body: some View {
        VStack(spacing: 10) {
            TimelineView(.animation) { timeline in
                Canvas { ctx, size in
                    draw(ctx: ctx, size: size, time: timeline.date.timeIntervalSinceReferenceDate)
                }
            }
            .frame(width: 190, height: 190)

            VStack(spacing: 3) {
                if !model.text.isEmpty {
                    Text(model.text)
                        .font(.system(size: 15, weight: .medium, design: .rounded))
                        .foregroundStyle(.white)
                        .multilineTextAlignment(.center)
                        .lineLimit(3)
                }
                if !model.subtext.isEmpty {
                    Text(model.subtext.uppercased())
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .tracking(2.2)
                        .foregroundStyle(model.tint.opacity(0.9))
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, (model.text.isEmpty && model.subtext.isEmpty) ? 0 : 9)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color.black.opacity((model.text.isEmpty && model.subtext.isEmpty) ? 0 : 0.74))
            )
            .frame(maxWidth: 420)
        }
        .frame(width: 460, height: 330, alignment: .top)
        .opacity(model.visible ? 1 : 0)
        .scaleEffect(model.visible ? 1 : 0.86)
        .animation(.easeOut(duration: reduceMotion ? 0.01 : 0.28), value: model.visible)
        .animation(.easeInOut(duration: 0.25), value: model.state)
    }

    private func draw(ctx: GraphicsContext, size: CGSize, time t: Double) {
        // Smooth the level so the core breathes rather than flickers.
        model.level += (model.targetLevel - model.level) * 0.25
        let lvl = max(0, min(1, model.level))
        let c = CGPoint(x: size.width / 2, y: size.height / 2)
        let tint = model.tint
        let st = model.state
        let speed: Double = reduceMotion ? 0 : (st == "thinking" ? 2.6 : (st == "confirm" ? 0.5 : 1.0))
        let sinceShown = Date().timeIntervalSince(model.shownAt)
        let boot = reduceMotion ? 1.0 : min(1.0, sinceShown / 0.45)   // rings draw themselves in on wake

        func arc(_ r: CGFloat, from a0: Double, to a1: Double, width: CGFloat, alpha: Double) {
            var p = Path()
            p.addArc(center: c, radius: r, startAngle: .radians(a0), endAngle: .radians(a1), clockwise: false)
            ctx.stroke(p, with: .color(tint.opacity(alpha)), style: StrokeStyle(lineWidth: width, lineCap: .round))
        }

        // A soft dark backing, so the rings read on a white page as well as a dark one.
        let backR: CGFloat = 98
        ctx.fill(Path(ellipseIn: CGRect(x: c.x - backR, y: c.y - backR, width: backR * 2, height: backR * 2)),
                 with: .radialGradient(Gradient(colors: [Color.black.opacity(0.62), Color.black.opacity(0.5), Color.black.opacity(0)]),
                                       center: c, startRadius: 0, endRadius: backR))

        // Soft outer glow
        let glowR: CGFloat = 70 + CGFloat(lvl) * 16
        ctx.fill(Path(ellipseIn: CGRect(x: c.x - glowR * 1.25, y: c.y - glowR * 1.25, width: glowR * 2.5, height: glowR * 2.5)),
                 with: .radialGradient(Gradient(colors: [tint.opacity(0.22 + lvl * 0.25), .clear]),
                                       center: c, startRadius: 4, endRadius: glowR * 1.25))

        // Ring 1: outer segmented arcs, slowly turning
        let r1: CGFloat = 86
        let segs = 6
        for i in 0..<segs {
            let base = Double(i) / Double(segs) * 2 * .pi + t * 0.35 * speed
            let span = (2 * .pi / Double(segs)) * 0.62 * boot
            arc(r1, from: base, to: base + span, width: 2.2, alpha: 0.85)
        }

        // Ring 2: tick marks turning the other way
        let r2: CGFloat = 74
        let ticks = 60
        for i in 0..<Int(Double(ticks) * boot) {
            let a = CGFloat(Double(i) / Double(ticks) * 2 * .pi - t * 0.22 * speed)
            let long = i % 5 == 0
            let inner = r2 - (long ? 7 : 3.5)
            var p = Path()
            p.move(to: CGPoint(x: c.x + cos(a) * inner, y: c.y + sin(a) * inner))
            p.addLine(to: CGPoint(x: c.x + cos(a) * r2, y: c.y + sin(a) * r2))
            ctx.stroke(p, with: .color(tint.opacity(long ? 0.75 : 0.35)), lineWidth: long ? 1.6 : 1)
        }

        // Ring 3: a bright sweep that reacts to sound
        let r3: CGFloat = 60
        arc(r3, from: 0, to: 2 * .pi, width: 1, alpha: 0.25)
        let sweepStart = t * 1.4 * speed
        arc(r3, from: sweepStart, to: sweepStart + 0.9 + lvl * 2.2, width: 3, alpha: 0.95)
        arc(r3, from: sweepStart + .pi, to: sweepStart + .pi + 0.4 + lvl * 1.2, width: 3, alpha: 0.6)

        // Ring 4: equaliser bars around the core, driven by the level
        let r4: CGFloat = 44
        let bars = 36
        for i in 0..<bars {
            let a = CGFloat(Double(i) / Double(bars) * 2 * .pi)
            let wobble = reduceMotion ? 0.5 : (0.5 + 0.5 * sin(t * 7 + Double(i) * 1.7))
            let h = 2 + CGFloat(lvl * 12 * wobble)
            var p = Path()
            p.move(to: CGPoint(x: c.x + cos(a) * r4, y: c.y + sin(a) * r4))
            p.addLine(to: CGPoint(x: c.x + cos(a) * (r4 + h), y: c.y + sin(a) * (r4 + h)))
            ctx.stroke(p, with: .color(tint.opacity(0.7)), lineWidth: 1.6)
        }

        // The core
        let breathe = reduceMotion ? 0 : 0.06 * sin(t * 2.2)
        let coreR: CGFloat = 26 * CGFloat(1 + breathe + lvl * 0.35)
        ctx.fill(Path(ellipseIn: CGRect(x: c.x - coreR, y: c.y - coreR, width: coreR * 2, height: coreR * 2)),
                 with: .radialGradient(Gradient(colors: [.white.opacity(0.95), tint.opacity(0.85), tint.opacity(0.0)]),
                                       center: c, startRadius: 0, endRadius: coreR))
        // Thin rim around the core
        arc(coreR + 5, from: 0, to: 2 * .pi, width: 1, alpha: 0.5)
    }
}

// MARK: - Window that never takes focus or clicks

final class HUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = HUDState()
    var panel: HUDPanel!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let size = NSSize(width: 460, height: 330)
        panel = HUDPanel(contentRect: NSRect(origin: .zero, size: size),
                         styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.contentView = NSHostingView(rootView: HUDView(model: model))
        place()
        panel.orderFrontRegardless()

        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in self?.place() }
        readInput()
    }

    /// Top centre of the screen that has the mouse, just under the menu bar.
    func place() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let vf = screen?.visibleFrame else { return }
        let x = vf.midX - panel.frame.width / 2
        let y = vf.maxY - panel.frame.height - 8
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    func readInput() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while let line = readLine(strippingNewline: true) {
                guard let data = line.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                DispatchQueue.main.async { self?.apply(obj) }
            }
            // Input closed: the Python core has stopped, so the helper stops too.
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }

    func apply(_ obj: [String: Any]) {
        if let s = obj["state"] as? String, s != model.state {
            if model.state == "hidden" && s != "hidden" {
                place()                        // appear on whichever screen you are using
                model.shownAt = Date()
            }
            model.state = s
            if s == "hidden" { model.targetLevel = 0 }
        }
        if let l = obj["level"] as? Double { model.targetLevel = l }
        if let tx = obj["text"] as? String { model.text = tx }
        if let sub = obj["subtext"] as? String { model.subtext = sub }
    }
}

// Snapshot mode, for checking the look without screen recording:
//   JarvisOverlay --snapshot out.png <state> <level> <text> <subtext> [seconds-since-shown]
let args = CommandLine.arguments
if args.count >= 7, args[1] == "--snapshot" {
    let m = HUDState()
    m.state = args[3]; m.targetLevel = Double(args[4]) ?? 0; m.level = m.targetLevel
    m.text = args[5]; m.subtext = args[6]
    m.shownAt = Date().addingTimeInterval(-(args.count > 7 ? Double(args[7]) ?? 2 : 2))
    MainActor.assumeIsolated {
    let light = ProcessInfo.processInfo.environment["SNAP_BG"] == "light"
    let view = HUDView(model: m)
        .background(LinearGradient(colors: light ? [Color(white: 0.97), Color(white: 0.90)] : [Color(white: 0.16), Color(white: 0.30)],
                                   startPoint: .top, endPoint: .bottom))
    let r = ImageRenderer(content: view)
    r.scale = 2
    if let img = r.nsImage, let tiff = img.tiffRepresentation,
       let rep = NSBitmapImageRep(data: tiff), let png = rep.representation(using: .png, properties: [:]) {
        try? png.write(to: URL(fileURLWithPath: args[2]))
        print("wrote \(args[2])")
    }
    }
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)     // no Dock icon, no menu bar takeover
let delegate = AppDelegate()
app.delegate = delegate
app.run()
