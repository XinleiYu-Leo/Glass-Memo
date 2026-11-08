import AVFoundation
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct RoboMemoVideoFrame: Sendable {
  let timestampSeconds: Double
  let jpegData: Data
}

/// A small, ordered set of stills, not the movie itself. Extraction never uploads
/// media and only holds one decoded frame at a time alongside bounded JPEG data.
struct RoboMemoVideoFrames: Sendable {
  static let samplingVersion = "uniform-with-start-5m-v3"

  let duration: Double
  let frames: [RoboMemoVideoFrame]

  enum Failure: LocalizedError {
    case message(String)
    var errorDescription: String? {
      if case .message(let message) = self { return message }
      return nil
    }
  }

  private static let maximumFrameBytes = 256 * 1_024
  private static let maximumTotalBytes = 12 * 1_024 * 1_024

  /// Runs on the concurrent executor even when called from a UI actor. Requests
  /// are sequential, cancellation stops the decoder, and no full movie is loaded.
  @concurrent
  static func extract(from url: URL) async throws -> Self {
    try Task.checkCancellation()
    guard url.isFileURL else {
      throw Failure.message("Choose a video saved on this phone.")
    }
    let values: URLResourceValues
    do {
      values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey, .isReadableKey])
    } catch {
      throw Failure.message("This saved video is missing or cannot be read.")
    }
    guard values.isRegularFile == true, values.isReadable == true,
      let size = values.fileSize, size > 0 else {
      throw Failure.message("This saved video is missing, empty, or cannot be read.")
    }
    guard size <= 128 * 1_024 * 1_024 else {
      throw Failure.message("This video is too large to analyze. Use a recording up to 5 minutes and 128 MiB.")
    }

    let asset = AVURLAsset(url: url)
    let duration: Double
    do {
      duration = try await asset.load(.duration).seconds
      guard duration.isFinite, duration > 0, duration <= 301 else {
        throw Failure.message("Video analysis supports recordings up to 5 minutes long.")
      }
      guard let track = try await asset.loadTracks(withMediaType: .video).first,
        try await asset.load(.isPlayable) else {
        throw Failure.message("This file does not contain a playable video.")
      }
      let dimensions = try await track.load(.naturalSize)
      guard dimensions.width.isFinite, dimensions.height.isFinite,
        dimensions.width > 0, dimensions.height > 0,
        max(dimensions.width, dimensions.height) <= 4_096 else {
        throw Failure.message("This video's dimensions are unsupported. Use a glasses recording.")
      }
    } catch {
      try Task.checkCancellation()
      if let failure = error as? Failure { throw failure }
      throw Failure.message("This saved video could not be opened. Try playing it first.")
    }
    try Task.checkCancellation()

    let generator = FrameGenerator(asset: asset)

    return try await withTaskCancellationHandler {
      defer { generator.cancel() }
      let count = min(150, max(1, Int(ceil(duration / 2))))
      // Keep the original short-clip density and a fixed total upload budget.
      // Five minutes yields 150 samples, approximately two seconds apart.
      let frameByteBudget = min(maximumFrameBytes, maximumTotalBytes / count)
      // Include the opening moment while retaining coverage through the former
      // final midpoint. Never request the exact end of the movie.
      let lastSampleSeconds = duration * (Double(count) - 0.5) / Double(count)
      var frames: [RoboMemoVideoFrame] = []
      frames.reserveCapacity(count)
      var bytes = 0
      for index in 0..<count {
        try Task.checkCancellation()
        let seconds = count == 1 ? 0 : lastSampleSeconds * Double(index) / Double(count - 1)
        let time = CMTime(seconds: seconds, preferredTimescale: 60_000)
        do {
          let result = try await generator.image(at: time)
          try Task.checkCancellation()
          let actualSeconds = result.actualTime.seconds
          guard actualSeconds.isFinite, actualSeconds >= 0, actualSeconds <= duration,
            frames.last.map({ actualSeconds > $0.timestampSeconds }) ?? true else {
            throw Failure.message("The video returned invalid or non-increasing frame timestamps. Try a new recording.")
          }
          let jpeg = try autoreleasepool { try encodeJPEG(result.image, maximumBytes: frameByteBudget) }
          bytes += jpeg.count
          guard bytes <= maximumTotalBytes else {
            throw Failure.message("The video preview is too large to analyze.")
          }
          frames.append(RoboMemoVideoFrame(timestampSeconds: actualSeconds, jpegData: jpeg))
        } catch {
          try Task.checkCancellation()
          if let failure = error as? Failure { throw failure }
          throw Failure.message("A video frame could not be read. Try playing the video or recording again.")
        }
      }
      try Task.checkCancellation()
      return Self(duration: duration, frames: frames)
    } onCancel: {
      generator.cancel()
    }
  }

  /// AVAssetImageGenerator is not Sendable. Configuration happens once in init;
  /// the lock serializes request submission with cancellation, closing the race
  /// where a cancelled task could submit a new request after cancelAll was called.
  /// Callbacks only resume their continuation and never access mutable state.
  private final class FrameGenerator: @unchecked Sendable {
    struct Image: Sendable {
      let image: CGImage
      let actualTime: CMTime
    }

    private let generator: AVAssetImageGenerator
    private let lock = NSLock()
    private var cancelled = false

    init(asset: AVAsset) {
      generator = AVAssetImageGenerator(asset: asset)
      generator.appliesPreferredTrackTransform = true
      generator.maximumSize = CGSize(width: 640, height: 640)
      // Bounded tolerance avoids keyframe-only sampling while allowing times
      // between encoded frames. Sampling stops before the movie's end time.
      generator.requestedTimeToleranceBefore = CMTime(seconds: 0.15, preferredTimescale: 600)
      generator.requestedTimeToleranceAfter = CMTime(seconds: 0.15, preferredTimescale: 600)
    }

    func image(at time: CMTime) async throws -> Image {
      try await withCheckedThrowingContinuation { continuation in
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled else {
          continuation.resume(throwing: CancellationError())
          return
        }
        generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: time)]) {
          _, image, actualTime, result, error in
          if result == .succeeded, let image {
            continuation.resume(returning: Image(image: image, actualTime: actualTime))
          } else if result == .cancelled {
            continuation.resume(throwing: CancellationError())
          } else {
            continuation.resume(throwing: error ?? Failure.message("A video frame could not be read."))
          }
        }
      }
    }

    func cancel() {
      lock.lock()
      defer { lock.unlock() }
      guard !cancelled else { return }
      cancelled = true
      generator.cancelAllCGImageGeneration()
    }
  }

  private static func encodeJPEG(_ image: CGImage, maximumBytes: Int) throws -> Data {
    guard image.width > 0, image.height > 0, max(image.width, image.height) <= 640 else {
      throw Failure.message("The video frame could not be resized safely.")
    }
    for quality in [0.72, 0.5, 0.3, 0.15] {
      let data = NSMutableData()
      guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else {
        throw Failure.message("The video frame could not be prepared for analysis.")
      }
      CGImageDestinationAddImage(destination, image, [
        kCGImageDestinationLossyCompressionQuality: quality,
      ] as CFDictionary)
      guard CGImageDestinationFinalize(destination) else {
        throw Failure.message("The video frame could not be prepared for analysis.")
      }
      if data.length <= maximumBytes { return data as Data }
    }
    throw Failure.message("A video frame is too large to analyze. Try another recording.")
  }
}
