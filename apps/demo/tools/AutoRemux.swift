import Foundation
import AVFoundation
import CryptoKit
import Darwin

private func failure(_ message: String) -> NSError {
  NSError(domain: "RoboMemoMediaPreparation", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
}

private struct Sample {
  let pts: CMTime
  let dts: CMTime
  let duration: CMTime
  let bytes: Int
  let sha256: String
}

private struct Inspection {
  let duration: CMTime
  let timeScale: CMTimeScale
  let width: Double
  let height: Double
  let codec: FourCharCode
  let playable: Bool
  let samples: [Sample]
  let emptyBuffers: Int
}

private func timeJSON(_ value: CMTime) -> [String: Any] {
  ["value": value.value, "timescale": value.timescale, "epoch": value.epoch,
   "flags": value.flags.rawValue,
   "seconds": value.seconds.isFinite ? value.seconds as Any : NSNull()]
}

private func inspect(_ url: URL) async throws -> Inspection {
  let asset = AVURLAsset(url: url)
  let tracks = try await asset.loadTracks(withMediaType: .video)
  guard tracks.count == 1, let track = tracks.first else { throw failure("Expected one video track") }
  guard try await asset.loadTracks(withMediaType: .audio).isEmpty else { throw failure("Unexpected audio track") }
  let duration = try await asset.load(.duration)
  let timeScale = try await track.load(.naturalTimeScale)
  let size = try await track.load(.naturalSize)
  let formats = try await track.load(.formatDescriptions)
  guard let format = formats.first else { throw failure("Missing video format") }
  let playable = try await asset.load(.isPlayable)
  let reader = try AVAssetReader(asset: asset)
  let output = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
  output.alwaysCopiesSampleData = false
  reader.add(output)
  guard reader.startReading() else { throw reader.error ?? failure("Compressed reading did not start") }
  var samples: [Sample] = []
  var emptyBuffers = 0
  while let buffer = output.copyNextSampleBuffer() {
    let bytes = CMSampleBufferGetTotalSampleSize(buffer)
    if bytes == 0 { emptyBuffers += 1; continue }
    guard CMSampleBufferGetNumSamples(buffer) == 1,
      let block = CMSampleBufferGetDataBuffer(buffer) else {
      throw failure("Unexpected compressed sample layout")
    }
    let length = CMBlockBufferGetDataLength(block)
    var data = Data(count: length)
    let status = data.withUnsafeMutableBytes { storage in
      CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: storage.baseAddress!)
    }
    guard status == kCMBlockBufferNoErr else { throw failure("Could not read compressed sample bytes") }
    samples.append(Sample(
      pts: CMSampleBufferGetPresentationTimeStamp(buffer),
      dts: CMSampleBufferGetDecodeTimeStamp(buffer),
      duration: CMSampleBufferGetDuration(buffer), bytes: bytes,
      sha256: SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()))
  }
  guard reader.status == .completed else { throw reader.error ?? failure("Compressed reading failed") }
  return Inspection(duration: duration, timeScale: timeScale,
                    width: size.width, height: size.height,
                    codec: CMFormatDescriptionGetMediaSubType(format),
                    playable: playable, samples: samples, emptyBuffers: emptyBuffers)
}

private func decodeAll(_ url: URL) async throws -> Int {
  let asset = AVURLAsset(url: url)
  guard let track = try await asset.loadTracks(withMediaType: .video).first else {
    throw failure("No video track to decode")
  }
  let reader = try AVAssetReader(asset: asset)
  let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
  ])
  output.alwaysCopiesSampleData = false
  reader.add(output)
  guard reader.startReading() else { throw reader.error ?? failure("Decoder did not start") }
  var frames = 0
  while let sample = output.copyNextSampleBuffer() {
    if CMSampleBufferGetImageBuffer(sample) != nil { frames += 1 }
  }
  guard reader.status == .completed else { throw reader.error ?? failure("Full decode failed") }
  return frames
}

private func equalTime(_ a: CMTime, _ b: CMTime) -> Bool {
  if !a.isValid || !b.isValid { return a.isValid == b.isValid }
  return CMTimeCompare(a, b) == 0 && a.epoch == b.epoch
}

@main private struct AutoRemux {
  static func main() async {
    var created: URL? = nil
    do {
      let args = CommandLine.arguments
      guard args.count == 4, args[1].hasPrefix("/"), args[2].hasPrefix("/"), args[3].hasPrefix("/") else {
        throw failure("Usage: auto-remux ABS_INPUT ABS_OUTPUT.mp4 ABS_REPORT.json")
      }
      let source = URL(fileURLWithPath: args[1]), destination = URL(fileURLWithPath: args[2])
      let reportURL = URL(fileURLWithPath: args[3])
      guard ["mov", "mp4"].contains(source.pathExtension.lowercased()), destination.pathExtension.lowercased() == "mp4",
        !FileManager.default.fileExists(atPath: destination.path), !FileManager.default.fileExists(atPath: reportURL.path) else {
        throw failure("Unsupported extension or output already exists")
      }
      let resource = try source.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
      guard resource.isRegularFile == true, let bytes = resource.fileSize, bytes > 0, bytes <= 1024 * 1024 * 1024 else { throw failure("Source is not a video file within 1 GiB") }
      let duration = try await AVURLAsset(url: source).load(.duration)
      guard duration.seconds.isFinite, duration.seconds > 0, duration.seconds <= 301 else { throw failure("Video must be no longer than 301 seconds") }
      let before = try await inspect(source)
      guard !before.samples.isEmpty, before.width <= 4096, before.height <= 4096,
        before.codec == kCMVideoCodecType_H264 else { throw failure("Expected an H.264 RoboMemo video") }
      guard let exporter = AVAssetExportSession(asset: AVURLAsset(url: source), presetName: AVAssetExportPresetPassthrough) else { throw failure("Passthrough is unsupported") }
      exporter.shouldOptimizeForNetworkUse = true
      created = destination
      try await exporter.export(to: destination, as: .mp4)
      let after = try await inspect(destination)
      let pairs = Array(zip(before.samples, after.samples))
      let durationDifferences = pairs.enumerated().filter { !equalTime($0.element.0.duration, $0.element.1.duration) }.map(\.offset)
      let finalDurationOnly = durationDifferences == [after.samples.count - 1]
        && after.samples.last.map { equalTime($0.duration, CMTimeSubtract(after.duration, $0.pts)) } == true
      let valid = before.samples.count == after.samples.count && equalTime(before.duration, after.duration)
        && (durationDifferences.isEmpty || finalDurationOnly)
        && pairs.allSatisfy { equalTime($0.0.pts, $0.1.pts) && equalTime($0.0.dts, $0.1.dts)
          && $0.0.bytes == $0.1.bytes && $0.0.sha256 == $0.1.sha256 }
        && before.codec == after.codec && before.width == after.width && before.height == after.height && after.playable
      guard valid else { throw failure("Passthrough changed video payload or timestamps") }
      let decoded = try await decodeAll(destination)
      guard decoded == before.samples.count else { throw failure("Playback did not fully decode") }
      let report: [String:Any] = ["version":1,"passed":true,"reencoded":false,"frames":decoded,
        "duration_seconds":after.duration.seconds,"payloads_and_timestamps_identical":true,
        "final_sample_duration_normalized":!durationDifferences.isEmpty]
      try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted,.sortedKeys]).write(to: reportURL, options: .atomic)
      print("Verified passthrough: \(decoded) frames")
    } catch {
      if let output = created { try? FileManager.default.removeItem(at: output) }
      FileHandle.standardError.write(Data("Remux failed: \(error.localizedDescription)\n".utf8)); exit(1)
    }
  }
}
