import AVFoundation
import Foundation

enum RecorderTestFailure: Error { case check(String) }
func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
  guard condition() else { throw RecorderTestFailure.check(message) }
  print("PASS \(message)")
}

func readJSON(_ file: URL) throws -> [String: Any] {
  try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [String: Any]
}

// Handles RFC 4180 quoting, including embedded newlines in diagnostic reasons.
func readCSV(_ file: URL) throws -> [[String: String]] {
  let text = try String(contentsOf: file, encoding: .utf8)
  var records: [[String]] = [], record: [String] = [], field = "", quoted = false
  var index = text.startIndex
  while index < text.endIndex {
    let ch = text[index], next = text.index(after: index)
    if ch == "\"" {
      if quoted && next < text.endIndex && text[next] == "\"" {
        field.append("\""); index = text.index(after: next); continue
      }
      quoted.toggle()
    } else if ch == "," && !quoted { record.append(field); field = "" }
    else if ch == "\n" && !quoted { record.append(field); records.append(record); record = []; field = "" }
    else if ch != "\r" || quoted { field.append(ch) }
    index = next
  }
  if !field.isEmpty || !record.isEmpty { record.append(field); records.append(record) }
  guard let header = records.first else { throw RecorderTestFailure.check("missing CSV header") }
  try require(Set(header).count == header.count, "\(file.lastPathComponent) unique columns")
  return try records.dropFirst().map { row in
    try require(row.count == header.count, "\(file.lastPathComponent) row width")
    return Dictionary(uniqueKeysWithValues: zip(header, row))
  }
}

func makeAudio(_ samples: [Int16], sampleRate: Double = 16_000, channels: AVAudioChannelCount = 1) throws -> AVAudioPCMBuffer {
  let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: channels, interleaved: true)!
  let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count / Int(channels)))!
  buffer.frameLength = buffer.frameCapacity
  guard let pointer = buffer.int16ChannelData?[0] else { throw RecorderTestFailure.check("audio allocation") }
  for (index, sample) in samples.enumerated() { pointer[index] = sample }
  return buffer
}

func makeVideoSample(index: Int, sourcePTS: CMTime? = nil) throws -> CMSampleBuffer {
  var possible: CVPixelBuffer?
  let width = 128, height = 192
  guard CVPixelBufferCreate(kCFAllocatorDefault, width, height, kCVPixelFormatType_32BGRA,
    [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &possible) == kCVReturnSuccess,
    let pixel = possible else { throw RecorderTestFailure.check("synthetic video allocation") }
  CVPixelBufferLockBaseAddress(pixel, [])
  let base = CVPixelBufferGetBaseAddress(pixel)!
  for y in 0..<height {
    let p = base.advanced(by: y * CVPixelBufferGetBytesPerRow(pixel)).assumingMemoryBound(to: UInt32.self)
    for x in 0..<width { p[x] = 0xff000000 | UInt32((x + index * 15) % 256) << 16 | UInt32(y % 256) << 8 }
  }
  CVPixelBufferUnlockBaseAddress(pixel, [])
  var description: CMVideoFormatDescription?
  guard CMVideoFormatDescriptionCreateForImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: pixel,
    formatDescriptionOut: &description) == noErr, let description else { throw RecorderTestFailure.check("video format") }
  var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: 7),
    presentationTimeStamp: sourcePTS ?? CMTime(value: 9007199254740993 + Int64(index) * 142857143, timescale: 1_000_000_000), decodeTimeStamp: .invalid)
  var sample: CMSampleBuffer?
  guard CMSampleBufferCreateReadyWithImageBuffer(allocator: kCFAllocatorDefault, imageBuffer: pixel,
    formatDescription: description, sampleTiming: &timing, sampleBufferOut: &sample) == noErr, let sample else {
    throw RecorderTestFailure.check("video sample")
  }
  return sample
}

struct WaveDescription {
  let format: UInt16
  let channels: UInt16
  let sampleRate: UInt32
  let bits: UInt16
  let payload: Data
}
func readWave(_ file: URL) throws -> WaveDescription {
  let d = try Data(contentsOf: file)
  func uint16(_ offset: Int) -> UInt16 { UInt16(d[offset]) | UInt16(d[offset + 1]) << 8 }
  func uint32(_ offset: Int) -> UInt32 { UInt32(d[offset]) | UInt32(d[offset + 1]) << 8 | UInt32(d[offset + 2]) << 16 | UInt32(d[offset + 3]) << 24 }
  func text(_ range: Range<Int>) -> String { String(decoding: d[range], as: UTF8.self) }
  try require(d.count >= 44 && text(0..<4) == "RIFF" && text(8..<12) == "WAVE", "valid WAV envelope")
  try require(UInt64(uint32(4)) + 8 == d.count, "WAV RIFF length matches file")
  var offset = 12, format: UInt16?, channels: UInt16?, rate: UInt32?, bits: UInt16?, payload: Data?
  while offset + 8 <= d.count {
    let name = text(offset..<(offset + 4)), length = Int(uint32(offset + 4)), begin = offset + 8
    try require(begin + length <= d.count, "WAV chunk length in bounds")
    if name == "fmt " {
      try require(length >= 16, "WAV fmt length")
      format = uint16(begin); channels = uint16(begin + 2); rate = uint32(begin + 4); bits = uint16(begin + 14)
    }
    if name == "data" { payload = Data(d[begin..<(begin + length)]) }
    offset = begin + length + length % 2
  }
  guard let format, let channels, let rate, let bits, let payload else { throw RecorderTestFailure.check("WAV mandatory chunks") }
  return WaveDescription(format: format, channels: channels, sampleRate: rate, bits: bits, payload: payload)
}

func readMoviePTS(_ file: URL) async throws -> [Double] {
  let asset = AVURLAsset(url: file)
  let tracks = try await asset.loadTracks(withMediaType: .video)
  guard let track = tracks.first else { throw RecorderTestFailure.check("movie video track absent") }
  let reader = try AVAssetReader(asset: asset)
  let output = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
  reader.add(output)
  guard reader.startReading() else { throw RecorderTestFailure.check("movie PTS reader start") }
  var result: [Double] = []
  while let sample = output.copyNextSampleBuffer() {
    result.append(CMSampleBufferGetPresentationTimeStamp(sample).seconds)
  }
  try require(reader.status == .completed, "movie PTS reader completes")
  return result
}
