import CoreMedia
import Foundation
import MWDATCamera
import MWDATCore
import MWDATMotion

/// Optional capabilities belong to the camera's existing DeviceSession. Their
/// errors are recorded here and never promoted to the video recorder's error.
@MainActor
final class RoboMemoMultiSensorController {
  let recorder: RoboMemoSensorRecorder
  private(set) var audioPermitted = false
  private(set) var audioFailed = false
  private(set) var motionSetupFinished = false
  private var audioToken: (any AnyListenerToken)?
  private var deviceToken: (any AnyListenerToken)?
  private var motionTokens: [any AnyListenerToken] = []
  private var motion: Motion?
  private var motionTask: Task<Void, Never>?
  private var audioWatchdog: Task<Void, Never>?
  private var sampleTask: Task<Void, Never>?
  private var motionGeneration = UUID()
  private var lastMotionError: MotionError?
  private var stopping = false
  private var session: DeviceSession?
  private var motionWasAttempted = false
  private var cameraIsStreaming = false

  init(recorder: RoboMemoSensorRecorder) { self.recorder = recorder }

  /// User-disabled audio is ready immediately; it is not an audio failure and
  /// must not hold the video/IMU synchronization prompt behind a PCM watchdog.
  var audioReadyForSync: Bool {
    !recorder.requestedAudioEnabled || audioFailed || recorder.snapshot.audioChunks > 0
  }

  isolated deinit {
    motionTask?.cancel()
    audioWatchdog?.cancel()
    sampleTask?.cancel()
    motion?.stop()
  }

  func setAudioPermission(granted: Bool, reason: String? = nil) {
    guard recorder.requestedAudioEnabled else { return }
    audioPermitted = granted
    audioFailed = !granted
    recorder.setModule("audio", status: granted ? "pending" : "failed", reason: reason)
  }

  func observeDevice(_ device: Device, session: DeviceSession) {
    self.session = session
    let output = recorder
    // SDK invokes this once immediately and subsequently on every change.
    deviceToken = device.addDeviceStateListener { state in
      let hostTime = RoboMemoHostClock.now()
      output.appendDeviceState(state, hostTime: hostTime)
    }
  }

  func attachAudio(to stream: MWDATCamera.Stream) {
    guard recorder.requestedAudioEnabled, audioPermitted, !audioFailed, !stopping else { return }
    let output = recorder
    recorder.setModule("audio", status: "starting", rateHz: 16_000)
    // iOS 1.0.1 calls this audioFramePublisher (audioStream is Android's name).
    // Stamp and copy on delivery, before any main-actor hop or disk queue.
    audioToken = stream.audioFramePublisher.listen { frame in
      let hostTime = RoboMemoHostClock.now()
      output.appendAudio(frame.pcmBuffer, sourcePTS: frame.presentationTimeStamp, hostTime: hostTime)
    }
  }

  func failAudio(_ reason: String) {
    guard recorder.requestedAudioEnabled, !stopping else { return }
    audioFailed = true
    recorder.setModule("audio", status: "failed", reason: reason)
    recorder.recordEvent("audio_failure", details: ["reason": reason])
  }

  func detachAudio() async {
    audioWatchdog?.cancel()
    audioWatchdog = nil
    let token = audioToken
    audioToken = nil
    await token?.cancel()
  }

  /// Called only after Stream.state == .streaming; never as part of session
  /// startup. Motion's negotiation runs independently of the video's main loop.
  func startMotionAfterStreaming() {
    cameraIsStreaming = true
    guard !stopping, !motionWasAttempted, let session else { return }
    motionWasAttempted = true
    if recorder.requestedAudioEnabled, audioPermitted, !audioFailed {
      audioWatchdog = Task { [weak self] in
        do { try await Task.sleep(for: .seconds(15)) } catch { return }
        guard let self, !self.stopping, self.recorder.snapshot.audioChunks == 0 else { return }
        self.failAudio("No PCM blocks written within 15 seconds of video streaming")
      }
    }
    motionTask = Task { [weak self] in
      await self?.negotiateMotion(session: session)
    }
  }

  func cameraStreamPausedOrRestarting() { cameraIsStreaming = false }

  private func negotiateMotion(session: DeviceSession) async {
    defer { motionSetupFinished = true }
    let availableRates: [(MotionSamplingRate, Int)] = [(.hz60, 60), (.hz30, 30), (.hz24, 24),
                                                     (.hz15, 15), (.hz10, 10), (.hz5, 5)]
    let rates = availableRates.filter { $0.1 <= recorder.requestedMotionRateHz }
    for (rate, hz) in rates {
      guard !Task.isCancelled, !stopping else { return }
      // A previous IMU rate may have failed while Camera paused or retried
      // without audio. Do not add the next capability until video resumes.
      while !cameraIsStreaming {
        do { try await Task.sleep(for: .milliseconds(50)) } catch { return }
        guard !Task.isCancelled, !stopping else { return }
      }
      let generation = UUID()
      motionGeneration = generation
      lastMotionError = nil
      recorder.setModule("motion", status: "starting", rateHz: hz)
      recorder.recordEvent("motion_start_attempt", details: ["requestedRateHz": String(hz)])
      do {
        guard let next = try session.addMotion(configuration: MotionConfiguration(samplingRate: rate)) else {
          recorder.setModule("motion", status: "failed", reason: "addMotion returned nil", rateHz: hz)
          return
        }
        motion = next
        let output = recorder
        motionTokens.append(next.errorPublisher.listen { [weak self] error in
          let hostTime = RoboMemoHostClock.now()
          output.recordEvent("motion_error", hostTime: hostTime,
                             details: ["error": String(describing: error), "requestedRateHz": String(hz)])
          Task { @MainActor in
            guard let self, !self.stopping, self.motionGeneration == generation else { return }
            self.lastMotionError = error
          }
        })
        motionTokens.append(next.statePublisher.listen { state in
          output.recordEvent("motion_state", hostTime: RoboMemoHostClock.now(),
                             details: ["state": state.description, "requestedRateHz": String(hz)])
        })
        sampleTask = Task.detached(priority: .userInitiated) {
          for await sample in next.samples {
            let hostTime = RoboMemoHostClock.now()
            guard !Task.isCancelled else { break }
            // Motion only exposes AsyncStream. This is app dequeue time, not
            // an undocumented SDK transport callback timestamp.
            output.appendMotion(sample, hostTime: hostTime, requestedRateHz: hz)
          }
        }
        let initialSamples = recorder.snapshot.motionSamples
        next.start()
        // The SDK can take about 10 s to emit first-sample sensorUnavailable.
        // Do not preempt that result with a shorter app timeout.
        let deadline = ContinuousClock.now + .seconds(12)
        while !Task.isCancelled, !stopping, lastMotionError == nil,
          !(next.state == .started && recorder.snapshot.motionSamples > initialSamples),
          ContinuousClock.now < deadline {
          try await Task.sleep(for: .milliseconds(50))
        }
        try Task.checkCancellation()
        guard !stopping else { return }
        if lastMotionError == nil, next.state == .started, recorder.snapshot.motionSamples > initialSamples {
          recorder.setModule("motion", status: "active", rateHz: hz)
          recorder.recordEvent("motion_started", details: ["requestedRateHz": String(hz)])
          // Also handle sensorUnavailable delivered after .started, including
          // failures during capture, rather than only checking the start call.
          motionSetupFinished = true
          while !Task.isCancelled, !stopping, lastMotionError == nil, next.state != .stopped {
            try await Task.sleep(for: .milliseconds(100))
          }
          try Task.checkCancellation()
          guard !stopping else { return }
        }
        if next.state == .stopped, lastMotionError == nil {
          // State and error are different publishers. Allow an already queued
          // error callback to reach MainActor before deciding whether to retry.
          try await Task.sleep(for: .milliseconds(200))
        }
        let error = lastMotionError
        let reason = error.map { String(describing: $0) }
          ?? (next.state == .stopped ? "Motion stopped without an error" : "Motion startup timed out")
        recorder.recordEvent("motion_attempt_failed", details: ["requestedRateHz": String(hz), "reason": reason])
        let removed = await removeMotion(session: session)
        guard removed else { return }
        if error == .sensorUnavailable, hz != 5 {
          motionSetupFinished = false
          recorder.recordEvent("motion_rate_fallback", details: ["fromRateHz": String(hz)])
          continue
        }
        recorder.setModule("motion", status: "failed", reason: reason, rateHz: hz)
        return
      } catch is CancellationError {
        return
      } catch {
        recorder.setModule("motion", status: "failed", reason: String(describing: error), rateHz: hz)
        _ = await removeMotion(session: session)
        return
      }
    }
  }

  /// No replacement capability may be added while the prior one is stopping.
  private func removeMotion(session: DeviceSession) async -> Bool {
    motionGeneration = UUID()
    sampleTask?.cancel()
    await sampleTask?.value
    sampleTask = nil
    let oldTokens = motionTokens
    motionTokens = []
    for token in oldTokens { await token.cancel() }
    guard let old = motion else { return true }
    old.stop()
    let deadline = ContinuousClock.now + .seconds(3)
    while old.state != .stopped, ContinuousClock.now < deadline {
      // Cleanup must continue even if the recording task was cancelled.
      try? await Task.sleep(for: .milliseconds(50))
      if Task.isCancelled { break }
    }
    guard old.state == .stopped else {
      recorder.setModule("motion", status: "failed", reason: "Motion stop timed out; no replacement attached")
      return false
    }
    do {
      try session.removeMotion()
      motion = nil
      return true
    } catch DeviceSessionError.capabilityNotFound {
      // Some terminal SDK paths have already detached the stopped capability.
      motion = nil
      return true
    } catch {
      recorder.setModule("motion", status: "failed", reason: "removeMotion: \(error)")
      return false
    }
  }

  /// Called in a noncancelled teardown task before Camera/DeviceSession stop.
  func stop() async {
    guard !stopping else { return }
    stopping = true
    motionTask?.cancel()
    await motionTask?.value
    motionTask = nil
    await detachAudio()
    if let session { _ = await removeMotion(session: session) }
    let token = deviceToken
    deviceToken = nil
    await token?.cancel()
  }
}
