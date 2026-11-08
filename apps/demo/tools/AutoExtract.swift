import Foundation
import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import CryptoKit
import Darwin

private enum ExtractError: Error {
  case stopped(String)
}

private func require(_ condition: Bool, _ message: String) throws {
  if !condition { throw ExtractError.stopped(message) }
}

private struct SamplingRange: Codable {
  let start: Double
  let end: Double
  let interval: Double
}

private struct FrameTime: Codable {
  let frame_id: String
  let output_pts_seconds: Double
}

private struct SampledFrame: Codable {
  let frame_id: String
  let output_pts_seconds: Double
  /// One-based index in the complete sequential decode, never the sampling index.
  let source_frame_index: Int
  let image_file: String
  let width: Int
  let height: Int
  let sha256: String
  let bytes: Int
}

private struct Manifest: Encodable {
  let version = 1
  let duration_seconds: Double
  let decoded_frames: Int
  let width: Int
  let height: Int
  let sampling_version = "sequential-nearest-actual-pts-v1"
  let sampling_ranges: [SamplingRange]
  let source_frame_index_base = 1
  let frames: [SampledFrame]
  let all_frame_pts: [FrameTime]
}

private struct Arguments {
  let video: URL
  let output: URL
  let interval: Double
  let rangesURL: URL?

  static func parse() throws -> Arguments {
    let args = Array(CommandLine.arguments.dropFirst())
    try require(!args.isEmpty && args.count % 2 == 0,
                "usage: auto-extract --video ABS --output ABS [--interval SECONDS] [--ranges-json ABS]")
    var values: [String: String] = [:]
    for index in stride(from: 0, to: args.count, by: 2) {
      let key = args[index]
      try require(["--video", "--output", "--interval", "--ranges-json"].contains(key)
                  && values[key] == nil, "unknown or repeated argument")
      values[key] = args[index + 1]
    }
    guard let videoPath = values["--video"], let outputPath = values["--output"] else {
      throw ExtractError.stopped("video and output paths are required")
    }
    try require((videoPath as NSString).isAbsolutePath && (outputPath as NSString).isAbsolutePath,
                "video and output must be absolute local paths")
    let interval = Double(values["--interval"] ?? "1") ?? .nan
    try require(interval.isFinite && interval >= 0.1 && interval <= 301,
                "sampling interval must be between 0.1 and 301 seconds")
    var rangesURL: URL?
    if let path = values["--ranges-json"] {
      try require((path as NSString).isAbsolutePath, "ranges JSON must be an absolute local path")
      rangesURL = URL(fileURLWithPath: path)
    }
    return Arguments(video: URL(fileURLWithPath: videoPath).standardizedFileURL,
                     output: URL(fileURLWithPath: outputPath, isDirectory: true).standardizedFileURL,
                     interval: interval, rangesURL: rangesURL)
  }
}

private struct DecodedFrame {
  let index: Int
  let pts: Double
  let pixel: CVPixelBuffer
  var id: String { String(format: "f%06d", index) }
}

private final class ImageWriter {
  let directory: URL
  let transform: CGAffineTransform
  let context = CIContext(options: [.useSoftwareRenderer: true, .cacheIntermediates: false])
  var sampled: [Int: SampledFrame] = [:]

  init(directory: URL, transform: CGAffineTransform) {
    self.directory = directory
    self.transform = transform
  }

  func write(_ frame: DecodedFrame) throws {
    if sampled[frame.index] != nil { return }
    let transformed = CIImage(cvPixelBuffer: frame.pixel).transformed(by: transform)
    let extent = transformed.extent.integral
    try require(extent.width.isFinite && extent.height.isFinite
                && extent.width > 0 && extent.height > 0
                && extent.width <= 4096 && extent.height <= 4096,
                "decoded image dimensions exceed the supported bounds")
    let normalized = transformed.transformed(by: CGAffineTransform(translationX: -extent.minX,
                                                                  y: -extent.minY))
    var maximumEdge = min(1280.0, max(extent.width, extent.height))
    var accepted: (data: Data, width: Int, height: Int)?
    // Bound the transfer size without changing the decoded frame or its actual PTS.
    // Exceptionally detailed frames may also be reduced in dimensions after quality trials.
    for _ in 0..<7 {
      let scale = min(1, maximumEdge / max(extent.width, extent.height))
      let scaled = normalized.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
      let width = max(1, Int(floor(extent.width * scale)))
      let height = max(1, Int(floor(extent.height * scale)))
      guard let image = context.createCGImage(scaled,
        from: CGRect(x: 0, y: 0, width: width, height: height)) else {
        throw ExtractError.stopped("could not render a selected image")
      }
      for quality in [0.88, 0.76, 0.64, 0.50, 0.36, 0.22, 0.10] {
        let bytes = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(bytes,
          UTType.jpeg.identifier as CFString, 1, nil) else {
          throw ExtractError.stopped("could not create a JPEG encoder")
        }
        CGImageDestinationAddImage(destination, image,
          [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        try require(CGImageDestinationFinalize(destination), "JPEG encoding failed")
        if bytes.length <= 256 * 1024 {
          accepted = (bytes as Data, width, height)
          break
        }
      }
      if accepted != nil { break }
      maximumEdge *= 0.75
    }
    guard let result = accepted, !result.data.isEmpty else {
      throw ExtractError.stopped("could not fit a selected JPEG within its size limit")
    }
    let relativePath = "images/\(frame.id).jpg"
    try result.data.write(to: directory.appendingPathComponent(relativePath), options: .atomic)
    sampled[frame.index] = SampledFrame(frame_id: frame.id, output_pts_seconds: frame.pts,
      source_frame_index: frame.index, image_file: relativePath,
      width: result.width, height: result.height,
      sha256: SHA256.hash(data: result.data).map { String(format: "%02x", $0) }.joined(),
      bytes: result.data.count)
  }
}

@main private struct AutoExtract {
  static func main() async {
    do { try await run(Arguments.parse()) }
    catch ExtractError.stopped(let message) {
      FileHandle.standardError.write(Data("Extraction stopped: \(message).\n".utf8))
      exit(1)
    } catch {
      // Underlying framework errors can expose source metadata or local paths.
      FileHandle.standardError.write(Data("Extraction stopped: local media or file operation failed.\n".utf8))
      exit(1)
    }
  }

  static func run(_ args: Arguments) async throws {
    let manager = FileManager.default
    let resource = try args.video.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
    try require(resource.isRegularFile == true && (resource.fileSize ?? 0) > 0
                && (resource.fileSize ?? Int.max) <= 1024 * 1024 * 1024,
                "source must be a nonempty regular video file no larger than 1 GiB")
    let asset = AVURLAsset(url: args.video)
    let duration = try await asset.load(.duration).seconds
    try require(duration.isFinite && duration > 0 && duration <= 301,
                "video duration must be greater than zero and at most 301 seconds")
    let tracks = try await asset.loadTracks(withMediaType: .video)
    try require(tracks.count == 1, "exactly one video track is required")
    let track = tracks[0]
    let size = try await track.load(.naturalSize)
    try require(size.width.isFinite && size.height.isFinite && size.width > 0 && size.height > 0
                && size.width <= 4096 && size.height <= 4096,
                "video dimensions must be at most 4096 pixels per side")
    let transform = try await track.load(.preferredTransform)
    try require([transform.a, transform.b, transform.c, transform.d, transform.tx, transform.ty]
                  .allSatisfy { $0.isFinite }, "video transform is invalid")
    let displayed = CGRect(origin: .zero, size: size).applying(transform).integral
    try require(displayed.width > 0 && displayed.height > 0
                && displayed.width <= 4096 && displayed.height <= 4096,
                "displayed video dimensions exceed the supported bounds")

    let ranges: [SamplingRange]
    if let rangeURL = args.rangesURL {
      let rangeSize = try rangeURL.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? Int.max
      try require(rangeSize <= 64 * 1024, "ranges JSON is too large")
      ranges = try JSONDecoder().decode([SamplingRange].self, from: Data(contentsOf: rangeURL))
      try require(!ranges.isEmpty && ranges.count <= 100, "provide between 1 and 100 sampling ranges")
    } else {
      ranges = [SamplingRange(start: 0, end: duration, interval: args.interval)]
    }
    var targets: [Double] = []
    for range in ranges {
      try require(range.start.isFinite && range.end.isFinite && range.interval.isFinite
                  && range.start >= 0 && range.start <= range.end && range.end <= duration
                  && range.interval >= 0.1 && range.interval <= 301,
                  "sampling ranges must lie within the video and use intervals between 0.1 and 301 seconds")
      let steps = Int(floor((range.end - range.start) / range.interval))
      for step in 0...steps { targets.append(range.start + Double(step) * range.interval) }
      targets.append(range.end)
      try require(targets.count <= 10000, "too many requested sampling targets")
    }
    targets = Array(Set(targets)).sorted()

    // An existing result is never reused or overwritten after a failed extraction.
    if manager.fileExists(atPath: args.output.path) {
      let resources = try args.output.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
      try require(resources.isDirectory == true && resources.isSymbolicLink != true,
                  "output must be an ordinary directory")
      try require(try manager.contentsOfDirectory(atPath: args.output.path).isEmpty,
                  "output directory must be empty")
    } else {
      try manager.createDirectory(at: args.output, withIntermediateDirectories: true)
    }
    let staging = args.output.appendingPathComponent(".extract-" + UUID().uuidString, isDirectory: true)
    try manager.createDirectory(at: staging.appendingPathComponent("images", isDirectory: true),
                                withIntermediateDirectories: true)
    defer { try? manager.removeItem(at: staging) }
    let writer = ImageWriter(directory: staging, transform: transform)
    let reader = try AVAssetReader(asset: asset)
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
      kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
    ])
    output.alwaysCopiesSampleData = false
    try require(reader.canAdd(output), "video decoder output is unsupported")
    reader.add(output)
    try require(reader.startReading(), "video decoder could not start")
    defer { if reader.status == .reading { reader.cancelReading() } }
    var allPTS: [FrameTime] = []
    var previous: DecodedFrame?
    var targetIndex = 0
    var count = 0
    while let buffer = output.copyNextSampleBuffer() {
      try autoreleasepool {
        let pts = CMSampleBufferGetPresentationTimeStamp(buffer).seconds
        try require(pts.isFinite && pts >= 0 && pts <= duration + 0.002,
                    "decoded presentation time is invalid")
        if let previous {
          try require(pts > previous.pts, "decoded presentation times must strictly increase")
        }
        guard let pixel = CMSampleBufferGetImageBuffer(buffer) else {
          throw ExtractError.stopped("a decoded sample had no image")
        }
        let pixelWidth = CVPixelBufferGetWidth(pixel)
        let pixelHeight = CVPixelBufferGetHeight(pixel)
        try require(pixelWidth > 0 && pixelHeight > 0 && pixelWidth <= 4096 && pixelHeight <= 4096,
                    "decoded video dimensions exceed the supported bounds")
        count += 1
        try require(count <= 180000, "decoded video has too many frames")
        let current = DecodedFrame(index: count, pts: pts, pixel: pixel)
        allPTS.append(FrameTime(frame_id: current.id, output_pts_seconds: pts))
        while targetIndex < targets.count && targets[targetIndex] <= pts {
          let target = targets[targetIndex]
          if let previous, abs(target - previous.pts) <= abs(pts - target) {
            try writer.write(previous)
          } else {
            try writer.write(current)
          }
          targetIndex += 1
        }
        previous = current
      }
    }
    try require(reader.status == .completed, "video did not decode completely")
    guard let finalFrame = previous, count > 0 else {
      throw ExtractError.stopped("video contains no decoded images")
    }
    // All targets beyond the final PTS map to the final decoded frame, never the target time.
    if targetIndex < targets.count { try writer.write(finalFrame) }
    let frames = writer.sampled.values.sorted { $0.source_frame_index < $1.source_frame_index }
    try require(!frames.isEmpty, "sampling produced no images")
    let manifest = Manifest(duration_seconds: duration, decoded_frames: count,
      width: Int(displayed.width), height: Int(displayed.height), sampling_ranges: ranges,
      frames: frames, all_frame_pts: allPTS)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    let bytes = try encoder.encode(manifest)
    // Only a complete decode is eligible for publication; manifest is the final commit marker.
    try manager.moveItem(at: staging.appendingPathComponent("images"),
                         to: args.output.appendingPathComponent("images"))
    do {
      try bytes.write(to: args.output.appendingPathComponent("manifest.json"), options: .atomic)
    } catch {
      try? manager.removeItem(at: args.output.appendingPathComponent("images"))
      throw error
    }
    print("Decoded \(count) frames; extracted \(frames.count) JPEGs; manifest committed.")
  }
}
