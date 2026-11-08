import AVFoundation
import Foundation
import MWDATCamera
import MWDATCore
import MWDATMotion

@MainActor @main struct ControllerValidation {
  static func waitUntil(_ condition: () -> Bool) async throws {
    let end = ContinuousClock.now + .seconds(8)
    while !condition() {
      guard ContinuousClock.now < end else { throw RecorderTestFailure.check("controller wait timed out") }
      try await Task.sleep(for: .milliseconds(20))
    }
  }

  static func main() async {
    do {
      let out = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
      try await fallback(out, name: "async_unavailable", plans: ["unavailable", "success"])
      try await fallback(out, name: "stopped_before_error", plans: ["stopped_before_error", "success"])
      try await fallback(out, name: "failure_after_start", plans: ["starts_then_unavailable", "success"])
      try await stopDuringStart(out)
      try await pausedCameraFallback(out)
      try await audioIsolation(out)
      try await requestedThirtyHz(out)
      try await audioDisabled(out)
      print("ALL CONTROLLER LIFECYCLE CHECKS PASSED")
    } catch { print("FAIL \(error)"); exit(1) }
  }

  static func fallback(_ out: URL, name: String, plans: [String]) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: out.appendingPathComponent(name), syncTest: false)
    let controller = RoboMemoMultiSensorController(recorder: recorder)
    let session = DeviceSession(plans: plans)
    controller.observeDevice(Device(), session: session)
    controller.startMotionAfterStreaming()
    controller.startMotionAfterStreaming() // repeated streaming notifications must not attach twice
    try await waitUntil { session.attempts.count == 2 && controller.motionSetupFinished && recorder.snapshot.motionSamples > 0 }
    try require(session.attempts == [60, 30], "\(name) falls back from60to30 exactly once")
    try require(session.removals == 1 && session.activeMotion, "\(name) old capability removed before replacement")
    recorder.stopAcceptingSamples()
    await controller.stop()
    try require(!session.activeMotion && session.removals == 2, "\(name) final capability detached")
    await controller.stop()
    try require(session.removals == 2, "\(name) duplicate stop is idempotent")
    _ = await recorder.finish(video: nil, error: "test_no_video")
    let json = try readJSON(recorder.directory.appendingPathComponent("session.json"))
    let modules = json["modules"] as! [String: [String: Any]]
    try require(modules["motion"]?["configuredRateHz"] as? Int == 30, "\(name) successful rate saved")
    let events = json["events"] as! [[String: Any]]
    try require(events.contains { $0["name"] as? String == "motion_rate_fallback" }, "\(name) records downgrade event")
  }

  static func requestedThirtyHz(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: out.appendingPathComponent("requested_30hz"),
                                              syncTest: false, requestedMotionRateHz: 30)
    let controller = RoboMemoMultiSensorController(recorder: recorder)
    let session = DeviceSession(plans: ["unavailable", "success"])
    controller.observeDevice(Device(), session: session)
    controller.startMotionAfterStreaming()
    try await waitUntil { session.attempts.count == 2 && controller.motionSetupFinished && recorder.snapshot.motionSamples > 0 }
    try require(session.attempts == [30, 24], "explicit 30 Hz starts at30 and falls backto24 without trying60")
    recorder.stopAcceptingSamples()
    await controller.stop()
    _ = await recorder.finish(video: nil, error: "test_no_video")
    let json = try readJSON(recorder.directory.appendingPathComponent("session.json"))
    let modules = json["modules"] as! [String: [String: Any]]
    try require(modules["motion"]?["configuredRateHz"] as? Int == 24, "explicit 30 Hz fallback saves actual24 Hz rate")
  }

  static func stopDuringStart(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: out.appendingPathComponent("cancel_start"), syncTest: false)
    let controller = RoboMemoMultiSensorController(recorder: recorder)
    let session = DeviceSession(plans: ["never_started"])
    controller.observeDevice(Device(), session: session)
    controller.startMotionAfterStreaming()
    try await waitUntil { session.attempts.count == 1 }
    recorder.stopAcceptingSamples()
    let before = ContinuousClock.now
    await controller.stop()
    try require(ContinuousClock.now - before < .seconds(1), "cancel during pending start does not await startup timeout")
    try require(session.attempts == [60] && !session.activeMotion && session.removals == 1, "cancel removes pending Motion and starts no lower rate")
    _ = await recorder.finish(video: nil, error: "cancelled")
  }

  static func audioIsolation(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: out.appendingPathComponent("audio_isolation"), syncTest: false)
    let controller = RoboMemoMultiSensorController(recorder: recorder)
    try require(recorder.requestedAudioEnabled && !controller.audioReadyForSync,
                "default audio remains requested and sync waits for its startup result")
    let session = DeviceSession(plans: ["success"])
    controller.observeDevice(Device(), session: session)
    controller.setAudioPermission(granted: true)
    let stream = MWDATCamera.Stream()
    controller.attachAudio(to: stream)
    try require(stream.audioFramePublisher.listenerCount == 1, "default audio still subscribes to PCM")
    controller.startMotionAfterStreaming()
    stream.audioFramePublisher.emit(AudioFrame(try makeAudio([2, 4, 6, 8])))
    try await waitUntil { recorder.snapshot.audioChunks == 1 && controller.motionSetupFinished }
    try require(controller.audioReadyForSync, "valid default audio makes sync ready")
    controller.failAudio("injected_audio_stream_failure")
    try require(controller.audioFailed && session.activeMotion, "audio failure does not stop the Motion session")
    await controller.detachAudio()
    stream.audioFramePublisher.emit(AudioFrame(try makeAudio([20, 40])))
    try await Task.sleep(for: .milliseconds(40))
    try require(recorder.snapshot.audioChunks == 1, "detached audio rejects late SDK delivery")
    recorder.stopAcceptingSamples()
    await controller.stop()
    _ = await recorder.finish(video: nil, error: "test_no_video")
    let json = try readJSON(recorder.directory.appendingPathComponent("session.json"))
    let modules = json["modules"] as! [String: [String: Any]]
    try require(modules["audio"]?["reason"] as? String == "injected_audio_stream_failure", "audio failure persists in session metadata")
  }

  static func audioDisabled(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: out.appendingPathComponent("audio_disabled"),
                                              syncTest: true, requestedMotionRateHz: 30, requestedAudioEnabled: false)
    let controller = RoboMemoMultiSensorController(recorder: recorder)
    let session = DeviceSession(plans: ["success"])
    let stream = MWDATCamera.Stream()
    controller.observeDevice(Device(), session: session)
    // Stray permission results/errors must not undo the frozen opt-out choice.
    controller.setAudioPermission(granted: true)
    controller.setAudioPermission(granted: false, reason: "injected_permission_denied")
    controller.failAudio("injected_unrequested_audio_error")
    controller.attachAudio(to: stream)
    try require(!controller.audioPermitted && !controller.audioFailed && controller.audioReadyForSync,
                "audio OFF is sync-ready and does not become permitted or failed from stray callbacks")
    try require(stream.audioFramePublisher.listenerCount == 0, "audio OFF attaches no PCM listener")
    stream.audioFramePublisher.emit(AudioFrame(try makeAudio([10,20,30,40])))
    controller.startMotionAfterStreaming()
    try await waitUntil { controller.motionSetupFinished && recorder.snapshot.motionSamples > 0 }
    try require(session.attempts == [30] && session.activeMotion,
                "audio OFF still starts the requested30 Hz Motion capability")
    // Longer than the actual production 15-second audio startup watchdog.
    try await Task.sleep(for: .milliseconds(15_250))
    try require(!controller.audioFailed && recorder.snapshot.audioChunks == 0 && recorder.snapshot.errors == 0,
                "audio OFF never times out or records unexpected PCM after the watchdog deadline")
    recorder.markSyncTestStart(hostTime: RoboMemoHostClock.now())
    recorder.markSyncTestEnd(hostTime: RoboMemoHostClock.now(), reason: "user_finished")
    recorder.stopAcceptingSamples()
    await controller.stop()
    _ = await recorder.finish(video: nil, error: "test_no_video")
    let json = try readJSON(recorder.directory.appendingPathComponent("session.json"))
    let modules = json["modules"] as! [String: [String: Any]]
    let config = json["configuration"] as! [String: Any]
    try require(modules["audio"]?["status"] as? String == "disabled"
      && modules["audio"]?["reason"] as? String == "user_disabled"
      && config["requestedAudioEnabled"] as? Bool == false,
                "audio OFF remains explicitly user-disabled in final session metadata")
    let events = json["events"] as! [[String: Any]]
    try require(!events.contains { $0["name"] as? String == "audio_failure" },
                "unrequested audio does not leave false failure events")
    try require((json["syncTest"] as! [String: Any])["endReason"] as? String == "user_finished",
                "sync interval can finish normally with audio intentionally disabled")
  }

  static func pausedCameraFallback(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: out.appendingPathComponent("paused_camera"), syncTest: false)
    let controller = RoboMemoMultiSensorController(recorder: recorder)
    let session = DeviceSession(plans: ["starts_then_unavailable", "success"])
    controller.observeDevice(Device(), session: session)
    controller.startMotionAfterStreaming()
    try await waitUntil { controller.motionSetupFinished && recorder.snapshot.motionSamples == 1 }
    controller.cameraStreamPausedOrRestarting()
    try await waitUntil { session.removals == 1 }
    try await Task.sleep(for: .milliseconds(150))
    try require(session.attempts == [60] && !session.activeMotion, "no lower Motion rate attaches while Camera is paused")
    controller.startMotionAfterStreaming()
    try await waitUntil { session.attempts.count == 2 && controller.motionSetupFinished && recorder.snapshot.motionSamples == 2 }
    try require(session.attempts == [60, 30], "Motion fallback continues when Camera resumes streaming")
    recorder.stopAcceptingSamples()
    await controller.stop()
    _ = await recorder.finish(video: nil, error: "test_no_video")
  }
}
