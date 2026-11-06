// Draws Jarvis's app icon: an original glowing ring on a dark tile, in the HUD's colours.
import AppKit
let size = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
ctx.addPath(CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil))
ctx.clip()
let bg = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                    colors: [CGColor(red: 0.04, green: 0.07, blue: 0.12, alpha: 1), CGColor(red: 0.08, green: 0.14, blue: 0.22, alpha: 1)] as CFArray,
                    locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 0, y: 1024), end: CGPoint(x: 0, y: 0), options: [])
let c = CGPoint(x: 512, y: 512)
let cyan = CGColor(red: 0.30, green: 0.86, blue: 1.0, alpha: 1)
let glow = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                      colors: [CGColor(red: 1, green: 1, blue: 1, alpha: 1), CGColor(red: 0.30, green: 0.86, blue: 1, alpha: 0.9),
                               CGColor(red: 0.30, green: 0.86, blue: 1, alpha: 0)] as CFArray, locations: [0, 0.35, 1])!
ctx.drawRadialGradient(glow, startCenter: c, startRadius: 0, endCenter: c, endRadius: 150, options: [])
ctx.setStrokeColor(cyan)
ctx.setLineCap(.round)
for i in 0..<6 {                                         // outer segmented ring
    let a0 = CGFloat(i) / 6 * 2 * .pi + 0.3
    ctx.setLineWidth(22)
    ctx.addArc(center: c, radius: 330, startAngle: a0, endAngle: a0 + 0.65, clockwise: false)
    ctx.strokePath()
}
for i in 0..<60 {                                        // ticks
    let a = CGFloat(i) / 60 * 2 * .pi
    let long = i % 5 == 0
    ctx.setLineWidth(long ? 10 : 5)
    ctx.setStrokeColor(cyan.copy(alpha: long ? 0.9 : 0.45)!)
    ctx.move(to: CGPoint(x: c.x + cos(a) * (long ? 228 : 246), y: c.y + sin(a) * (long ? 228 : 246)))
    ctx.addLine(to: CGPoint(x: c.x + cos(a) * 270, y: c.y + sin(a) * 270))
    ctx.strokePath()
}
ctx.setStrokeColor(cyan)
ctx.setLineWidth(26)
ctx.addArc(center: c, radius: 190, startAngle: 0.4, endAngle: 2.6, clockwise: false)
ctx.strokePath()
ctx.setLineWidth(8)
ctx.setStrokeColor(cyan.copy(alpha: 0.5)!)
ctx.addArc(center: c, radius: 190, startAngle: 0, endAngle: 2 * .pi, clockwise: false)
ctx.strokePath()
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
