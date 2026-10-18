// goldmine-ocr — Apple Vision OCR helper for goldmine v2 (item B4, docs/v2/02 §5.2).
//
// One-shot batch process: Vision keeps ~100 MB of recognition models resident
// in whatever process ran OCR (DigUp IndexService.swift:4-8), so recognition
// happens in THIS disposable process, never inside goldmine itself.
//
// Modes:
//   goldmine-ocr --version          -> "goldmine-ocr 1" (doctor handshake)
//   goldmine-ocr PATH [PATH ...]    -> one JSON line per image on stdout
//   goldmine-ocr (stdin, non-tty)   -> batch: read one JSON line
//                                      {"id":"...","path":"..."} per image,
//                                      answer one JSON line per input
//
// Output line (either mode):
//   {"id":"...","path":"...","texts":[{"text":"..","confidence":0.97,
//    "box":[x,y,w,h]}],"ms":213}
//   {"id":"...","path":"...","error":"reason"}   (per-image failure; exit stays 0)
//
// Recognition settings are DigUp's measured-best (Extractors.swift:79-86):
// .accurate level (fast saved 8% time but lost an exact lookup), language
// correction ON, en-US + zh-Hans languages, full-resolution input ("small UI
// text is what screenshots are full of", :76-77).

import Foundation
import ImageIO
import Vision

struct OcrLine: Encodable {
    let text: String
    let confidence: Double
    let box: [Double]          // normalized [x, y, w, h] of the recognized line
}

struct OcrResult: Encodable {
    let id: String
    let path: String
    var texts: [OcrLine]?
    var ms: Double?
    var error: String?
}

let encoder = JSONEncoder()

func emit(_ result: OcrResult) {
    if let data = try? encoder.encode(result) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

/// Synchronous OCR of one image URL; never throws — errors ride the JSON line.
func recognize(url: URL, id: String) -> OcrResult {
    let started = Date()
    func elapsed() -> Double { Date().timeIntervalSince(started) * 1000.0 }
    guard let data = try? Data(contentsOf: url),
          let source = CGImageSourceCreateWithData(data as CFData, nil),
          let cgImage = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        return OcrResult(id: id, path: url.path, texts: nil,
                         ms: elapsed(), error: "cannot decode image")
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US", "zh-Hans"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return OcrResult(id: id, path: url.path, texts: nil,
                         ms: elapsed(), error: "\(error)")
    }
    var lines: [OcrLine] = []
    for observation in (request.results ?? []) {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let b = observation.boundingBox
        lines.append(OcrLine(
            text: candidate.string,
            confidence: Double(candidate.confidence),
            box: [Double(b.origin.x), Double(b.origin.y),
                  Double(b.width), Double(b.height)]
        ))
    }
    return OcrResult(id: id, path: url.path, texts: lines,
                     ms: elapsed(), error: nil)
}

// -- main ---------------------------------------------------------------------

let args = CommandLine.arguments.dropFirst()

if args.first == "--version" {
    print("goldmine-ocr 1")
    exit(0)
}

if args.isEmpty {
    // stdin batch mode: one {"id","path"} JSON line per input; exit 0 even
    // with per-image errors (02 §5.2 contract, LlamaEmbedder pattern).
    while let line = readLine() {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { continue }
        guard let data = trimmed.data(using: .utf8),
              let req = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let path = req["path"] as? String else {
            continue
        }
        let id = (req["id"] as? String) ?? path
        emit(recognize(url: URL(fileURLWithPath: path), id: id))
    }
    exit(0)
}

for arg in args {
    emit(recognize(url: URL(fileURLWithPath: arg), id: arg))
}
exit(0)
