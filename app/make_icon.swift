// Draws the Grab app icon (1024x1024 PNG): a gradient tile with a download arrow into a tray.
// Usage: swift make_icon.swift out.png
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

// macOS icon grid: 824pt tile centred in 1024 with a soft shadow.
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
let path = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 30, color: NSColor.black.withAlphaComponent(0.35).cgColor)
ctx.addPath(path)
ctx.setFillColor(NSColor.black.cgColor)
ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(path)
ctx.clip()
let colors = [NSColor(srgbRed: 0.33, green: 0.55, blue: 1.0, alpha: 1).cgColor,
              NSColor(srgbRed: 0.42, green: 0.25, blue: 0.92, alpha: 1).cgColor] as CFArray
let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1])!
ctx.drawLinearGradient(grad, start: CGPoint(x: 100, y: 924), end: CGPoint(x: 924, y: 100), options: [])
// subtle top highlight
ctx.setFillColor(NSColor.white.withAlphaComponent(0.08).cgColor)
ctx.fillEllipse(in: CGRect(x: -100, y: 560, width: 1224, height: 700))
ctx.restoreGState()

// arrow
ctx.setFillColor(NSColor.white.cgColor)
ctx.setStrokeColor(NSColor.white.cgColor)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
ctx.setLineWidth(78)
ctx.move(to: CGPoint(x: 512, y: 760))
ctx.addLine(to: CGPoint(x: 512, y: 420))
ctx.strokePath()
ctx.move(to: CGPoint(x: 360, y: 500))
ctx.addLine(to: CGPoint(x: 512, y: 348))
ctx.addLine(to: CGPoint(x: 664, y: 500))
ctx.strokePath()
// tray
ctx.setLineWidth(64)
ctx.move(to: CGPoint(x: 290, y: 330))
ctx.addLine(to: CGPoint(x: 290, y: 250))
ctx.addLine(to: CGPoint(x: 734, y: 250))
ctx.addLine(to: CGPoint(x: 734, y: 330))
ctx.strokePath()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
