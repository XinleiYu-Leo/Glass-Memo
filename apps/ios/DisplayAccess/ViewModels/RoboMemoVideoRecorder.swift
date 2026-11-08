import AVFoundation
import Foundation
import VideoToolbox
import os

struct RoboMemoVideoClip: Identifiable, Sendable {
  var id: URL { url }
  let url: URL
  let createdAt: Date
  let duration: Double
  let bytes: Int64

  var sensorDirectory: URL? {
    let parent = url.deletingLastPathComponent()
    return url.lastPathComponent == "video.mov" && parent.lastPathComponent.hasPrefix("RoboMemo-Multi-") ? parent : nil
  }

  /// Legacy recap filenames stay unchanged. Each nested video.mov must have a
  /// distinct stable key, otherwise two sessions collide in the memory store.
  var memoryKeyFilename: String {
    sensorDirectory.map { $0.lastPathComponent + ".mov" } ?? url.lastPathComponent
  }

  static func read(_ url: URL) async throws -> Self {
    let asset = AVURLAsset(url: url)
    let duration = try await asset.load(.duration).seconds
    let tracks = try await asset.loadTracks(withMediaType: .video)
    guard duration.isFinite, duration > 0, !tracks.isEmpty,
      try await asset.load(.isPlayable) else {
      throw RoboMemoVideoRecorder.Failure.message("The recorded video could not be opened.")
    }
    let values = try url.resourceValues(forKeys: [.creationDateKey, .fileSizeKey])
    return Self(url: url, createdAt: values.creationDate ?? .now,
                duration: duration, bytes: Int64(values.fileSize ?? 0))
  }

  static func directory() throws -> URL {
    let directory = try FileManager.default.url(
      for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: true
    ).appendingPathComponent("RoboMemoVideos", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    return directory
  }

  static func savedClips() async throws -> [Self] {
    let contents = try FileManager.default.contentsOfDirectory(
      at: directory(), includingPropertiesForKeys: [.creationDateKey, .contentModificationDateKey]
    )
    // A terminated process cannot finalize its movie. Only remove our own
    // abandoned partial files after a day, never a completed/user-exported clip.
    for url in contents where url.lastPathComponent.hasPrefix(".RoboMemo-")
      && url.lastPathComponent.hasSuffix(".recording.mov") {
      if let modified = try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
        modified < Date.now.addingTimeInterval(-86_400) {
        try? FileManager.default.removeItem(at: url)
      }
    }
    var urls = contents.filter { !$0.lastPathComponent.hasPrefix(".") && $0.pathExtension == "mov" }
    // Multi-sensor recordings have their own folder. Do not recursively inspect
    // unrelated user exports or partially written movies.
    urls += contents.filter { $0.lastPathComponent.hasPrefix("RoboMemo-Multi-") }
      .map { $0.appendingPathComponent("video.mov") }
      .filter { FileManager.default.fileExists(atPath: $0.path) }
    var clips: [Self] = []
    for url in urls {
      if let clip = try? await read(url) { clips.append(clip) }
    }
    return clips.sorted { $0.createdAt > $1.createdAt }
  }
}

/// Encodes raw frames directly from the SDK callback to disk;
/// encoder backpressure drops frames instead of retaining an unbounded queue.
/// All writer mutations are locked until finish takes exclusive ownership.
final class RoboMemoVideoRecorder: Sendable {
  enum Failure: LocalizedError {
    case message(String)
    var errorDescription: String? {
      if case .message(let message) = self { return message }
      return nil
    }
  }

  struct Snapshot: Sendable {
    let elapsed: Double
    let frames: Int
    let stopped: Bool
    let error: String?
  }

  // AVFoundation writer objects are not Sendable. They never escape the lock
  // except as an exclusive finalization job after append has been disabled.
  private struct State: @unchecked Sendable {
    var writer: AVAssetWriter?
    var input: AVAssetWriterInput?
    var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    var transferSession: VTPixelTransferSession?
    var start: ContinuousClock.Instant?
    var end: Double?
    var accepting = true
    var finishing = false
    var frames = 0
    var receivedFrames = 0
    var dropped = 0
    var sizeChangeCount = 0
    var normalizedFrames = 0
    var width = 0
    var height = 0
    var pixelFormat: OSType = 0
    var startedAt: Date?
    var lastTime = CMTime.invalid
    var firstSourcePTS = CMTime.invalid
    var lastSourcePTS = CMTime.invalid
    var error: String?
    var streamStarted: ContinuousClock.Instant?
    var firstArrival: ContinuousClock.Instant?
    var firstArrivalUnix: Double?
    var nativeFrameCounts: [String: Int] = [:]
    var resultCounts: [String: Int] = [:]
    var diagnostics: [FrameDiagnostic] = []
    var diagnosticsOmittedRows = 0
    var totalAppendDurationSeconds: Double = 0
    var maximumAppendDurationSeconds: Double = 0
    var maximumLockWaitSeconds: Double = 0
  }

  /// Plain values only: no SDK sample or pixel buffer is retained for diagnostics.
  /// CSV formatting and file IO happen only after recording has stopped.
  private struct FrameDiagnostic: Sendable {
    let frameIndex: Int
    let arrivalElapsedSeconds: Double
    let hostTime: CMTime
    let sourcePTSValue: Int64
    let sourcePTSTimescale: Int32
    let sourcePTSEpoch: Int64
    let sourcePTSFlags: UInt32
    let sourcePTSSeconds: Double?
    var sourceWidth: Int?
    var sourceHeight: Int?
    var sizeMismatch = false
    var wasScaled = false
    var pixelFormatConverted = false
    var result = "error"
    var outputPTSSeconds: Double?
    var outputPTS: CMTime?
    var appendDurationSeconds: Double = 0
    var lockWaitSeconds: Double = 0

    static let csvHeader = "frameIndex,arrivalElapsedSeconds,sourcePTSValue,sourcePTSTimescale,sourcePTSEpoch,sourcePTSFlags,sourcePTSSeconds,sourceWidth,sourceHeight,sizeMismatch,wasScaled,pixelFormatConverted,result,outputPTSSeconds,hostTimeValue,hostTimeTimescale,hostTimeEpoch,hostTimeFlags,hostTimeSeconds,outputPTSValue,outputPTSTimescale,outputPTSEpoch,outputPTSFlags,appendDurationSeconds,lockWaitSeconds\n"

    var csvLine: String {
      // String(Double) is locale-independent, so decimal commas cannot corrupt CSV.
      var fields: [String] = [String(frameIndex), String(arrivalElapsedSeconds)]
      fields.append(contentsOf: [String(sourcePTSValue), String(sourcePTSTimescale),
                                 String(sourcePTSEpoch), String(sourcePTSFlags)])
      fields.append(sourcePTSSeconds.map { String($0) } ?? "")
      fields.append(sourceWidth.map { String($0) } ?? "")
      fields.append(sourceHeight.map { String($0) } ?? "")
      fields.append(contentsOf: [String(sizeMismatch), String(wasScaled), String(pixelFormatConverted), result])
      fields.append(outputPTSSeconds.map { String($0) } ?? "")
      fields.append(contentsOf: RoboMemoHostClock.csvFields(hostTime))
      fields.append(contentsOf: outputPTS.map {
        [String($0.value), String($0.timescale), String($0.epoch), String($0.flags.rawValue)]
      } ?? ["", "", "", ""])
      fields.append(contentsOf: [String(appendDurationSeconds), String(lockWaitSeconds)])
      return fields.joined(separator: ",") + "\n"
    }
  }

  private struct RecordingMetadata: Encodable, Sendable {
    struct Resolution: Encodable, Sendable {
      let width: Int
      let height: Int
    }

    struct Timestamp: Encodable, Sendable {
      let value: Int64
      let timescale: Int32
      let epoch: Int64
      let flags: UInt32
      let seconds: Double

      init(_ time: CMTime) {
        value = time.value
        timescale = time.timescale
        epoch = time.epoch
        flags = time.flags.rawValue
        seconds = time.seconds
      }
    }

    let schemaVersion: Int
    let timelineBasis: String
    let sourcePTSOrigin: Timestamp?
    let wallDurationSeconds: Double
    let presentationEndSeconds: Double
    let maximumDurationSeconds: Double
    let durationLimitPolicy: String
    let sourceTimestampPolicy: String
    let maximumSourceTimelineLeadSeconds: Double?
    let totalAppendDurationSeconds: Double
    let maximumAppendDurationSeconds: Double
    let maximumLockWaitSeconds: Double
    let requestedResolution: String
    let outputResolution: Resolution?
    let frameRate: UInt // Requested stream/encoder rate, not measured delivery rate.
    let receivedFrames: Int // Accepted append calls, including dropped/error/limit rows.
    let writtenFrames: Int
    let dropped: Int
    let sizeChangeCount: Int // Arriving frames whose dimensions differ from the fixed writer size.
    let normalizedFrames: Int // Successfully written frames converted to the writer's format/size.
    let startedAt: Date? // First successfully written frame, not first arrival.
    let firstArrivalUnix: Double?
    let streamStartToFirstFrameSeconds: Double?
    let nativeFrameCounts: [String: Int] // All valid image arrivals, including drops.
    let resultCounts: [String: Int]
    var recordingStatus = "failed"
    var recordingError: String?
    var diagnosticsStatus = "complete"
    var diagnosticsRows = 0 // Rows actually saved, excluding the CSV header.
    let diagnosticsBufferedRows: Int
    let diagnosticsOmittedRows: Int
    var diagnosticsError: String?
  }

  private struct FinishJob: @unchecked Sendable {
    let writer: AVAssetWriter?
    let input: AVAssetWriterInput?
    let end: CMTime
    let error: String?
    let metadata: RecordingMetadata
    let diagnostics: [FrameDiagnostic]
  }

  static let defaultMaximumDuration: Double = 300
  // Defensive application tolerance, not an SDK clock/delivery guarantee.
  // Covers startup catch-up without permitting a corrupted PTS to create an
  // hours-long movie. Rejected rows preserve the original timestamp for review.
  private static let maximumSourceTimelineLeadSeconds: Double = 15
  private static let logger = Logger(subsystem: "RoboMemo", category: "VideoRecorder")

  let maximumDuration: Double
  private let frameRate: UInt
  private let requestedResolution: String
  private let usesSourceTimeline: Bool
  private let diagnosticRowLimit: Int
  private let temporaryURL: URL
  private let finalURL: URL
  private let diagnosticsURL: URL
  private let metadataSink: (@Sendable (Data) -> Void)?
  private let state: OSAllocatedUnfairLock<State>

  init(directory: URL, requestedResolution: String, frameRate: UInt,
       maximumDuration: Double = RoboMemoVideoRecorder.defaultMaximumDuration,
       diagnosticRowLimit: Int = 10_000, sessionLayout: Bool = false,
       metadataSink: (@Sendable (Data) -> Void)? = nil) {
    self.maximumDuration = maximumDuration
    self.frameRate = frameRate
    self.requestedResolution = requestedResolution
    usesSourceTimeline = sessionLayout
    self.diagnosticRowLimit = max(0, min(10_000, diagnosticRowLimit))
    var initialState = State()
    initialState.diagnostics.reserveCapacity(self.diagnosticRowLimit)
    state = OSAllocatedUnfairLock(initialState: initialState)
    let name = "RoboMemo-\(UUID().uuidString)"
    temporaryURL = directory.appendingPathComponent(".\(name).recording.mov")
    finalURL = directory.appendingPathComponent(sessionLayout ? "video.mov" : "\(name).mov")
    diagnosticsURL = directory.appendingPathComponent(sessionLayout ? "frames.csv" : "\(name)_frames.csv")
    self.metadataSink = metadataSink
  }

  deinit {
    state.withLock {
      if !$0.finishing { $0.writer?.cancelWriting() }
      if let session = $0.transferSession { VTPixelTransferSessionInvalidate(session) }
      $0.transferSession = nil
    }
    try? FileManager.default.removeItem(at: temporaryURL)
  }

  private func elapsed(_ value: State, at now: ContinuousClock.Instant) -> Double {
    guard let start = value.start else { return 0 }
    return min(maximumDuration, max(0, seconds(from: start, to: now)))
  }

  private func seconds(from start: ContinuousClock.Instant, to end: ContinuousClock.Instant) -> Double {
    let duration = start.duration(to: end).components
    return Double(duration.seconds) + Double(duration.attoseconds) / 1e18
  }

  /// Called immediately before Stream.start(), after permissions/session setup.
  func markStreamStart() {
    state.withLock {
      guard $0.accepting, !$0.finishing, $0.streamStarted == nil else { return }
      $0.streamStarted = .now
    }
  }

  var snapshot: Snapshot {
    state.withLock {
      Snapshot(elapsed: $0.end ?? elapsed($0, at: .now), frames: $0.frames,
               stopped: !$0.accepting, error: $0.error)
    }
  }

  /// Starts the recording clock only after the first frame is successfully written.
  func append(_ sampleBuffer: CMSampleBuffer, hostTime: CMTime = RoboMemoHostClock.now()) {
    let appendEnteredAt = ContinuousClock.now
    // This synchronous critical section borrows a read-only SDK frame; it does
    // not send the non-Sendable CMSampleBuffer to another thread or retain it.
    state.withLockUnchecked { value in
      guard value.accepting, !value.finishing, value.error == nil else { return }
      let now = ContinuousClock.now
      let arrivedAt = Date.now
      let time = elapsed(value, at: now)
      if value.firstArrival == nil {
        value.firstArrival = now
        value.firstArrivalUnix = arrivedAt.timeIntervalSince1970
      }
      value.receivedFrames += 1
      let sourcePTS = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
      let sourceSeconds = sourcePTS.seconds
      var diagnostic = FrameDiagnostic(
        frameIndex: value.receivedFrames,
        arrivalElapsedSeconds: seconds(from: value.firstArrival ?? now, to: now),
        hostTime: hostTime,
        sourcePTSValue: sourcePTS.value, sourcePTSTimescale: sourcePTS.timescale,
        sourcePTSEpoch: sourcePTS.epoch, sourcePTSFlags: sourcePTS.flags.rawValue,
        sourcePTSSeconds: sourcePTS.isValid && sourceSeconds.isFinite ? sourceSeconds : nil)
      diagnostic.lockWaitSeconds = seconds(from: appendEnteredAt, to: now)
      let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer)
      if let imageBuffer {
        let width = CVPixelBufferGetWidth(imageBuffer), height = CVPixelBufferGetHeight(imageBuffer)
        diagnostic.sourceWidth = width
        diagnostic.sourceHeight = height
        diagnostic.sizeMismatch = value.width > 0 && (width != value.width || height != value.height)
        if diagnostic.sizeMismatch { value.sizeChangeCount += 1 }
        value.nativeFrameCounts["\(width)x\(height)", default: 0] += 1
      }
      defer {
        diagnostic.appendDurationSeconds = seconds(from: appendEnteredAt, to: .now)
        value.totalAppendDurationSeconds += diagnostic.appendDurationSeconds
        value.maximumAppendDurationSeconds = max(value.maximumAppendDurationSeconds, diagnostic.appendDurationSeconds)
        value.maximumLockWaitSeconds = max(value.maximumLockWaitSeconds, diagnostic.lockWaitSeconds)
        value.resultCounts[diagnostic.result, default: 0] += 1
        if value.diagnostics.count < diagnosticRowLimit {
          value.diagnostics.append(diagnostic)
        } else {
          value.diagnosticsOmittedRows += 1
        }
      }
      if time >= maximumDuration {
        diagnostic.result = "limit_reached"
        value.end = maximumDuration
        value.accepting = false
        return
      }
      // Multi-sensor playback follows capture timestamps, not delivery bursts.
      // Never repair a reset/invalid SDK timestamp with a different clock: keep
      // the already-recorded movie and leave an explicit rejected-row trace.
      let timestamp: CMTime
      if usesSourceTimeline {
        guard sourcePTS.isNumeric, sourcePTS.timescale > 0, sourceSeconds.isFinite else {
          value.dropped += 1
          diagnostic.result = "dropped_invalid_source_pts"
          return
        }
        if value.lastSourcePTS.isValid {
          guard sourcePTS.epoch == value.lastSourcePTS.epoch else {
            value.dropped += 1
            diagnostic.result = "dropped_source_epoch_change"
            return
          }
          guard CMTimeCompare(sourcePTS, value.lastSourcePTS) > 0 else {
            value.dropped += 1
            diagnostic.result = "dropped_nonmonotonic_source_pts"
            return
          }
        }
        timestamp = value.firstSourcePTS.isValid ? CMTimeSubtract(sourcePTS, value.firstSourcePTS) : .zero
        guard timestamp.isNumeric, timestamp.seconds.isFinite, timestamp >= .zero else {
          value.dropped += 1
          diagnostic.result = "dropped_invalid_source_pts"
          return
        }
        guard timestamp.seconds <= time + Self.maximumSourceTimelineLeadSeconds else {
          value.dropped += 1
          diagnostic.result = "dropped_source_pts_ahead_of_wall"
          return
        }
        if timestamp.seconds >= maximumDuration {
          diagnostic.result = "source_limit_reached"
          value.end = time
          value.accepting = false
          return
        }
      } else {
        // Ordinary recording retains its existing host-arrival time axis.
        timestamp = CMTime(seconds: time, preferredTimescale: 60_000)
      }
      do {
        guard let pixelBuffer = imageBuffer else {
          throw Failure.message("The camera did not provide a raw video frame. Please restart recording.")
        }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        if value.writer == nil {
          let writer = try AVAssetWriter(outputURL: temporaryURL, fileType: .mov)
          let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width, AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
              AVVideoAverageBitRateKey: max(2_000_000, width * height * 6),
              AVVideoExpectedSourceFrameRateKey: Int(frameRate),
              AVVideoMaxKeyFrameIntervalKey: Int(frameRate),
              AVVideoAllowFrameReorderingKey: false,
            ],
          ])
          input.expectsMediaDataInRealTime = true
          if usesSourceTimeline { input.mediaTimeScale = 1_000_000 }
          let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [
              kCVPixelBufferPixelFormatTypeKey as String: CVPixelBufferGetPixelFormatType(pixelBuffer),
              kCVPixelBufferWidthKey as String: width,
              kCVPixelBufferHeightKey as String: height,
              kCVPixelBufferIOSurfacePropertiesKey as String: [:],
            ])
          guard writer.canAdd(input) else { throw Failure.message("Video encoding is unavailable.") }
          writer.add(input)
          value.writer = writer
          value.input = input
          value.adaptor = adaptor
          value.width = width
          value.height = height
          value.pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)
          guard writer.startWriting() else {
            throw writer.error ?? Failure.message("Could not start saving the video.")
          }
          writer.startSession(atSourceTime: .zero)
        }
        guard let writer = value.writer, let input = value.input, let adaptor = value.adaptor else { return }
        guard writer.status == .writing else {
          throw writer.error ?? Failure.message("Video recording stopped unexpectedly.")
        }
        let sizeChanged = width != value.width || height != value.height
        diagnostic.sizeMismatch = sizeChanged
        let formatChanged = CVPixelBufferGetPixelFormatType(pixelBuffer) != value.pixelFormat
        let needsNormalization = sizeChanged || formatChanged
        guard input.isReadyForMoreMediaData else {
          value.dropped += 1
          diagnostic.result = "dropped_busy"
          return
        }
        guard !value.lastTime.isValid || timestamp > value.lastTime else {
          if usesSourceTimeline { value.dropped += 1 }
          diagnostic.result = "dropped_nonmonotonic"
          return
        }
        let outputBuffer: CVPixelBuffer
        if needsNormalization {
          guard let normalized = try normalize(pixelBuffer, adaptor: adaptor, state: &value) else {
            value.dropped += 1
            diagnostic.result = "dropped_pool"
            return
          }
          outputBuffer = normalized
          // Letterboxing may only add borders without resizing the source.
          let scale = min(Double(value.width) / Double(width), Double(value.height) / Double(height))
          diagnostic.wasScaled = sizeChanged && abs(scale - 1) > 1e-9
          diagnostic.pixelFormatConverted = formatChanged
        } else {
          outputBuffer = pixelBuffer
        }
        guard adaptor.append(outputBuffer, withPresentationTime: timestamp) else {
          throw writer.error ?? Failure.message("Could not write the camera frame.")
        }
        if value.start == nil {
          value.start = now
          value.startedAt = arrivedAt
        }
        value.lastTime = timestamp
        if usesSourceTimeline {
          if !value.firstSourcePTS.isValid { value.firstSourcePTS = sourcePTS }
          value.lastSourcePTS = sourcePTS
        }
        value.frames += 1
        diagnostic.result = "written"
        diagnostic.outputPTSSeconds = timestamp.seconds
        diagnostic.outputPTS = timestamp
        if needsNormalization { value.normalizedFrames += 1 }
      } catch {
        value.error = error.localizedDescription
        value.end = time
        value.accepting = false
      }
    }
  }

  /// The glasses may lower resolution mid-stream. Keep the movie's dimensions
  /// fixed, but preserve new content by converting those frames before writing.
  /// Called only under the existing writer lock; no additional queue or task.
  private func normalize(
    _ source: CVPixelBuffer, adaptor: AVAssetWriterInputPixelBufferAdaptor, state value: inout State
  ) throws -> CVPixelBuffer? {
    if value.transferSession == nil {
      var session: VTPixelTransferSession?
      let status = VTPixelTransferSessionCreate(allocator: kCFAllocatorDefault, pixelTransferSessionOut: &session)
      guard status == noErr, let session else {
        throw Failure.message("Could not prepare video size conversion (\(status)).")
      }
      let scalingStatus = VTSessionSetProperty(session, key: kVTPixelTransferPropertyKey_ScalingMode,
                                             value: kVTScalingMode_Letterbox)
      guard scalingStatus == noErr else {
        VTPixelTransferSessionInvalidate(session)
        throw Failure.message("Could not configure video size conversion (\(scalingStatus)).")
      }
      value.transferSession = session
    }
    guard let session = value.transferSession, let pool = adaptor.pixelBufferPool else {
      throw Failure.message("The video conversion buffer is unavailable.")
    }
    // A separate pool buffer is required: the writer retains each submitted
    // image asynchronously. Cap allocations so backpressure stays bounded.
    var destination: CVPixelBuffer?
    let status = CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(
      kCFAllocatorDefault, pool, [kCVPixelBufferPoolAllocationThresholdKey: 8] as CFDictionary, &destination
    )
    if status == kCVReturnWouldExceedAllocationThreshold { return nil }
    guard status == kCVReturnSuccess, let destination else {
      throw Failure.message("Could not allocate a video conversion buffer (\(status)).")
    }
    let transferStatus = VTPixelTransferSessionTransferImage(session, from: source, to: destination)
    guard transferStatus == noErr else {
      throw Failure.message("Could not adapt the changed camera image size (\(transferStatus)).")
    }
    return destination
  }

  /// Freeze the ending before camera teardown so closing time is not recorded.
  func requestStop() {
    state.withLock {
      if $0.accepting {
        $0.end = elapsed($0, at: .now)
        $0.accepting = false
      }
      if let session = $0.transferSession { VTPixelTransferSessionInvalidate(session) }
      $0.transferSession = nil
    }
  }

  /// Call once, after the frame consumer has stopped. Cancellation of the UI
  /// task must not interrupt finalizing the file's index and moving it into place.
  func finish() async throws -> RoboMemoVideoClip {
    requestStop()
    let job: FinishJob = try state.withLock { value in
      guard !value.finishing else { throw Failure.message("The video is already being saved.") }
      value.finishing = true
      let resolution: RecordingMetadata.Resolution? = value.width > 0 && value.height > 0
        ? .init(width: value.width, height: value.height) : nil
      let startupDelay: Double?
      if let start = value.streamStarted, let first = value.firstArrival {
        startupDelay = seconds(from: start, to: first)
      } else { startupDelay = nil }
      let movieEnd: CMTime
      if usesSourceTimeline, value.lastTime.isNumeric {
        // The final sample duration is nominal; SDK frame PTS supplies starts,
        // not a guaranteed exposure/end time. Host stop delay must not stretch
        // the movie or truncate late-arriving source timestamps.
        let nominalEnd = CMTimeAdd(value.lastTime,
          CMTime(seconds: 1 / Double(max(1, frameRate)), preferredTimescale: 1_000_000))
        movieEnd = CMTimeMinimum(nominalEnd,
          CMTime(seconds: maximumDuration, preferredTimescale: 1_000_000))
      } else {
        movieEnd = CMTime(seconds: max(1.0 / 24, value.end ?? 0), preferredTimescale: 60_000)
      }
      return FinishJob(writer: value.writer, input: value.input,
                       end: movieEnd, error: value.error,
                       metadata: RecordingMetadata(
                         schemaVersion: usesSourceTimeline ? 3 : 2,
                         timelineBasis: usesSourceTimeline ? "sdk_video_pts_relative_to_first_written_frame" : "host_arrival_elapsed",
                         sourcePTSOrigin: usesSourceTimeline && value.firstSourcePTS.isNumeric
                           ? .init(value.firstSourcePTS) : nil,
                         wallDurationSeconds: value.end ?? 0,
                         presentationEndSeconds: movieEnd.seconds,
                         maximumDurationSeconds: maximumDuration,
                         durationLimitPolicy: usesSourceTimeline
                           ? "Stop at either wall elapsed or valid source elapsed limit. Clamp nominal final-frame end to the same limit. Reject implausible forward PTS before applying source limit."
                           : "Stop at wall elapsed limit.",
                         sourceTimestampPolicy: usesSourceTimeline
                           ? "Reject invalid, duplicate, decreasing, changed-epoch, or source elapsed more than 15 seconds ahead of wall elapsed SDK PTS; do not substitute host time. Final sample uses requested frame interval."
                           : "SDK PTS is diagnostic only; playback follows host arrival elapsed time.",
                         maximumSourceTimelineLeadSeconds: usesSourceTimeline ? Self.maximumSourceTimelineLeadSeconds : nil,
                         totalAppendDurationSeconds: value.totalAppendDurationSeconds,
                         maximumAppendDurationSeconds: value.maximumAppendDurationSeconds,
                         maximumLockWaitSeconds: value.maximumLockWaitSeconds,
                         requestedResolution: requestedResolution, outputResolution: resolution,
                         frameRate: frameRate, receivedFrames: value.receivedFrames, writtenFrames: value.frames,
                         dropped: value.dropped, sizeChangeCount: value.sizeChangeCount,
                         normalizedFrames: value.normalizedFrames,
                         startedAt: value.startedAt, firstArrivalUnix: value.firstArrivalUnix,
                         streamStartToFirstFrameSeconds: startupDelay,
                         nativeFrameCounts: value.nativeFrameCounts, resultCounts: value.resultCounts,
                         diagnosticsBufferedRows: value.diagnostics.count,
                         diagnosticsOmittedRows: value.diagnosticsOmittedRows),
                       diagnostics: value.diagnostics)
    }
    do {
      if let error = job.error { throw Failure.message(error) }
      guard let writer = job.writer, let input = job.input, job.metadata.writtenFrames > 0 else {
        throw Failure.message("No video frames were received. No recording was saved.")
      }
      guard writer.status == .writing else {
        throw writer.error ?? Failure.message("The video could not be saved.")
      }
      writer.endSession(atSourceTime: job.end)
      input.markAsFinished()
      await writer.finishWriting()
      guard writer.status == .completed else {
        throw writer.error ?? Failure.message("The video could not be saved. Check available storage.")
      }
      // Validate before publishing: incomplete files never appear in Saved videos.
      _ = try await RoboMemoVideoClip.read(temporaryURL)
      try FileManager.default.moveItem(at: temporaryURL, to: finalURL)
      let clip = try await RoboMemoVideoClip.read(finalURL)
      saveDiagnostics(job, recordingError: nil)
      return clip
    } catch {
      job.writer?.cancelWriting()
      try? FileManager.default.removeItem(at: temporaryURL)
      // Error/no-frame recordings still leave best-effort diagnostics, but no
      // playable MOV is published. Abrupt process termination cannot reach here.
      saveDiagnostics(job, recordingError: error.localizedDescription)
      throw error
    }
  }

  private func saveDiagnostics(_ job: FinishJob, recordingError: String?) {
    var metadata = job.metadata
    metadata.recordingStatus = recordingError == nil ? "completed" : "failed"
    metadata.recordingError = recordingError
    metadata.diagnosticsStatus = metadata.diagnosticsOmittedRows == 0 ? "complete" : "truncated"
    let baseURL = finalURL.deletingPathExtension()
    do {
      var csv = FrameDiagnostic.csvHeader
      csv.reserveCapacity(job.diagnostics.count * 180)
      for record in job.diagnostics { csv += record.csvLine }
      try Data(csv.utf8).write(to: diagnosticsURL, options: .atomic)
      metadata.diagnosticsRows = job.diagnostics.count
    } catch {
      metadata.diagnosticsStatus = "writeFailed"
      metadata.diagnosticsError = error.localizedDescription
    }
    do {
      let encoder = JSONEncoder()
      encoder.dateEncodingStrategy = .iso8601
      encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
      let data = try encoder.encode(metadata)
      if let metadataSink {
        metadataSink(data)
      } else {
        try data.write(to: baseURL.appendingPathExtension("json"), options: .atomic)
      }
    } catch {
      // Missing sidecars must be treated as incomplete diagnostics by consumers.
      // Neither a CSV nor a JSON write failure invalidates a successfully saved movie.
      Self.logger.error("Recording diagnostics could not be saved: \(error.localizedDescription, privacy: .public)")
    }
  }
}
