// Render docs/demo/session.json (from record.py) into docs/demo.gif. macOS only: it uses
// CoreText and ImageIO, so nothing needs installing.
//
//     swift docs/demo/render.swift [session.json] [out.gif]

import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

let args = CommandLine.arguments
let here = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let input = args.count > 1 ? URL(fileURLWithPath: args[1]) : here.appendingPathComponent("session.json")
let output = args.count > 2
    ? URL(fileURLWithPath: args[2])
    : here.deletingLastPathComponent().appendingPathComponent("demo.gif")

struct Step: Decodable { let say: String?; let cmd: String?; let out: [String]? }
struct Session: Decodable { let persona: String; let steps: [Step] }
let session = try JSONDecoder().decode(Session.self, from: Data(contentsOf: input))

// --- look -------------------------------------------------------------------------------------
let cols = 100, rows = 30
let fontSize: CGFloat = 14
let font = CTFontCreateWithName("Menlo" as CFString, fontSize, nil)
let bold = CTFontCreateWithName("Menlo-Bold" as CFString, fontSize, nil)
let cellW: CGFloat = {
    var g = CGGlyph(), ch: UniChar = 0x4D  // "M"
    CTFontGetGlyphsForCharacters(font, &ch, &g, 1)
    var adv = CGSize()
    CTFontGetAdvancesForGlyphs(font, .horizontal, &g, &adv, 1)
    return adv.width
}()
let lineH: CGFloat = 18, pad: CGFloat = 16, titleH: CGFloat = 30
let width = Int(ceil(CGFloat(cols) * cellW + 2 * pad))
let height = Int(ceil(CGFloat(rows) * lineH + 2 * pad + titleH))

func rgb(_ hex: UInt32) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
}
let bg = rgb(0x1E1F24), chrome = rgb(0x2B2D33), fg = rgb(0xD7DAE0), dim = rgb(0x8A909C)
let green = rgb(0x8BD17C), yellow = rgb(0xE6C46A), red = rgb(0xEF7D7D), cyan = rgb(0x7CC7E8)

enum Kind { case prompt, comment, output }
struct Line { var text: String; var kind: Kind }

func color(for line: Line) -> CGColor {
    switch line.kind {
    case .comment: return dim
    case .prompt: return fg
    case .output:
        let t = line.text
        if t.hasPrefix("[ok") { return green }
        if t.hasPrefix("[WARN") { return yellow }
        if t.hasPrefix("[FAIL") { return red }
        if t.hasSuffix(":") && !t.hasPrefix(" ") { return cyan }  // section headings
        return fg
    }
}

func wrap(_ s: String, _ kind: Kind) -> [Line] {
    var out: [Line] = [], rest = Substring(s)
    while rest.count > cols {  // break at the last space that fits, else hard-break
        let head = rest.prefix(cols)
        let cut = head.lastIndex(of: " ").map { rest.distance(from: rest.startIndex, to: $0) } ?? cols
        out.append(Line(text: String(rest.prefix(cut)), kind: kind))
        rest = rest.dropFirst(cut).drop(while: { $0 == " " })
    }
    out.append(Line(text: String(rest), kind: kind))
    return out
}

// --- drawing ----------------------------------------------------------------------------------
let space = CGColorSpace(name: CGColorSpace.sRGB)!

func draw(_ text: String, x: CGFloat, y: CGFloat, color: CGColor, font: CTFont, in ctx: CGContext) {
    let attrs: [NSAttributedString.Key: Any] = [
        NSAttributedString.Key(kCTFontAttributeName as String): font,
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
    ]
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attrs))
    ctx.textPosition = CGPoint(x: x, y: y)
    CTLineDraw(line, ctx)
}

func frame(_ screen: [Line], cursor: Bool) -> CGImage {
    let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                        space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    let H = CGFloat(height), W = CGFloat(width)
    ctx.setFillColor(bg); ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    ctx.setFillColor(chrome); ctx.fill(CGRect(x: 0, y: H - titleH, width: W, height: titleH))
    for (i, c) in [red, yellow, green].enumerated() {
        ctx.setFillColor(c)
        ctx.fillEllipse(in: CGRect(x: 14 + CGFloat(i) * 20, y: H - titleH / 2 - 6, width: 12, height: 12))
    }
    let title = "jobhunt — synthetic persona \(session.persona), offline fixtures"
    draw(title, x: (W - CGFloat(title.count) * cellW) / 2, y: H - titleH / 2 - 5, color: dim,
         font: font, in: ctx)
    let shown = screen.suffix(rows)
    for (i, line) in shown.enumerated() {
        let y = H - titleH - pad - CGFloat(i + 1) * lineH + 4
        if line.kind == .prompt, line.text.hasPrefix("$ ") {
            draw("$", x: pad, y: y, color: green, font: bold, in: ctx)
            draw(String(line.text.dropFirst(2)), x: pad + 2 * cellW, y: y, color: fg, font: bold, in: ctx)
        } else {
            draw(line.text, x: pad, y: y, color: color(for: line), font: font, in: ctx)
        }
    }
    if cursor, let last = shown.last {
        let i = shown.count - 1
        let x = pad + CGFloat(last.text.count) * cellW
        let y = H - titleH - pad - CGFloat(i + 1) * lineH
        ctx.setFillColor(fg); ctx.fill(CGRect(x: x, y: y, width: cellW, height: lineH - 2))
    }
    return ctx.makeImage()!
}

// --- timeline ---------------------------------------------------------------------------------
var frames: [(CGImage, Double)] = []
var screen: [Line] = []

func show(_ delay: Double, cursor: Bool = false) { frames.append((frame(screen, cursor: cursor), delay)) }

func type(_ text: String, kind: Kind, chunk: Int) {
    let prompt = kind == .prompt ? "$ " : ""
    let full = prompt + text
    screen.append(Line(text: prompt, kind: kind))
    var n = prompt.count
    while n < full.count {
        n = min(full.count, n + chunk)
        screen[screen.count - 1].text = String(full.prefix(n))
        show(0.05, cursor: true)
    }
}

screen.append(Line(text: "$ ", kind: .prompt))
show(0.8, cursor: true)
screen.removeLast()
for step in session.steps {
    if let say = step.say {
        type(say, kind: .comment, chunk: 12)
        show(1.6)
    } else if let cmd = step.cmd {
        type(cmd, kind: .prompt, chunk: 5)
        show(0.35)
        let out = step.out ?? []
        for l in out { screen += wrap(l, .output) }
        show(out.isEmpty ? 0.3 : min(5.5, 1.4 + 0.12 * Double(out.count)))
    }
}
screen.append(Line(text: "$ ", kind: .prompt))
show(3.0, cursor: true)

// --- write ------------------------------------------------------------------------------------
let dest = CGImageDestinationCreateWithURL(output as CFURL, UTType.gif.identifier as CFString,
                                           frames.count, nil)!
CGImageDestinationSetProperties(dest, [
    kCGImagePropertyGIFDictionary: [kCGImagePropertyGIFLoopCount: 0],
] as CFDictionary)
for (img, delay) in frames {
    CGImageDestinationAddImage(dest, img, [
        kCGImagePropertyGIFDictionary: [kCGImagePropertyGIFDelayTime: delay],
    ] as CFDictionary)
}
guard CGImageDestinationFinalize(dest) else { fatalError("could not write \(output.path)") }
let bytes = (try? FileManager.default.attributesOfItem(atPath: output.path)[.size] as? Int) ?? 0
print("wrote \(output.path): \(frames.count) frames, \(width)x\(height), \(bytes / 1024) KB")
