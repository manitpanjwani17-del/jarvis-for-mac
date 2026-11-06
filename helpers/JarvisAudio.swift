// Jarvis's ears and mouth together, with echo cancellation.
//
// Jarvis needs to hear you while he is talking, so you can interrupt him. Without help, the
// microphone also hears his own voice from the speakers. This helper runs the microphone and his
// voice through macOS's voice processing (the same echo cancellation FaceTime uses), so what
// reaches Jarvis is your voice with his own taken out.
//
// Protocol (Jarvis's Python side talks to this over pipes):
//   stdout  microphone audio: 16 kHz, mono, 16-bit little-endian, continuously
//   stdin   commands: 1 byte type + 4 byte little-endian length + payload
//             'P' play: float32 mono 24 kHz samples (queued after anything already playing)
//             'S' stop: stop speaking now and drop anything queued
//             'V' volume: one float32, 0…1 (used to lower his voice while checking an interruption)
//   stderr  events, one JSON object per line: {"ready":true,"aec":true}, {"played":N}, {"error":"…"}
//
// Audio is only passed through. Nothing is recorded or saved.

import AVFoundation
import Foundation

let out = FileHandle.standardOutput
let err = FileHandle.standardError
let lock = NSLock()

func event(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj) {
        lock.lock(); err.write(d + Data([10])); lock.unlock()
    }
}

// ---- --probe: try different ways to set up echo cancellation on this Mac; report which work.
// Each try listens for under a second; nothing is kept. Prints one JSON line and exits.
if CommandLine.arguments.contains("--probe") {
    var results: [[String: Any]] = []
    func tryConfig(_ name: String, _ setup: (AVAudioEngine) throws -> Void) {
        let e = AVAudioEngine()
        var r: [String: Any] = ["config": name]
        var peak: Float = 0
        do {
            try setup(e)
            let fmt = e.inputNode.outputFormat(forBus: 0)
            r["input_rate"] = fmt.sampleRate
            r["input_channels"] = fmt.channelCount
            e.inputNode.installTap(onBus: 0, bufferSize: 1024, format: fmt) { b, _ in
                if let d = b.floatChannelData { for i in 0..<Int(b.frameLength) { peak = max(peak, abs(d[0][i])) } }
            }
            e.prepare()
            try e.start()
            Thread.sleep(forTimeInterval: 0.9)
            r["started"] = true
            r["heard_sound"] = peak > 0.0005
            r["peak"] = peak
            e.stop()
            e.inputNode.removeTap(onBus: 0)
        } catch {
            r["started"] = false
            r["error"] = "\(error)"
        }
        results.append(r)
        Thread.sleep(forTimeInterval: 0.3)
    }
    tryConfig("plain") { _ in }
    tryConfig("vp-input") { e in try e.inputNode.setVoiceProcessingEnabled(true) }
    tryConfig("vp-both-touch-output-first") { e in
        _ = e.outputNode; _ = e.mainMixerNode
        try e.inputNode.setVoiceProcessingEnabled(true)
        try e.outputNode.setVoiceProcessingEnabled(true)
    }
    tryConfig("vp-input-in-graph") { e in
        _ = e.mainMixerNode
        try e.inputNode.setVoiceProcessingEnabled(true)
        let m = AVAudioMixerNode()
        e.attach(m)
        e.connect(e.inputNode, to: m, format: e.inputNode.outputFormat(forBus: 0))
        e.connect(m, to: e.mainMixerNode, format: nil)
        m.outputVolume = 0
    }
    if let d = try? JSONSerialization.data(withJSONObject: ["probe": results]) {
        FileHandle.standardError.write(d + Data([10]))
    }
    exit(0)
}

// Set up the engine. Several ways are tried, in order, and the first that starts is used; the
// --probe results on this Mac showed voice processing starts only with some of them.
let voiceFormat = AVAudioFormat(standardFormatWithSampleRate: 24000, channels: 1)!
var engine = AVAudioEngine()
var player = AVAudioPlayerNode()
var playFormat = voiceFormat
var aec = false
var variantUsed = "none"

func build(_ variant: String) -> Bool {
    let e = AVAudioEngine()
    let p = AVAudioPlayerNode()
    do {
        if variant != "vp-late-mixer" { _ = e.mainMixerNode }         // mixer exists before voice processing
        if variant != "plain" { try e.inputNode.setVoiceProcessingEnabled(true) }
        if variant != "plain", #available(macOS 14.0, *) {
            // Don't turn other apps (your music) down while Jarvis is running.
            e.inputNode.voiceProcessingOtherAudioDuckingConfiguration =
                AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
        }
        if variant == "vp-input-in-graph" || variant == "vp-late-mixer" {
            let m = AVAudioMixerNode()                                  // the microphone inside the graph, silenced
            e.attach(m)
            e.connect(e.inputNode, to: m, format: e.inputNode.outputFormat(forBus: 0))
            e.connect(m, to: e.mainMixerNode, format: nil)
            m.outputVolume = 0
        }
        let rate = e.mainMixerNode.outputFormat(forBus: 0).sampleRate
        let pf = AVAudioFormat(standardFormatWithSampleRate: rate > 0 ? rate : 48000, channels: 1)!
        e.attach(p)
        e.connect(p, to: e.mainMixerNode, format: pf)
        e.prepare()
        try e.start()
        e.stop()            // started fine; the tap is added before starting for real
        engine = e; player = p; playFormat = pf
        aec = variant != "plain"
        variantUsed = variant
        return true
    } catch {
        event(["warning": "setup-failed", "variant": variant, "detail": "\(error)"])
        return false
    }
}
for v in ["vp-input-in-graph", "vp-mixer-first", "vp-late-mixer", "plain"] {
    if build(v) { break }
}
guard variantUsed != "none" else { event(["error": "start-failed"]); exit(1) }
let input = engine.inputNode
let upsample = AVAudioConverter(from: voiceFormat, to: playFormat)!

// ---- the microphone, converted to what Jarvis's ears expect
let inFormat = input.outputFormat(forBus: 0)
let monoIn = AVAudioFormat(standardFormatWithSampleRate: inFormat.sampleRate, channels: 1)!
let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
guard let converter = AVAudioConverter(from: monoIn, to: target) else {
    event(["error": "no-converter"]); exit(1)
}

input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { buf, _ in
    guard let src = buf.floatChannelData, buf.frameLength > 0,
          let mono = AVAudioPCMBuffer(pcmFormat: monoIn, frameCapacity: buf.frameLength) else { return }
    mono.frameLength = buf.frameLength
    memcpy(mono.floatChannelData![0], src[0], Int(buf.frameLength) * MemoryLayout<Float>.size)   // channel 0
    let cap = AVAudioFrameCount(Double(buf.frameLength) * 16000 / inFormat.sampleRate) + 32
    guard let res = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
    var given = false
    var e: NSError?
    converter.convert(to: res, error: &e) { _, status in
        if given { status.pointee = .noDataNow; return nil }
        given = true
        status.pointee = .haveData
        return mono
    }
    if res.frameLength > 0, let p = res.int16ChannelData {
        out.write(Data(bytes: p[0], count: Int(res.frameLength) * 2))
    }
}

engine.prepare()
do {
    try engine.start()
} catch {
    event(["error": "start-failed", "detail": "\(error)"]); exit(1)
}
player.play()
event(["ready": true, "aec": aec, "setup": variantUsed])

// ---- commands from Jarvis
var played = 0
func readExactly(_ n: Int) -> Data? {
    var d = Data()
    while d.count < n {
        let chunk = FileHandle.standardInput.readData(ofLength: n - d.count)
        if chunk.isEmpty { return nil }
        d.append(chunk)
    }
    return d
}

while let head = readExactly(5) {
    let kind = head[0]
    let len = Int(head[1]) | Int(head[2]) << 8 | Int(head[3]) << 16 | Int(head[4]) << 24
    guard len >= 0, len < 50_000_000, let body = len > 0 ? readExactly(len) : Data() else { break }
    switch kind {
    case UInt8(ascii: "P"):
        let n = len / 4
        guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: voiceFormat, frameCapacity: AVAudioFrameCount(n)) else { continue }
        buf.frameLength = AVAudioFrameCount(n)
        body.withUnsafeBytes { raw in
            memcpy(buf.floatChannelData![0], raw.baseAddress!, n * 4)
        }
        let cap = AVAudioFrameCount(Double(n) * playFormat.sampleRate / 24000) + 64
        guard let conv = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: cap) else { continue }
        var given = false
        var cerr: NSError?
        upsample.reset()
        upsample.convert(to: conv, error: &cerr) { _, status in
            if given { status.pointee = .endOfStream; return nil }
            given = true
            status.pointee = .haveData
            return buf
        }
        player.scheduleBuffer(conv, completionCallbackType: .dataPlayedBack) { _ in
            played += 1
            event(["played": played])
        }
        if !player.isPlaying { player.play() }
    case UInt8(ascii: "S"):
        player.stop()          // drops everything queued; each dropped buffer still reports "played"
        player.play()
        event(["stopped": true])
    case UInt8(ascii: "V"):
        if body.count >= 4 {
            player.volume = body.withUnsafeBytes { $0.load(as: Float.self) }
        }
    default:
        break
    }
}
engine.stop()
