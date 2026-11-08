import Foundation
import MWDATCamera
import MWDATCore
import MWDATDisplay
import Observation
import os
import UIKit

/// Serializes photos, local live preview, and explicitly requested silent videos.
/// A bounded preview mailbox keeps memory independent of recording duration.
@Observable
@MainActor
final class RoboMemoGlassesCameraViewModel {
  enum PreviewState {
    case idle, preparing, live, paused, stopping
  }

  enum RecordingPreset: String, CaseIterable, Identifiable {
    case high15, high7, medium15

    var id: String { rawValue }

    var label: String {
      switch self {
      case .high15: "High @15"
      case .high7: "High @7"
      case .medium15: "Medium @15"
      }
    }

    var resolution: StreamingResolution {
      self == .medium15 ? .medium : .high
    }

    var requestedResolution: String { self == .medium15 ? "medium" : "high" }
    var frameRate: UInt { self == .high7 ? 7 : 15 }
  }

  private(set) var selectedRecordingPreset: RecordingPreset = .high15
  // Opt-in for this launch only. Switching it off restores the legacy preset.
  private(set) var multiSensorEnabled = false
  private(set) var sensorAudioEnabled = true
  private(set) var motionRateHz = 60
  private(set) var syncTestEnabled = false
  private(set) var syncTestInProgress = false
  private(set) var lastSensorFolder: URL?
  private(set) var sensorStatusMessage: String?

  private(set) var previewState: PreviewState = .idle
  private(set) var previewImage: UIImage?
  var isPreviewing: Bool { previewState != .idle }
  var isBusy: Bool { isCapturing || isPreviewing || isSavingVideo }
  private(set) var isRecording = false
  private(set) var isSavingVideo = false
  private(set) var recordingSeconds = 0
  private(set) var recordedClips: [RoboMemoVideoClip] = []
  private(set) var videoLibraryError: String?
  private(set) var isCapturing = false
  private(set) var statusMessage: String?
  private(set) var errorMessage: String?
  private(set) var requiresDATAppUpdate = false

  @ObservationIgnored private let wearables: WearablesInterface
  @ObservationIgnored private var previewTask: Task<Void, Never>?
  @ObservationIgnored private var frameTask: Task<Void, Never>?
  @ObservationIgnored private var frameContinuation: AsyncStream<VideoFrame>.Continuation?
  @ObservationIgnored private var lastFrameAt: ContinuousClock.Instant?
  @ObservationIgnored private var previewFrameCount = 0
  @ObservationIgnored private var videoRecorder: RoboMemoVideoRecorder?
  @ObservationIgnored private var sensorController: RoboMemoMultiSensorController?
  @ObservationIgnored private var liveStreamTokens: [any AnyListenerToken] = []
  @ObservationIgnored private var liveStreamID = UUID()
  @ObservationIgnored private var syncTestStarted = false
  @ObservationIgnored private var sensorCaptureStopped = false
  @ObservationIgnored private var videoBackgroundTask: UIBackgroundTaskIdentifier = .invalid
  @ObservationIgnored private var isRequestingPermission = false
  @ObservationIgnored private var captureTask: Task<Data?, Never>?
  @ObservationIgnored private var delayedTeardownTask: Task<Void, Never>?
  @ObservationIgnored private var attemptID: UUID?
  @ObservationIgnored private var session: DeviceSession?
  @ObservationIgnored private var camera: Camera?
  @ObservationIgnored private var tokens: [any AnyListenerToken] = []
  @ObservationIgnored private var photoState: PhotoState = .stopped
  @ObservationIgnored private var pendingPhoto: Data?
  @ObservationIgnored private var pendingFailure: (any Error)?
  @ObservationIgnored private var acceptingEvents = false
  @ObservationIgnored private var sessionReachedStarted = false
  @ObservationIgnored private var isBootstrappingStream = false
  @ObservationIgnored private var captureStage = "Preparing camera"
  @ObservationIgnored private var attemptStartedAt = ContinuousClock.now
  @ObservationIgnored private var lastProgressBucket = -1
  @ObservationIgnored private let logger = Logger(
    subsystem: "org.example.robomemo", category: "RoboMemoCamera"
  )

  init(wearables: WearablesInterface = Wearables.shared) {
    self.wearables = wearables
    Task { [weak self] in
      do {
        let clips = try await Task.detached { try await RoboMemoVideoClip.savedClips() }.value
        // A new recording may have finished while the library was being read.
        guard let self else { return }
        let existing = Set(self.recordedClips.map(\.id))
        self.recordedClips.append(contentsOf: clips.filter { !existing.contains($0.id) })
        self.recordedClips.sort { $0.createdAt > $1.createdAt }
      } catch {
        self?.videoLibraryError = "Could not load saved videos: \(error.localizedDescription)"
      }
    }
  }

  isolated deinit {
    captureTask?.cancel()
    previewTask?.cancel()
    frameContinuation?.finish()
    frameTask?.cancel()
    delayedTeardownTask?.cancel()
    camera?.photo.stop()
    camera?.stop()
    session?.stop()
  }

  /// The caller closes Developer preview first, so this selects physical glasses.
  /// Keeping the capture task here makes Cancel work during SDK permission redirects too.
  func capturePhoto(
    stopDisplaySession: @escaping @MainActor () async throws -> Void
  ) async -> Data? {
    guard !isBusy else { return nil }
    guard session == nil else {
      errorMessage = "The previous camera session is still closing. Keep your glasses nearby and try again."
      return nil
    }
    let id = UUID()
    attemptID = id
    attemptStartedAt = .now
    isCapturing = true
    errorMessage = nil
    requiresDATAppUpdate = false
    lastProgressBucket = -1
    photoState = .stopped
    setStage("Preparing camera")
    statusMessage = "Preparing glasses camera…"

    let task = Task { [weak self] in
      await self?.runCapture(id: id, stopDisplaySession: stopDisplaySession)
    }
    captureTask = task
    let result = await withTaskCancellationHandler {
      await task.value
    } onCancel: {
      task.cancel()
    }
    if attemptID == id { captureTask = nil }
    return Task.isCancelled ? nil : result
  }

  func cancel() {
    guard isBusy else { return }
    videoRecorder?.requestStop()
    stopSensorCapture(reason: "cancel_or_lifecycle")
    if isPreviewing { previewState = .stopping }
    previewImage = nil
    frameContinuation?.finish()
    frameTask?.cancel()
    previewTask?.cancel()
    trace("Cancellation requested")
    statusMessage = "Closing glasses camera…"
    captureTask?.cancel()
    // A timed-out teardown retains the handle until the SDK reports stopped.
    if captureTask == nil && previewTask == nil { session?.stop() }
  }

  private func runCapture(
    id: UUID,
    stopDisplaySession: @escaping @MainActor () async throws -> Void
  ) async -> Data? {
    var result: Data?
    var selectedDevice: Device?
    do {
      let (device, newSession) = try await prepareSession(id: id, stopDisplaySession: stopDisplaySession)
      selectedDevice = device

      // Follow BirdSpotter's cold-sensor startup: the short compressed stream
      // wakes Camera, then Photo becomes ready and Stream releases the sensor.
      guard let newCamera = try newSession.addCamera(
        config: StreamConfiguration(videoCodec: .hvc1, resolution: .low, frameRate: 7)
      ) else {
        throw CaptureFailure.message("Could not open the glasses camera. Try again.")
      }
      camera = newCamera
      photoState = .stopped
      observePhoto(newCamera.photo, id: id)
      tokens.append(newCamera.statePublisher.listen { [weak self] state in
        Task { @MainActor in
          guard let self, self.acceptingEvents, self.attemptID == id else { return }
          self.trace("Camera state: \(String(describing: state))")
        }
      })
      tokens.append(newCamera.stream.statePublisher.listen { [weak self] state in
        Task { @MainActor in
          guard let self, self.acceptingEvents, self.attemptID == id else { return }
          self.trace("Stream state: \(String(describing: state))")
        }
      })
      tokens.append(newCamera.stream.errorPublisher.listen { [weak self] error in
        Task { @MainActor in
          guard let self, self.acceptingEvents, self.attemptID == id else { return }
          self.traceError(error, source: "Stream")
          guard self.isBootstrappingStream else { return }
          self.pendingFailure = error
        }
      })
      isBootstrappingStream = true
      setStage("Waking camera")
      statusMessage = "Waking glasses camera…"
      newCamera.stream.start()
      try await waitUntil(timeout: .seconds(12), message: "The glasses camera did not wake up. Keep the glasses on and try again.") {
        newCamera.stream.state == .streaming
      }
      setStage("Starting photo mode")
      newCamera.photo.start()
      statusMessage = "Starting glasses camera…"
      try await waitUntil(timeout: .seconds(12), message: "The photo camera did not become ready. Please try again; the glasses may need a camera restart.") {
        self.photoState == .started
      }
      isBootstrappingStream = false
      setStage("Switching to photo mode")
      newCamera.stream.stop()
      try await waitUntil(timeout: .seconds(5), message: "The camera is still switching to photo mode. Please try again.") {
        newCamera.stream.state == .stopped
      }

      // A stopped Stream is necessary but has not been sufficient in repeated
      // hardware tests. Give the handoff a short, cancellable settling window;
      // this is a mitigation under test, not a diagnosis of SDK capture errors.
      setStage("Preparing shutter")
      statusMessage = "Preparing shutter…"
      try await waitForStablePhotoReadiness(newCamera)

      try Task.checkCancellation()
      setStage("Taking and receiving photo")
      traceDeviceState(device)
      try checkWearState(device)
      trace("Shutter request; session=\(newSession.state.description), camera=\(String(describing: newCamera.state)), stream=\(String(describing: newCamera.stream.state)), photo=\(String(describing: photoState))")
      statusMessage = "Taking photo…"
      newCamera.photo.capturePhoto(resolution: .full, quality: .high)
      try await waitUntil(
        timeout: .seconds(25),
        message: "The photo did not arrive. Keep your glasses close to your iPhone and try again.",
        checkState: {
          try self.checkWearState(device)
          if newCamera.state == .stopped || self.photoState == .stopped {
            throw CaptureFailure.message("The glasses camera stopped before the photo arrived. Try taking the photo again.")
          }
        }
      ) {
        self.pendingPhoto != nil
      }
      guard let imageData = pendingPhoto, !imageData.isEmpty else {
        throw CaptureFailure.message("The glasses returned an empty photo. Try again.")
      }
      pendingPhoto = nil

      // Release the hardware before image work. ImageIO downsamples without
      // decoding a full-resolution UIImage, and only a small JPEG reaches the UI.
      let didStop = await closeCamera(id: id)
      guard didStop else { return nil }
      try Task.checkCancellation()
      setStage("Preparing photo")
      statusMessage = "Preparing photo…"
      result = try await RoboMemoPhotoThumbnailer.shared.thumbnail(from: imageData)
      try Task.checkCancellation()
      setStage("Complete")
      statusMessage = "Photo captured from glasses."
    } catch is CancellationError {
      trace("Capture cancelled")
      statusMessage = "Photo capture cancelled."
    } catch {
      traceError(error, source: "Capture failed during \(captureStage)")
      if let selectedDevice { traceDeviceState(selectedDevice) }
      trace("Failure state: session=\(String(describing: session?.state)), camera=\(String(describing: camera?.state)), stream=\(String(describing: camera?.stream.state)), photo=\(String(describing: photoState))")
      requiresDATAppUpdate = (error as? DeviceSessionError) == .datAppOnTheGlassesUpdateRequired
      if let photoError = error as? PhotoError, case .captureFailure = photoError {
        errorMessage = "The glasses could not complete this photo. Wait a moment, then tap Take photo with glasses again. Any previous photo is unchanged."
      } else {
        errorMessage = "\(captureStage): \(error.localizedDescription)"
      }
      statusMessage = nil
    }

    let didStop = await closeCamera(id: id)
    if didStop, attemptID == id { isCapturing = false }
    return Task.isCancelled || !didStop ? nil : result
  }

  func selectRecordingPreset(_ preset: RecordingPreset) {
    guard !isBusy, session == nil else { return }
    selectedRecordingPreset = preset
  }

  func setMultiSensorEnabled(_ enabled: Bool) {
    guard !isBusy, session == nil else { return }
    multiSensorEnabled = enabled
  }

  func setSyncTestEnabled(_ enabled: Bool) {
    guard !isBusy, session == nil else { return }
    syncTestEnabled = enabled
  }

  func setMotionRateHz(_ rate: Int) {
    guard !isBusy, session == nil, rate == 60 || rate == 30 else { return }
    motionRateHz = rate
  }

  func setSensorAudioEnabled(_ enabled: Bool) {
    guard !isBusy, session == nil else { return }
    sensorAudioEnabled = enabled
  }

  func finishSyncTest() {
    guard syncTestInProgress else { return }
    sensorController?.recorder.markSyncTestEnd(hostTime: RoboMemoHostClock.now(), reason: "user_finished")
    syncTestInProgress = false
  }

  /// Runs until Stop, backgrounding, disconnect, or an SDK error. Permission
  /// redirects are exempt from background cancellation until the user returns.
  func startLivePreview(
    recordVideo: Bool = false,
    stopDisplaySession: @escaping @MainActor () async throws -> Void
  ) async {
    guard !isBusy, session == nil else { return }
    // Freeze the selection before any permission or device-session awaits.
    // The stream and its metadata must describe the same requested preset.
    let recordingPreset: RecordingPreset? = recordVideo ? (multiSensorEnabled ? .high7 : selectedRecordingPreset) : nil
    sensorController = nil
    syncTestStarted = false
    syncTestInProgress = false
    sensorCaptureStopped = false
    sensorStatusMessage = nil
    if let recordingPreset {
      do {
        let baseDirectory = try RoboMemoVideoClip.directory()
        let sensors = multiSensorEnabled
          ? try RoboMemoSensorRecorder(parentDirectory: baseDirectory, syncTest: syncTestEnabled,
                                      requestedMotionRateHz: motionRateHz,
                                      requestedAudioEnabled: sensorAudioEnabled) : nil
        if let sensors { sensorController = RoboMemoMultiSensorController(recorder: sensors) }
        var metadataSink: (@Sendable (Data) -> Void)?
        if let sensors { metadataSink = { data in sensors.setVideoMetadata(data) } }
        videoRecorder = RoboMemoVideoRecorder(
          directory: sensors?.directory ?? baseDirectory,
          requestedResolution: recordingPreset.requestedResolution,
          frameRate: recordingPreset.frameRate,
          sessionLayout: sensors != nil,
          metadataSink: metadataSink
        )
      } catch {
        errorMessage = "Could not create a recording: \(error.localizedDescription)"
        return
      }
    }
    // A five-minute foreground recording must not be cut short by auto-lock.
    // Restore the previous setting on every completion/error/cancellation path.
    let previousIdleTimerDisabled = UIApplication.shared.isIdleTimerDisabled
    if recordVideo { UIApplication.shared.isIdleTimerDisabled = true }
    defer {
      if recordVideo { UIApplication.shared.isIdleTimerDisabled = previousIdleTimerDisabled }
    }
    isRecording = recordVideo
    recordingSeconds = 0
    let id = UUID()
    attemptID = id
    attemptStartedAt = .now
    previewState = .preparing
    previewImage = nil
    errorMessage = nil
    requiresDATAppUpdate = false
    lastFrameAt = nil
    previewFrameCount = 0
    statusMessage = "Starting live preview…"
    setStage("Preparing live preview")
    let task = Task<Void, Never> { [weak self] in
      await self?.runLivePreview(id: id, recordingPreset: recordingPreset, stopDisplaySession: stopDisplaySession)
    }
    previewTask = task
    await withTaskCancellationHandler {
      await task.value
    } onCancel: {
      task.cancel()
    }
    if attemptID == id { previewTask = nil }
  }

  func stopForBackground() {
    guard isPreviewing, !isRequestingPermission else { return }
    if isRecording { beginVideoBackgroundTask() }
    trace("Stopping live preview for background")
    cancel()
  }

  private func runLivePreview(
    id: UUID,
    recordingPreset: RecordingPreset?,
    stopDisplaySession: @escaping @MainActor () async throws -> Void
  ) async {
    do {
      let (device, newSession) = try await prepareSession(id: id, stopDisplaySession: stopDisplaySession)
      sensorController?.observeDevice(newSession.device ?? device, session: newSession)
      setStage("Starting video stream")
      // The recorder receives every callback directly. Only preview frames use
      // the bounded mailbox; conversion stays serial and off the main actor.
      let (frames, continuation) = AsyncStream<VideoFrame>.makeStream(bufferingPolicy: .bufferingNewest(1))
      frameContinuation = continuation
      let recorder = videoRecorder
      frameTask = Task.detached(priority: .userInitiated) { [weak self] in
        var nextPreviewAt = ContinuousClock.now
        for await frame in frames {
          guard !Task.isCancelled else { break }
          // Redraw the phone at most 7 fps.
          guard ContinuousClock.now >= nextPreviewAt else { continue }
          nextPreviewAt = .now + .milliseconds(140)
          let image = autoreleasepool { frame.makeUIImage() }
          guard !Task.isCancelled else { break }
          if let image { await self?.receivePreviewImage(image, id: id) }
        }
      }
      let useAudio = sensorController?.audioPermitted == true
      var retriedWithoutAudio = !useAudio
      var stream: MWDATCamera.Stream
      do {
        stream = try await openLiveCamera(session: newSession, preset: recordingPreset,
                                         audio: useAudio, continuation: continuation, id: id)
      } catch {
        try Task.checkCancellation()
        guard useAudio, newSession.state == .started,
          pendingFailure == nil || isRecoverableCombinedStreamError(pendingFailure!) else { throw error }
        sensorController?.failAudio("Audio/video stream startup failed; retrying video only: \(error)")
        pendingFailure = nil
        retriedWithoutAudio = true
        try await releaseLiveCameraForRetry()
        stream = try await openLiveCamera(session: newSession, preset: recordingPreset,
                                         audio: false, continuation: continuation, id: id)
      }
      var frameDeadline = ContinuousClock.now + .seconds(12)
      var wasPaused = false
      while true {
        try Task.checkCancellation()
        if let recorder {
          let progress = recorder.snapshot
          recordingSeconds = Int(progress.elapsed)
          if let error = progress.error { throw CaptureFailure.message(error) }
          if progress.elapsed >= recorder.maximumDuration || progress.stopped {
            trace("Recording reached its limit; frames=\(progress.frames)")
            recorder.requestStop()
            break
          }
        }
        if let pendingFailure {
          if !retriedWithoutAudio, newSession.state == .started, isRecoverableCombinedStreamError(pendingFailure) {
            sensorController?.failAudio("Combined stream failed; attempting video only: \(pendingFailure)")
            self.pendingFailure = nil
          } else { throw pendingFailure }
        }
        guard newSession.state != .stopped, device.linkState == .connected else {
          throw CaptureFailure.message("The glasses disconnected. Reconnect, then start live preview again.")
        }
        try checkWearState(device)
        // An audio fault can also terminate the combined SDK stream. Retry
        // only Camera, once, on the same DeviceSession and same video writer.
        if !retriedWithoutAudio, stream.state != .paused,
          stream.state == .stopped || stream.state == .stopping
            || (sensorController?.audioFailed == true && ContinuousClock.now >= frameDeadline) {
          retriedWithoutAudio = true
          sensorController?.failAudio("Combined camera stream interrupted; video-only retry (cause may be audio or transport)")
          sensorController?.recorder.recordEvent("video_only_restart")
          try await releaseLiveCameraForRetry()
          stream = try await openLiveCamera(session: newSession, preset: recordingPreset,
                                           audio: false, continuation: continuation, id: id)
          lastFrameAt = nil
          frameDeadline = .now + .seconds(12)
          continue
        }
        switch stream.state {
        case .paused:
          sensorController?.cameraStreamPausedOrRestarting()
          if lastFrameAt == nil, ContinuousClock.now >= frameDeadline {
            throw CaptureFailure.message("The camera paused before sending an image. Please start it again.")
          }
          if !wasPaused { trace("Live preview paused; holding last frame") }
          wasPaused = true
          previewState = .paused
          statusMessage = isRecording
            ? "Camera paused — last image held. The 5-minute timer continues."
            : "Paused — last frame shown. Tap the glasses touchpad to resume, or stop preview."
        case .streaming:
          sensorController?.startMotionAfterStreaming()
          if wasPaused {
            trace("Live preview resumed")
            frameDeadline = .now + .seconds(12)
            lastFrameAt = nil
            wasPaused = false
          }
          if let lastFrameAt {
            previewState = .live
            statusMessage = recordingPreset.map {
              sensorController == nil ? "Recording · \($0.label) · No audio · Saved on this phone"
                : "多传感器录制 · High @7 · 保存在本机"
            }
              ?? "Live · Local preview only"
            if syncTestEnabled, !syncTestStarted, let controller = sensorController,
              controller.motionSetupFinished, controller.audioReadyForSync {
              syncTestStarted = true
              syncTestInProgress = true
              controller.recorder.markSyncTestStart(hostTime: RoboMemoHostClock.now())
            }
            frameDeadline = lastFrameAt + .seconds(10)
          } else {
            previewState = .preparing
            statusMessage = "Waiting for camera image…"
          }
          guard ContinuousClock.now < frameDeadline else {
            throw CaptureFailure.message("No new camera images arrived. Stop and restart the preview with your glasses nearby.")
          }
        case .stopped, .stopping:
          throw CaptureFailure.message("The glasses stopped the preview. You can start it again.")
        case .starting, .waitingForDevice:
          guard ContinuousClock.now < frameDeadline else {
            throw CaptureFailure.message("The preview connection timed out. Reconnect your glasses and try again.")
          }
        @unknown default:
          throw CaptureFailure.message("The camera entered an unsupported state. Please restart preview.")
        }
        if let controller = sensorController {
          let counts = controller.recorder.snapshot
          let audioStatus = controller.recorder.requestedAudioEnabled
            ? "已写入音频 \(counts.audioChunks) 块" : "音频已关闭"
          sensorStatusMessage = "\(audioStatus) · IMU \(counts.motionSamples) 条 · 状态 \(counts.deviceStateChanges) 条"
          if controller.audioFailed { sensorStatusMessage! += " · 音频不可用或已中断" }
          if counts.errors > 0 || counts.droppedJobs > 0 { sensorStatusMessage! += " · 有诊断警告，详见 session.json" }
          if syncTestEnabled, !syncTestStarted { sensorStatusMessage! += " · 等待传感器启动结果后提示同步动作" }
        }
        try await Task.sleep(for: .milliseconds(200))
      }
    } catch is CancellationError {
      trace("Live preview stopped by user or app lifecycle")
      statusMessage = "Live preview stopped."
    } catch {
      traceError(error, source: "Live preview failed")
      requiresDATAppUpdate = (error as? DeviceSessionError) == .datAppOnTheGlassesUpdateRequired
      errorMessage = error.localizedDescription
      statusMessage = nil
    }
    videoRecorder?.requestStop()
    stopSensorCapture(reason: "recording_finished")
    previewState = .stopping
    isSavingVideo = isRecording
    if isRecording { beginVideoBackgroundTask() }
    let didStop = await closeCamera(id: id)
    var savedVideo: RoboMemoVideoClip?
    if let recorder = videoRecorder {
      statusMessage = "Saving video…"
      do {
        let clip = try await Task.detached { try await recorder.finish() }.value
        recordedClips.insert(clip, at: 0)
        savedVideo = clip
        statusMessage = sensorController == nil
          ? String(format: "Video saved · %.1f seconds · No audio", clip.duration)
          : String(format: "多传感器文件夹已保存 · 视频 %.1f 秒", clip.duration)
        trace("Video saved; duration=\(clip.duration)s, bytes=\(clip.bytes), frames=\(recorder.snapshot.frames)")
      } catch {
        traceError(error, source: "Saving video")
        errorMessage = [errorMessage, error.localizedDescription].compactMap { $0 }.joined(separator: " ")
        statusMessage = nil
      }
    }
    if let controller = sensorController {
      lastSensorFolder = controller.recorder.directory
      if let error = await controller.recorder.finish(video: savedVideo, error: errorMessage) {
        errorMessage = [errorMessage, error].compactMap { $0 }.joined(separator: " ")
      }
    }
    sensorController = nil
    videoRecorder = nil
    isRecording = false
    isSavingVideo = false
    endVideoBackgroundTask()
    if didStop, attemptID == id { previewState = .idle }
  }

  private func stopSensorCapture(reason: String) {
    guard let output = sensorController?.recorder, !sensorCaptureStopped else { return }
    sensorCaptureStopped = true
    let hostTime = RoboMemoHostClock.now()
    output.stopAcceptingSamples()
    output.recordEvent("capture_stop_requested", hostTime: hostTime, details: ["reason": reason])
    if syncTestInProgress {
      output.markSyncTestEnd(hostTime: hostTime, reason: "recording_stopped_before_user_finished")
      syncTestInProgress = false
    }
  }

  private func isRecoverableCombinedStreamError(_ error: any Error) -> Bool {
    guard let error = error as? StreamError else { return false }
    switch error {
    case .audioStreamingError, .permissionDenied, .internalError, .timeout, .videoStreamingError: return true
    default: return false
    }
  }

  private func openLiveCamera(
    session: DeviceSession, preset: RecordingPreset?, audio: Bool,
    continuation: AsyncStream<VideoFrame>.Continuation, id: UUID
  ) async throws -> MWDATCamera.Stream {
    try Task.checkCancellation()
    let config = StreamConfiguration(videoCodec: .raw,
      audioCodec: audio ? .pcm(sampleRate: .rate16000, numberOfChannels: 1) : nil,
      resolution: preset?.resolution ?? .low, frameRate: preset?.frameRate ?? 7)
    guard let newCamera = try session.addCamera(config: config) else {
      throw CaptureFailure.message("Could not open the glasses camera. Try again.")
    }
    camera = newCamera
    let stream = newCamera.stream
    let streamID = UUID()
    liveStreamID = streamID
    liveStreamTokens.append(stream.errorPublisher.listen { [weak self] error in
      let hostTime = RoboMemoHostClock.now()
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id, self.liveStreamID == streamID else { return }
        self.traceError(error, source: "Live stream")
        self.sensorController?.recorder.recordEvent("stream_error", hostTime: hostTime,
          details: ["error": String(describing: error)])
        if error == .audioStreamingError, let controller = self.sensorController {
          controller.failAudio("StreamError.audioStreamingError")
        } else {
          self.pendingFailure = error
        }
      }
    })
    let recorder = videoRecorder
    let sensors = sensorController?.recorder
    liveStreamTokens.append(stream.videoFramePublisher.listen { frame in
      let hostTime = RoboMemoHostClock.now()
      recorder?.append(frame.sampleBuffer, hostTime: hostTime)
      continuation.yield(frame)
    })
    liveStreamTokens.append(stream.statePublisher.listen { state in
      sensors?.recordEvent("camera_stream_state", hostTime: RoboMemoHostClock.now(),
        details: ["state": String(describing: state), "audioRequested": String(audio)])
    })
    if audio { sensorController?.attachAudio(to: stream) }
    recorder?.markStreamStart()
    stream.start()
    try await waitUntil(timeout: .seconds(15), message: "The live camera did not start. Keep your glasses on and try again.") {
      stream.state == .streaming || stream.state == .paused
    }
    return stream
  }

  private func releaseLiveCameraForRetry() async throws {
    sensorController?.cameraStreamPausedOrRestarting()
    liveStreamID = UUID()
    let oldTokens = liveStreamTokens
    liveStreamTokens = []
    for token in oldTokens { await token.cancel() }
    await sensorController?.detachAudio()
    guard let old = camera else { return }
    old.stop()
    let deadline = ContinuousClock.now + .seconds(5)
    while old.state != .stopped {
      try Task.checkCancellation()
      guard ContinuousClock.now < deadline else {
        throw CaptureFailure.message("The camera did not stop for the video-only retry. Existing video will be saved.")
      }
      try await Task.sleep(for: .milliseconds(50))
    }
    camera = nil
  }

  private func beginVideoBackgroundTask() {
    guard videoBackgroundTask == .invalid else { return }
    videoBackgroundTask = UIApplication.shared.beginBackgroundTask(withName: "Save RoboMemo video") { [weak self] in
      Task { @MainActor in self?.endVideoBackgroundTask() }
    }
  }

  private func endVideoBackgroundTask() {
    guard videoBackgroundTask != .invalid else { return }
    UIApplication.shared.endBackgroundTask(videoBackgroundTask)
    videoBackgroundTask = .invalid
  }

  private func receivePreviewImage(_ image: UIImage, id: UUID) {
    guard acceptingEvents, attemptID == id, isPreviewing, previewState != .stopping,
      camera?.stream.state == .streaming else { return }
    previewImage = image
    lastFrameAt = .now
    previewFrameCount += 1
    if previewFrameCount == 1 || previewFrameCount % 70 == 0 {
      trace("Live preview frames displayed: \(previewFrameCount); size=\(Int(image.size.width))x\(Int(image.size.height))")
    }
  }

  private func prepareSession(
    id: UUID,
    stopDisplaySession: @escaping @MainActor () async throws -> Void
  ) async throws -> (Device, DeviceSession) {
    try Task.checkCancellation()
    guard wearables.registrationState == .registered else {
      throw CaptureFailure.message("Register this app with Meta AI before using the glasses camera.")
    }
    guard let device = wearables.devices.compactMap({ wearables.deviceForIdentifier($0) })
      .first(where: { $0.supportsDisplay() && $0.linkState == .connected })
    else {
      throw CaptureFailure.message("Connect and put on your Display glasses, then try again.")
    }
    traceDeviceState(device)
    try checkWearState(device)

    // DeviceSession is exclusive: finish the existing display session before
    // creating a camera session. The outer timeout cancels the stream wait.
    setStage("Closing display")
    try await stopDisplayWithDeadline(stopDisplaySession)
    try Task.checkCancellation()

    setStage("Camera permission")
    statusMessage = "Checking glasses camera permission…"
    if try await wearables.checkPermissionStatus(.camera) != .granted {
      try Task.checkCancellation()
      statusMessage = "Allow camera access in Meta AI, then return here."
      isRequestingPermission = true
      defer { isRequestingPermission = false }
      guard try await wearables.requestPermission(.camera) == .granted else {
        throw CaptureFailure.message("Camera access was not granted. Allow access in Meta AI and try again.")
      }
      // The callback can precede the foreground transition. Keep suppression
      // active during this bounded wait; never start a camera in background.
      let foregroundDeadline = ContinuousClock.now + .seconds(5)
      while UIApplication.shared.applicationState != .active, ContinuousClock.now < foregroundDeadline {
        try await Task.sleep(for: .milliseconds(100))
      }
    }
    guard UIApplication.shared.applicationState == .active else {
      throw CaptureFailure.message("Return to RoboMemo and start the camera again.")
    }
    trace("Camera permission granted")
    try Task.checkCancellation()

    if let controller = sensorController, controller.recorder.requestedAudioEnabled {
      // Optional microphone permission is checked before DeviceSession starts,
      // so the Meta AI permission redirect cannot interrupt a running video.
      isRequestingPermission = true
      do {
        statusMessage = "多传感器录制需要眼镜麦克风权限；未允许时仍会录视频。"
        let status = try await wearables.checkPermissionStatus(.microphone)
        let granted = status == .granted ? true : try await wearables.requestPermission(.microphone) == .granted
        controller.setAudioPermission(granted: granted, reason: granted ? nil : "Microphone permission denied")
      } catch {
        controller.setAudioPermission(granted: false, reason: "Microphone permission: \(error)")
      }
      let deadline = ContinuousClock.now + .seconds(5)
      while UIApplication.shared.applicationState != .active, ContinuousClock.now < deadline {
        do { try await Task.sleep(for: .milliseconds(100)) }
        catch { isRequestingPermission = false; throw error }
      }
      isRequestingPermission = false
      try Task.checkCancellation()
      guard UIApplication.shared.applicationState == .active else {
        throw CaptureFailure.message("Return to RoboMemo and start the camera again.")
      }
    }

    setStage("Connecting camera")
    let newSession = try wearables.createSession(
      deviceSelector: SpecificDeviceSelector(device: device.identifier)
    )
    session = newSession
    pendingFailure = nil
    pendingPhoto = nil
    sessionReachedStarted = false
    acceptingEvents = true
    tokens.append(newSession.errorPublisher.listen { [weak self] error in
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id else { return }
        self.traceError(error, source: "DeviceSession")
        self.pendingFailure = error
      }
    })
    tokens.append(newSession.statePublisher.listen { [weak self] state in
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id else { return }
        self.trace("DeviceSession state: \(state.description)")
      }
    })
    statusMessage = "Connecting to glasses camera…"
    try newSession.start()
    try await waitUntil(timeout: .seconds(15), message: "The glasses camera did not connect. Keep the glasses on and try again.") {
      newSession.state == .started
    }
    sessionReachedStarted = true

    return (device, newSession)
  }

  private func observePhoto(_ photo: Photo, id: UUID) {
    tokens.append(photo.statePublisher.listen { [weak self] state in
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id else { return }
        self.trace("Photo state: \(String(describing: state))")
        self.photoState = state
      }
    })
    tokens.append(photo.photoDataPublisher.listen { [weak self] data in
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id,
          self.pendingPhoto == nil else { return }
        self.trace("Photo received")
        self.pendingPhoto = data.imageData
      }
    })
    tokens.append(photo.errorPublisher.listen { [weak self] error in
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id else { return }
        self.traceError(error, source: "Photo")
        self.pendingFailure = error
      }
    })
    tokens.append(photo.transferProgressPublisher.listen { [weak self] progress in
      Task { @MainActor in
        guard let self, self.acceptingEvents, self.attemptID == id else { return }
        if progress.totalBytes > 0, progress.fraction.isFinite {
          let percent = Int(min(1, max(0, progress.fraction)) * 100)
          self.statusMessage = "Receiving photo… \(percent)%"
          let bucket = percent / 25
          if bucket > self.lastProgressBucket {
            self.lastProgressBucket = bucket
            self.trace("Photo transfer: \(percent)%")
          }
        } else {
          self.statusMessage = "Receiving photo…"
        }
      }
    })
  }

  private func waitUntil(
    timeout: Duration,
    message: String,
    checkState: @MainActor () throws -> Void = {},
    condition: @MainActor () -> Bool
  ) async throws {
    let deadline = ContinuousClock.now + timeout
    while true {
      try Task.checkCancellation()
      if let pendingFailure { throw pendingFailure }
      if condition() { return }
      try checkState()
      if sessionReachedStarted, let session, session.state == .stopped {
        throw CaptureFailure.message("The glasses camera disconnected. Reconnect and try again.")
      }
      guard ContinuousClock.now < deadline else { throw CaptureFailure.message(message) }
      try await Task.sleep(for: .milliseconds(100))
    }
  }

  private func waitForStablePhotoReadiness(_ camera: Camera) async throws {
    var readySince: ContinuousClock.Instant?
    try await waitUntil(
      timeout: .seconds(5),
      message: "The camera did not finish switching to photo mode. Try again.",
      checkState: {
        if camera.state == .stopped || self.photoState == .stopped {
          throw CaptureFailure.message("The glasses camera stopped while preparing the photo. Try again.")
        }
      }
    ) {
      guard self.session?.state == .started, camera.state == .started,
        camera.stream.state == .stopped, self.photoState == .started else {
        readySince = nil
        return false
      }
      let now = ContinuousClock.now
      guard let readySince else {
        readySince = now
        return false
      }
      return now - readySince >= .seconds(1)
    }
  }

  private func stopDisplayWithDeadline(
    _ operation: @escaping @MainActor () async throws -> Void
  ) async throws {
    let stopTask = Task { try await operation() }
    try await withTaskCancellationHandler {
      try await withThrowingTaskGroup(of: Void.self) { group in
        group.addTask { try await stopTask.value }
        group.addTask {
          try await Task.sleep(for: .seconds(8))
          throw CaptureFailure.message("The display is still closing. Wait a moment and try again.")
        }
        defer {
          stopTask.cancel()
          group.cancelAll()
        }
        _ = try await group.next()
      }
    } onCancel: {
      stopTask.cancel()
    }
  }

  /// Teardown runs in its own task so cancelling a capture cannot skip cleanup.
  /// If the SDK stalls, retain its handle and keep controls busy until stopped;
  /// never create a replacement session alongside a still-active one.
  private func closeCamera(id: UUID) async -> Bool {
    acceptingEvents = false
    frameContinuation?.finish()
    frameContinuation = nil
    frameTask?.cancel()
    await frameTask?.value
    frameTask = nil
    previewImage = nil
    lastFrameAt = nil
    isBootstrappingStream = false
    pendingPhoto = nil
    pendingFailure = nil
    let oldTokens = tokens
    tokens.removeAll()
    let oldCamera = camera
    let oldSession = session
    guard oldCamera != nil || oldSession != nil else { return true }
    trace("Closing Photo, Camera, and DeviceSession")

    let cleanup = Task { @MainActor in
      await self.sensorController?.stop()
      self.liveStreamID = UUID()
      let oldStreamTokens = self.liveStreamTokens
      self.liveStreamTokens = []
      for token in oldStreamTokens { await token.cancel() }
      for token in oldTokens { await token.cancel() }
      oldCamera?.photo.stop()
      oldCamera?.stop()
      oldSession?.stop()
      let deadline = ContinuousClock.now + .seconds(5)
      while let oldSession, oldSession.state != .stopped {
        guard ContinuousClock.now < deadline else { return false }
        try? await Task.sleep(for: .milliseconds(100))
      }
      return true
    }
    let didStop = await cleanup.value
    guard attemptID == id else { return didStop }
    if didStop {
      trace("Camera session stopped")
      camera = nil
      session = nil
      return true
    }

    trace("Camera teardown exceeded deadline; retaining session until stopped")
    errorMessage = "The camera is still closing. Keep your glasses nearby until it finishes."
    statusMessage = "Closing glasses camera…"
    delayedTeardownTask?.cancel()
    delayedTeardownTask = Task { [weak self, oldSession] in
      guard let oldSession else { return }
      // Read current state as well as subsequent changes, so a stop between the
      // deadline and this task's first turn cannot leave the controls busy.
      while oldSession.state != .stopped {
        do { try await Task.sleep(for: .milliseconds(200)) }
        catch { return }
      }
      guard !Task.isCancelled, let self, self.attemptID == id else { return }
      self.camera = nil
      self.session = nil
      self.isCapturing = false
      self.previewState = .idle
      self.trace("Delayed camera teardown completed")
      self.statusMessage = "Camera closed. You can try again."
    }
    return false
  }

  private func setStage(_ stage: String) {
    captureStage = stage
    trace("Stage: \(stage)")
  }

  private func trace(_ event: String) {
    let attempt = attemptID.map { String($0.uuidString.prefix(8)) } ?? "none"
    let elapsed = attemptStartedAt.duration(to: .now).components
    let milliseconds = elapsed.seconds * 1_000 + elapsed.attoseconds / 1_000_000_000_000_000
    logger.info("[RoboMemoCamera \(attempt, privacy: .public) +\(milliseconds)ms] \(event, privacy: .public)")
  }

  private func traceDeviceState(_ device: Device) {
    trace("Device state: link=\(String(describing: device.linkState)), worn=\(String(describing: device.donState)), battery=\(device.batteryLevel.map(String.init) ?? "unknown"), thermal=\(String(describing: device.thermalLevel))")
  }

  /// Unknown wear state is not a denial. Only act on an explicit SDK report.
  private func checkWearState(_ device: Device) throws {
    if device.donState == .doffed {
      throw CaptureFailure.message("Your glasses report that they are not being worn. Put them on, adjust the fit, then try again.")
    }
  }

  /// Logs typed SDK failures and underlying domain/code only: never image data,
  /// metadata, credentials, or an NSError's potentially sensitive userInfo.
  private func traceError(_ error: any Error, source: String) {
    let detail: String
    if let photoError = error as? PhotoError {
      switch photoError {
      case .notReady: detail = "PhotoError.notReady"
      case .busy: detail = "PhotoError.busy"
      case .serviceUnavailable: detail = "PhotoError.serviceUnavailable"
      case .permissionDenied: detail = "PhotoError.permissionDenied"
      case .deviceHealthCritical: detail = "PhotoError.deviceHealthCritical"
      case .deviceDisconnected: detail = "PhotoError.deviceDisconnected"
      case .captureFailure(let underlying):
        detail = "PhotoError.captureFailure; \(underlyingErrorCode(underlying))"
      case .sessionSetupFailed(let underlying):
        detail = "PhotoError.sessionSetupFailed; \(underlyingErrorCode(underlying))"
      @unknown default: detail = "PhotoError.unknown"
      }
    } else if let streamError = error as? StreamError {
      switch streamError {
      case .deviceNotFound: detail = "StreamError.deviceNotFound"
      case .deviceNotConnected: detail = "StreamError.deviceNotConnected"
      default: detail = "StreamError: \(String(reflecting: streamError))"
      }
    } else if let sessionError = error as? DeviceSessionError {
      switch sessionError {
      case .unexpectedError: detail = "DeviceSessionError.unexpectedError (payload omitted)"
      default: detail = "DeviceSessionError: \(String(reflecting: sessionError))"
      }
    } else {
      detail = "\(String(reflecting: type(of: error))); \(underlyingErrorCode(error))"
    }
    trace("\(source): \(detail)")
  }

  private func underlyingErrorCode(_ error: (any Error)?) -> String {
    guard let error else { return "no underlying error" }
    let nsError = error as NSError
    return "domain=\(nsError.domain), code=\(nsError.code)"
  }
}

private enum CaptureFailure: LocalizedError {
  case message(String)

  var errorDescription: String? {
    switch self {
    case .message(let message): message
    }
  }
}
