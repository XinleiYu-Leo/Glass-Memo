import AVFoundation
import Foundation
import MWDATCore
import MWDATMotion
import os

/// Capture at the SDK callback/AsyncStream consumer, before actor hops or IO.
/// These are phone arrival times, NOT estimates of sensor capture time.
enum RoboMemoHostClock {
  static func now() -> CMTime { CMClockGetTime(CMClockGetHostTimeClock()) }

  static func csvFields(_ time: CMTime) -> [String] {
    [String(time.value), String(time.timescale), String(time.epoch), String(time.flags.rawValue),
     time.isValid && time.seconds.isFinite ? String(time.seconds) : ""]
  }

  static func json(_ time: CMTime) -> [String: Any] {
    var value: [String: Any] = ["value": time.value, "timescale": time.timescale,
                              "epoch": time.epoch, "flags": time.flags.rawValue]
    if time.isValid && time.seconds.isFinite { value["seconds"] = time.seconds }
    return value
  }
}

/// A single serial IO queue owns sidecars; optional sensors never throw into video.
/// At most 256 data jobs (each audio chunk <= 256 KiB) can be queued. Overflow is
/// counted in session.json instead of keeping an unbounded backlog of SDK buffers.
final class RoboMemoSensorRecorder: Sendable {
  struct Snapshot: Sendable {
    let audioChunks: Int
    let motionSamples: Int
    let deviceStateChanges: Int
    let droppedJobs: Int
    let errors: Int
  }

  private struct Gate: Sendable {
    var accepting = true
    var finishing = false
    var queued = 0
    var peakQueued = 0
    var ignoredEmptyAudioBlocks = 0
    var invalidAudioBlocks = 0
    var audioChunks = 0
    var motionSamples = 0
    var deviceStateChanges = 0
    var drops: [String: Int] = [:]
    var queueOverflowDrops: [String: Int] = [:]
    var reportedInputErrors: Set<String> = []
    var errors = 0
  }

  // This box is accessed only on queue, except initialization before publication.
  private final class Storage: @unchecked Sendable {
    var files: [String: FileHandle] = [:]
    var modules: [String: [String: Any]] = [
      "video": ["status": "pending"], "audio": ["status": "pending"],
      "motion": ["status": "pending"], "deviceState": ["status": "pending"],
    ]
    var events: [[String: Any]] = []
    var errors: [[String: String]] = []
    var videoMetadata: [String: Any]?
    var audioBytes: UInt32 = 0
    var audioRate: UInt32 = 16_000
    var audioChannels: UInt16 = 1
    var audioHasFormat = false
    var audioWriteFailed = false
    var audioInputFormat: String?
    var audioSequence = 0
    var motionSequence = 0
    var deviceSequence = 0
    var audioLastPTS: CMTime?
    var audioPreviousDuration = CMTime.zero
    var audioTimestampDiscontinuities = 0
    var audioSourcePTSInvalidCount = 0
    var audioSourcePTSNonIncreasingCount = 0
    var audioSourcePTSEpochChanges = 0
    var audioPossibleSourceGapCount = 0
    var audioMaximumSourceResidualSeconds: Double = 0
    var audioLastHostTime: CMTime?
    var audioMaximumHostDeliveryGapSeconds: Double = 0
    var motionLastTimestampNs: Int64?
    var motionTimestampDiscontinuities = 0
    var motionOutOfOrderArrivalCount = 0
    var motionLastHostTime: CMTime?
    var motionMaximumHostDeliveryGapSeconds: Double = 0
    var motionTimestamps: [(timestampNs: Int64, rateHz: Int)] = []
    var motionTimestampSummaryOmittedRows = 0
    var motionCoverage: [String: Any]?
    var maximumQueueResidenceSeconds: Double = 0
    var syncStart: CMTime?
    var syncEnd: CMTime?
    var syncEndReason: String?
    var lastDeviceState: DeviceState?
    var finalManifestError: String?
  }

  // An owned copy is never concurrently read/written and is only used on queue.
  private struct OwnedPCM: @unchecked Sendable { let buffer: AVAudioPCMBuffer }

  let directory: URL
  let requestedMotionRateHz: Int
  let requestedAudioEnabled: Bool
  private let syncTest: Bool
  private let createdAt = Date()
  private let createdHostTime = RoboMemoHostClock.now()
  private let queue = DispatchQueue(label: "RoboMemo.sensor-files", qos: .utility)
  private let gate = OSAllocatedUnfairLock(initialState: Gate())
  private let storage = Storage()
  private static let maximumQueuedJobs = 256
  private static let maximumAudioChunkBytes = 256 * 1024
  // Over three times the 300 s recording limit at 60 Hz. CSV itself is complete;
  // only this in-memory summary is capped if an unexpected source floods rows.
  private static let maximumMotionDiagnosticSamples = 65_536

  init(parentDirectory: URL, syncTest: Bool, requestedMotionRateHz: Int = 60,
       requestedAudioEnabled: Bool = true) throws {
    self.syncTest = syncTest
    self.requestedAudioEnabled = requestedAudioEnabled
    guard [60, 30].contains(requestedMotionRateHz) else {
      throw Failure.message("Unsupported requested Motion test rate; choose 60 or 30 Hz.")
    }
    self.requestedMotionRateHz = requestedMotionRateHz
    directory = parentDirectory.appendingPathComponent("RoboMemo-Multi-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    if !requestedAudioEnabled {
      storage.modules["audio"] = ["status": "disabled", "reason": "user_disabled"]
    }
    let host = "hostTimeValue,hostTimeTimescale,hostTimeEpoch,hostTimeFlags,hostTimeSeconds"
    let pts = "sourcePTSValue,sourcePTSTimescale,sourcePTSEpoch,sourcePTSFlags,sourcePTSSeconds"
    createFile("audio_chunks.csv", data: Data(("chunkIndex,\(host),\(pts),sampleRate,channels,inputFrames,wavSampleOffset,wavByteOffset,result\n").utf8), module: "audio")
    createFile("motion.csv", data: Data(("sampleIndex,\(host),timestampNs,source,requestedRateHz,accelX,accelY,accelZ,gyroX,gyroY,gyroZ,orientationX,orientationY,orientationZ,orientationW,magX,magY,magZ\n").utf8), module: "motion")
    createFile("device_state.csv", data: Data(("changeIndex,\(host),sdkTimestamp,batteryLevel,chargingState,donState,hingeState,thermalLevel,linkState,compatibility\n").utf8), module: "deviceState")
    createFile("audio.wav", data: Self.wavHeader(bytes: 0, rate: 16_000, channels: 1), module: "audio")
    // The video recorder replaces this header with full diagnostics on finish.
    do { try Data("".utf8).write(to: directory.appendingPathComponent("frames.csv")) }
    catch { noteError(module: "videoDiagnostics", reason: error.localizedDescription) }
    writeManifest(status: "preparing", endTime: nil)
  }

  var snapshot: Snapshot {
    gate.withLock { Snapshot(audioChunks: $0.audioChunks, motionSamples: $0.motionSamples,
      deviceStateChanges: $0.deviceStateChanges, droppedJobs: $0.drops.values.reduce(0, +), errors: $0.errors) }
  }

  /// Do not retain an SDK-owned buffer past its callback: copy its PCM storage.
  /// Conversion to interleaved signed 16-bit PCM and all disk writes run on queue.
  func appendAudio(_ buffer: AVAudioPCMBuffer, sourcePTS: CMTime, hostTime: CMTime) {
    guard requestedAudioEnabled, gate.withLock({ $0.accepting && !$0.finishing }) else { return }
    if buffer.frameLength == 0 {
      // A real pilot delivered an initial zero-frame callback. It contains no
      // audio to lose: preserve its timing without claiming a PCM/storage error.
      // This is an observed empty callback, not a documented SDK header type.
      let rate = buffer.format.sampleRate
      let channels = buffer.format.channelCount
      gate.withLock { state in
        if state.accepting && !state.finishing { state.ignoredEmptyAudioBlocks += 1 }
      }
      enqueueSample(module: "audioDiagnostics") { recorder in
        recorder.writeAudioDiagnostic(sourcePTS: sourcePTS, hostTime: hostTime,
          rate: rate, channels: channels, frames: 0, result: "ignored_empty")
      }
      return
    }
    let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
    let byteCount = source.reduce(0) { $0 + Int($1.mDataByteSize) }
    guard byteCount > 0, byteCount <= Self.maximumAudioChunkBytes,
      let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
      rejectAudio(buffer, sourcePTS: sourcePTS, hostTime: hostTime, reason: "Invalid or oversized PCM block (limit 256 KiB).")
      return
    }
    copy.frameLength = buffer.frameLength
    let target = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
    guard source.count == target.count else {
      rejectAudio(buffer, sourcePTS: sourcePTS, hostTime: hostTime, reason: "PCM buffer layout could not be copied.")
      return
    }
    for index in source.indices {
      guard let from = source[index].mData, let to = target[index].mData,
        source[index].mDataByteSize >= target[index].mDataByteSize else {
        rejectAudio(buffer, sourcePTS: sourcePTS, hostTime: hostTime, reason: "PCM buffer storage was incomplete.")
        return
      }
      // SDK buffers may have a larger capacity than their frameLength. Copy
      // only the frames owned by this chunk, never reject spare source capacity.
      memcpy(to, from, Int(target[index].mDataByteSize))
    }
    let owned = OwnedPCM(buffer: copy)
    enqueueSample(module: "audio") { recorder in
      recorder.writeAudio(owned.buffer, sourcePTS: sourcePTS, hostTime: hostTime)
    }
  }

  func appendMotion(_ sample: MotionSample, hostTime: CMTime, requestedRateHz: Int) {
    enqueueSample(module: "motion") { recorder in
      let s = recorder.storage
      s.motionSequence += 1
      var fields = [String(s.motionSequence)] + RoboMemoHostClock.csvFields(hostTime)
      fields += [String(sample.timestampNs), String(describing: sample.source), String(requestedRateHz)]
      fields += Self.vectorFields(sample.accelerometer) + Self.vectorFields(sample.gyroscope)
      if let q = sample.orientation {
        fields += [Self.number(q.x), Self.number(q.y), Self.number(q.z), Self.number(q.w)]
      } else { fields += ["", "", "", ""] }
      fields += Self.vectorFields(sample.magnetometer)
      if recorder.writeCSV("motion.csv", fields: fields, module: "motion") {
        if let previous = s.motionLastTimestampNs {
          let (delta, overflow) = sample.timestampNs.subtractingReportingOverflow(previous)
          if sample.timestampNs < previous { s.motionOutOfOrderArrivalCount += 1 }
          if overflow || delta <= 0 || Double(delta) > (1_000_000_000 / Double(max(1, requestedRateHz))) * 2.5 {
            s.motionTimestampDiscontinuities += 1
          }
        }
        s.motionLastTimestampNs = sample.timestampNs
        if let previous = s.motionLastHostTime {
          s.motionMaximumHostDeliveryGapSeconds = max(s.motionMaximumHostDeliveryGapSeconds,
            Self.positiveInterval(hostTime, since: previous))
        }
        s.motionLastHostTime = hostTime
        if s.motionTimestamps.count < Self.maximumMotionDiagnosticSamples {
          s.motionTimestamps.append((sample.timestampNs, requestedRateHz))
        } else { s.motionTimestampSummaryOmittedRows += 1 }
        recorder.gate.withLock { $0.motionSamples += 1 }
      }
    }
  }

  func appendDeviceState(_ state: DeviceState, hostTime: CMTime) {
    enqueueSample(module: "deviceState") { recorder in
      guard recorder.storage.lastDeviceState != state else { return }
      recorder.storage.lastDeviceState = state
      recorder.storage.deviceSequence += 1
      var fields = [String(recorder.storage.deviceSequence)] + RoboMemoHostClock.csvFields(hostTime)
      // DeviceState has no SDK timestamp in SDK 1.0.1; intentionally empty.
      fields += ["", state.batteryLevel.map(String.init) ?? "", String(describing: state.chargingState),
                 String(describing: state.donState), String(describing: state.hingeState),
                 String(describing: state.thermalLevel), String(describing: state.linkState),
                 String(describing: state.compatibility)]
      if recorder.writeCSV("device_state.csv", fields: fields, module: "deviceState") {
        recorder.gate.withLock { $0.deviceStateChanges += 1 }
      }
    }
  }

  /// Record requested/started/failed/skipped states and every sampling-rate attempt.
  /// Failures stay in the event history even if a subsequent fallback succeeds.
  func setModule(_ name: String, status: String, reason: String? = nil, rateHz: Int? = nil) {
    let hostTime = RoboMemoHostClock.now()
    enqueueControl { recorder in
      var module: [String: Any] = ["status": status]
      if let reason { module["reason"] = reason }
      if let rateHz { module["configuredRateHz"] = rateHz }
      recorder.storage.modules[name] = module
      var event: [String: Any] = ["name": "module_status", "module": name, "status": status,
                                  "hostTime": RoboMemoHostClock.json(hostTime)]
      if let reason { event["reason"] = reason }
      if let rateHz { event["rateHz"] = rateHz }
      recorder.storage.events.append(event)
      recorder.writeManifest(status: "recording", endTime: nil)
    }
  }

  func recordEvent(_ name: String, hostTime: CMTime = RoboMemoHostClock.now(), details: [String: String] = [:]) {
    enqueueControl { recorder in
      recorder.storage.events.append(["name": name, "hostTime": RoboMemoHostClock.json(hostTime), "details": details])
      recorder.writeManifest(status: "recording", endTime: nil)
    }
  }

  func markSyncTestStart(hostTime: CMTime = RoboMemoHostClock.now()) {
    enqueueControl { recorder in
      guard recorder.syncTest, recorder.storage.syncStart == nil else { return }
      recorder.storage.syncStart = hostTime
      recorder.storage.events.append(["name": "sync_test_start", "hostTime": RoboMemoHostClock.json(hostTime)])
      recorder.writeManifest(status: "recording", endTime: nil)
    }
  }

  func markSyncTestEnd(hostTime: CMTime = RoboMemoHostClock.now(), reason: String) {
    enqueueControl { recorder in
      guard recorder.storage.syncStart != nil, recorder.storage.syncEnd == nil else { return }
      recorder.storage.syncEnd = hostTime
      recorder.storage.syncEndReason = reason
      recorder.storage.events.append(["name": "sync_test_end", "reason": reason, "hostTime": RoboMemoHostClock.json(hostTime)])
      recorder.writeManifest(status: "recording", endTime: nil)
    }
  }

  func setVideoMetadata(_ data: Data) {
    enqueueControl { recorder in
      do {
        guard let metadata = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
          throw Failure.message("Video diagnostics were not a JSON object.")
        }
        recorder.storage.videoMetadata = metadata
      } catch { recorder.noteError(module: "videoDiagnostics", reason: error.localizedDescription) }
    }
  }

  /// Stop accepting sensor rows before camera teardown; final metadata is still
  /// accepted until finish, so video diagnostics can arrive after AVAssetWriter.
  func stopAcceptingSamples() { gate.withLock { $0.accepting = false } }

  /// Drain already accepted work, fix the WAV header, close all handles and write
  /// the final manifest. Optional-module failures never delete/throw away video.
  func finish(video: RoboMemoVideoClip?, error: String?) async -> String? {
    let endTime = RoboMemoHostClock.now()
    return await withCheckedContinuation { continuation in
      gate.withLock { state in
        state.accepting = false
        if state.finishing {
          queue.async { continuation.resume(returning: self.storage.finalManifestError) }
          return
        }
        state.finishing = true
        queue.async {
          let s = self.storage
          if s.syncStart != nil, s.syncEnd == nil {
            s.syncEnd = endTime
            s.syncEndReason = "recording_stopped"
            s.events.append(["name": "sync_test_end", "reason": "recording_stopped",
                             "hostTime": RoboMemoHostClock.json(endTime)])
          }
          if let video {
            s.modules["video"] = ["status": "complete", "durationSeconds": video.duration, "bytes": video.bytes]
          } else {
            s.modules["video"] = ["status": "failed", "reason": error ?? "No completed video."]
          }
          if let error { s.events.append(["name": "recording_end_reason", "reason": error,
                                          "hostTime": RoboMemoHostClock.json(endTime)]) }
          if let wav = s.files["audio.wav"] {
            do {
              try wav.seek(toOffset: 0)
              try wav.write(contentsOf: Self.wavHeader(bytes: s.audioBytes, rate: s.audioRate, channels: s.audioChannels))
            } catch { self.noteError(module: "audio", reason: "WAV finalization: \(error.localizedDescription)") }
          }
          for (file, handle) in s.files {
            do { try handle.synchronize(); try handle.close() }
            catch { self.noteError(module: file == "audio.wav" ? "audio" : "storage", reason: "\(file): \(error.localizedDescription)") }
          }
          s.files.removeAll()
          s.motionCoverage = self.summarizeMotionCoverage()
          let counts = self.snapshot
          for (name, count) in [("audio", counts.audioChunks), ("motion", counts.motionSamples), ("deviceState", counts.deviceStateChanges)] {
            let prior = s.modules[name]?["status"] as? String ?? "pending"
            if s.errors.contains(where: { $0["module"] == name }) {
              s.modules[name, default: [:]]["status"] = "failed"
              s.modules[name, default: [:]]["reason"] = s.errors.last(where: { $0["module"] == name })?["reason"]
            } else if !["failed", "unavailable", "denied", "skipped", "disabled"].contains(prior) {
              if self.gate.withLock({ $0.drops[name, default: 0] }) > 0 {
                s.modules[name, default: [:]]["status"] = "partial"
                s.modules[name, default: [:]]["reason"] = "Some input jobs were dropped; see queue.droppedByModule."
              } else {
                s.modules[name, default: [:]]["status"] = count > 0 ? "complete" : "no_data"
                if count == 0 { s.modules[name, default: [:]]["reason"] = "No samples were written before stop." }
              }
            }
            s.modules[name, default: [:]]["writtenRows"] = count
          }
          self.writeManifest(status: video == nil ? "failed" : "completed", endTime: endTime)
          continuation.resume(returning: s.finalManifestError)
        }
      }
    }
  }

  private func enqueueSample(module: String, _ body: @escaping @Sendable (RoboMemoSensorRecorder) -> Void) {
    // Submission and finish are ordered under one lock; a copied PCM chunk cannot
    // race behind the finalization job and write into a closed/reused session.
    gate.withLock { state in
      guard state.accepting, !state.finishing else { return }
      guard state.queued < Self.maximumQueuedJobs else {
        state.drops[module, default: 0] += 1
        state.queueOverflowDrops[module, default: 0] += 1
        return
      }
      state.queued += 1
      state.peakQueued = max(state.peakQueued, state.queued)
      let submittedAt = RoboMemoHostClock.now()
      queue.async {
        self.storage.maximumQueueResidenceSeconds = max(self.storage.maximumQueueResidenceSeconds,
          Self.positiveInterval(RoboMemoHostClock.now(), since: submittedAt))
        body(self)
        self.gate.withLock { $0.queued -= 1 }
      }
    }
  }

  private func enqueueControl(_ body: @escaping @Sendable (RoboMemoSensorRecorder) -> Void) {
    gate.withLock { state in
      guard !state.finishing else { return }
      queue.async { body(self) }
    }
  }

  private func rejectAudio(_ buffer: AVAudioPCMBuffer, sourcePTS: CMTime, hostTime: CMTime, reason: String) {
    let rate = buffer.format.sampleRate
    let channels = buffer.format.channelCount
    let frames = buffer.frameLength
    gate.withLock { state in
      guard state.accepting, !state.finishing else { return }
      state.drops["audio", default: 0] += 1
      state.invalidAudioBlocks += 1
      // Invalid PCM can repeat at frame rate. Queue each error category once.
      guard state.reportedInputErrors.insert(reason).inserted else { return }
      queue.async {
        self.noteError(module: "audio", reason: reason)
        self.writeManifest(status: "recording", endTime: nil)
      }
    }
    // The payload was rejected, but preserve the original block timing when the
    // bounded diagnostics queue has room. No SDK PCM buffer escapes this call.
    enqueueSample(module: "audioDiagnostics") { recorder in
      recorder.writeAudioDiagnostic(sourcePTS: sourcePTS, hostTime: hostTime,
        rate: rate, channels: channels, frames: frames, result: "not_written_invalid_pcm")
    }
  }

  private func writeAudioDiagnostic(sourcePTS: CMTime, hostTime: CMTime, rate: Double,
                                    channels: AVAudioChannelCount, frames: AVAudioFrameCount, result: String) {
    let s = storage
    s.audioSequence += 1
    let sampleOffset = UInt64(s.audioBytes) / UInt64(max(1, s.audioChannels) * 2)
    let fields = [String(s.audioSequence)] + RoboMemoHostClock.csvFields(hostTime)
      + RoboMemoHostClock.csvFields(sourcePTS)
      + [String(rate), String(channels), String(frames), String(sampleOffset), String(UInt64(s.audioBytes) + 44), result]
    writeCSV("audio_chunks.csv", fields: fields, module: "audio")
  }

  private func createFile(_ name: String, data: Data, module: String) {
    do {
      let url = directory.appendingPathComponent(name)
      try data.write(to: url, options: .atomic)
      let handle = try FileHandle(forUpdating: url)
      try handle.seekToEnd()
      storage.files[name] = handle
    } catch { noteError(module: module, reason: "Create \(name): \(error.localizedDescription)") }
  }

  @discardableResult
  private func writeCSV(_ name: String, fields: [String], module: String) -> Bool {
    guard let handle = storage.files[name] else { return false }
    do {
      let line = fields.map(Self.csvEscape).joined(separator: ",") + "\n"
      try handle.write(contentsOf: Data(line.utf8))
      return true
    } catch {
      noteError(module: module, reason: "Write \(name): \(error.localizedDescription)")
      try? handle.close()
      storage.files[name] = nil
      return false
    }
  }

  private func writeAudio(_ buffer: AVAudioPCMBuffer, sourcePTS: CMTime, hostTime: CMTime) {
    let s = storage
    s.audioSequence += 1
    let sampleRate = buffer.format.sampleRate
    let channels = buffer.format.channelCount
    var result = "written"
    let sampleOffset = UInt64(s.audioBytes) / UInt64(max(1, s.audioChannels) * 2)
    let byteOffset = UInt64(s.audioBytes) + 44
    do {
      guard sampleRate.isFinite, (1_000...192_000).contains(sampleRate), (1...8).contains(channels),
        sampleRate.rounded() == sampleRate else { throw Failure.message("Unsupported PCM rate/channel layout.") }
      if !s.audioHasFormat {
        s.audioRate = UInt32(sampleRate)
        s.audioChannels = UInt16(channels)
        s.audioInputFormat = String(describing: buffer.format.commonFormat)
        s.audioHasFormat = true
      }
      guard s.audioRate == UInt32(sampleRate), s.audioChannels == UInt16(channels) else {
        throw Failure.message("PCM sample rate/channel layout changed; subsequent block not written.")
      }
      guard !s.audioWriteFailed else { throw Failure.message("WAV writing failed earlier; block not written.") }
      let pcm = try Self.pcm16(buffer)
      guard UInt64(s.audioBytes) + UInt64(pcm.count) <= UInt64(UInt32.max) - 36 else {
        throw Failure.message("WAV reached RIFF size limit.")
      }
      guard let wav = s.files["audio.wav"] else { throw Failure.message("WAV file is unavailable.") }
      do {
        try wav.write(contentsOf: pcm)
      } catch {
        s.audioWriteFailed = true
        // Keep only successfully counted bytes if a partial system write failed.
        try? wav.truncate(atOffset: byteOffset)
        try? wav.seek(toOffset: byteOffset)
        throw error
      }
      s.audioBytes += UInt32(pcm.count)
      gate.withLock { $0.audioChunks += 1 }
      observeAudioTiming(sourcePTS: sourcePTS, hostTime: hostTime,
        frames: buffer.frameLength, sampleRate: Int32(sampleRate))
    } catch {
      result = "not_written"
      noteError(module: "audio", reason: error.localizedDescription)
    }
    let fields = [String(s.audioSequence)] + RoboMemoHostClock.csvFields(hostTime)
      + RoboMemoHostClock.csvFields(sourcePTS)
      + [String(sampleRate), String(channels), String(buffer.frameLength), String(sampleOffset), String(byteOffset), result]
    writeCSV("audio_chunks.csv", fields: fields, module: "audio")
  }

  private func observeAudioTiming(sourcePTS: CMTime, hostTime: CMTime,
                                  frames: AVAudioFrameCount, sampleRate: Int32) {
    let s = storage
    if let previous = s.audioLastHostTime {
      s.audioMaximumHostDeliveryGapSeconds = max(s.audioMaximumHostDeliveryGapSeconds,
        Self.positiveInterval(hostTime, since: previous))
    }
    s.audioLastHostTime = hostTime
    guard sourcePTS.isNumeric, sourcePTS.timescale > 0 else {
      s.audioSourcePTSInvalidCount += 1
      s.audioLastPTS = nil
      return
    }
    if let previous = s.audioLastPTS {
      if sourcePTS.epoch != previous.epoch {
        s.audioSourcePTSEpochChanges += 1
      } else {
        let delta = CMTimeSubtract(sourcePTS, previous)
        if !delta.isNumeric || !delta.seconds.isFinite {
          s.audioSourcePTSInvalidCount += 1
        } else if CMTimeCompare(delta, .zero) <= 0 {
          s.audioSourcePTSNonIncreasingCount += 1
        } else {
          // Subtract integer-rational timestamps before converting to seconds.
          // This avoids subtracting large Double epochs at the 2 ms boundary.
          let residual = CMTimeSubtract(delta, s.audioPreviousDuration)
          let magnitude = CMTimeCompare(residual, .zero) < 0
            ? CMTimeMultiply(residual, multiplier: -1) : residual
          let tolerance = CMTimeMaximum(CMTime(value: 2, timescale: 1_000), CMTime(value: 2, timescale: sampleRate))
          if magnitude.isNumeric, magnitude.seconds.isFinite {
            if CMTimeCompare(magnitude, tolerance) > 0 { s.audioTimestampDiscontinuities += 1 }
            s.audioMaximumSourceResidualSeconds = max(s.audioMaximumSourceResidualSeconds, magnitude.seconds)
            // A larger positive residual is a possible source-time coverage gap,
            // not proof of lost audio. Jitter is deliberately counted separately.
            let gapThreshold = CMTimeMaximum(tolerance, CMTimeMultiplyByRatio(s.audioPreviousDuration, multiplier: 1, divisor: 2))
            if CMTimeCompare(residual, gapThreshold) > 0 { s.audioPossibleSourceGapCount += 1 }
          } else { s.audioSourcePTSInvalidCount += 1 }
        }
      }
    }
    s.audioLastPTS = sourcePTS
    s.audioPreviousDuration = CMTime(value: Int64(frames), timescale: sampleRate)
  }

  /// Called once after draining the queue. Preserve received order in motion.csv;
  /// sorting here prevents late packets being mistaken for permanently missing data.
  private func summarizeMotionCoverage() -> [String: Any] {
    let rows = storage.motionTimestamps.sorted { $0.timestampNs < $1.timestampNs }
    var duplicates = 0, gaps = 0
    var maximumGap: Double = 0
    var largestGap: [String: Any] = [:]
    var previous: (timestampNs: Int64, rateHz: Int)?
    for row in rows {
      if let before = previous {
        if row.timestampNs == before.timestampNs { duplicates += 1; continue }
        let (delta, overflow) = row.timestampNs.subtractingReportingOverflow(before.timestampNs)
        // At a rate transition, use the slower nominal rate for the threshold.
        let thresholdNs = 2.5 * 1_000_000_000 / Double(max(1, min(row.rateHz, before.rateHz)))
        if overflow || Double(delta) > thresholdNs {
          gaps += 1
          let seconds = overflow ? (Double(row.timestampNs) - Double(before.timestampNs)) / 1e9 : Double(delta) / 1e9
          if seconds > maximumGap {
            maximumGap = seconds
            largestGap = ["startTimestampNs": before.timestampNs, "endTimestampNs": row.timestampNs,
                          "durationSeconds": seconds]
          }
        }
      }
      previous = row
    }
    return ["sortedUniqueGapCount": gaps, "maximumSortedUniqueGapSeconds": maximumGap,
            "largestSortedUniqueGap": largestGap, "duplicateTimestampCount": duplicates,
            "summarizedRows": rows.count, "omittedRows": storage.motionTimestampSummaryOmittedRows,
            "complete": storage.motionTimestampSummaryOmittedRows == 0]
  }

  private static func positiveInterval(_ time: CMTime, since previous: CMTime) -> Double {
    guard time.isNumeric, previous.isNumeric, time.epoch == previous.epoch else { return 0 }
    let seconds = CMTimeSubtract(time, previous).seconds
    return seconds.isFinite ? max(0, seconds) : 0
  }

  private func noteError(module: String, reason: String) {
    // Keep bounded repeated failures, with total count available separately.
    if storage.errors.count < 100 { storage.errors.append(["module": module, "reason": reason]) }
    storage.modules[module, default: [:]]["status"] = "failed"
    storage.modules[module, default: [:]]["reason"] = reason
    gate.withLock { $0.errors += 1 }
  }

  private func writeManifest(status: String, endTime: CMTime?) {
    let s = storage
    let counts = snapshot
    var motionDiagnostics = s.motionCoverage ?? ["summaryStatus": "available_after_finish"]
    motionDiagnostics["outOfOrderArrivalCount"] = s.motionOutOfOrderArrivalCount
    motionDiagnostics["maximumHostDeliveryGapSeconds"] = s.motionMaximumHostDeliveryGapSeconds
    motionDiagnostics["gapDefinition"] = "Sorted unique successfully written SDK timestamps: interval > 2.5 nominal sample periods; slower requested rate used at rate transitions. No missing-sample count inferred."
    motionDiagnostics["arrivalMeaning"] = "AsyncStream consumer receipt on phone; includes upstream buffering and consumer scheduling. Does not identify the cause of a gap."
    motionDiagnostics["maximumSummaryRows"] = Self.maximumMotionDiagnosticSamples
    motionDiagnostics["legacyDiscontinuityMeaning"] = "counts.motionTimestampDiscontinuities counts adjacent received-order non-increasing timestamps or intervals > 2.5 nominal periods; not a loss count."
    var sync: [String: Any] = ["enabled": syncTest,
                              "instruction": requestedAudioEnabled ? "拍手 3 次、快速左右甩头 3 次" : "在舒适范围内左右转头 3 轮",
                              "status": !syncTest ? "disabled" : s.syncStart == nil ? "not_started" : s.syncEnd == nil ? "active" : "ended"]
    if let time = s.syncStart { sync["startHostTime"] = RoboMemoHostClock.json(time) }
    if let time = s.syncEnd { sync["endHostTime"] = RoboMemoHostClock.json(time) }
    if let reason = s.syncEndReason { sync["endReason"] = reason }
    var manifest: [String: Any] = [
      "schemaVersion": 1, "sdkVersion": "1.0.1", "mode": "multisensor_experimental",
      "status": status, "createdAtUTC": ISO8601DateFormatter().string(from: createdAt),
      "startHostTime": RoboMemoHostClock.json(createdHostTime), "modules": s.modules,
      "configuration": ["requestedResolution": "high", "requestedVideoFPS": 7, "videoInputCodec": "raw",
                        "videoOutputCodec": "h264", "requestedAudioEnabled": requestedAudioEnabled,
                        "requestedAudioSampleRate": requestedAudioEnabled ? (16_000 as Any) : NSNull(),
                        "requestedAudioChannels": requestedAudioEnabled ? (1 as Any) : NSNull(),
                        "audioTransport": requestedAudioEnabled ? "camera_stream_pcm" : "disabled_by_user", "appBeamforming": false,
                        "deviceAudioProcessing": requestedAudioEnabled
                          ? "PCM as delivered by SDK; no device DSP control applied" : "Audio not requested",
                        "requestedMotionRateHz": requestedMotionRateHz,
                        "motionFallbackRatesHz": [60, 30, 24, 15, 10, 5].filter { $0 <= requestedMotionRateHz }],
      "clock": ["host": "CMClockGetHostTimeClock", "hostMeaning": "phone arrival at SDK callback or AsyncStream consumer; monotonic, not wall time",
                "videoSDK": "CMSampleBuffer presentation timestamp; retain raw CMTime",
                "audioSDK": "AudioFrame.presentationTimeStamp; retain raw CMTime",
                "motionSDK": "MotionSample.timestampNs; SDK monotonic nanoseconds",
                "deviceStateSDK": "not exposed; CSV field empty",
                "videoAudioClock": "glasses clock, not phone host clock; audio capture/codec delay requires measurement",
                "motionToMediaClock": "common origin/rate mapping not confirmed; do not subtract directly",
                "automaticClockOffsetApplied": false],
      "audio": ["wavEncoding": "PCM signed 16-bit little endian, interleaved",
                "actualSampleRate": s.audioHasFormat ? (s.audioRate as Any) : NSNull(),
                "actualChannels": s.audioHasFormat ? (s.audioChannels as Any) : NSNull(),
                "wavHeaderSampleRate": s.audioRate, "wavHeaderChannels": s.audioChannels,
                "inputPCMFormat": s.audioInputFormat ?? "no_data",
                "dataBytes": s.audioBytes, "placeholderOnly": s.audioBytes == 0,
                "concatenation": "received blocks only; no inserted silence or guessed timing correction",
                "ignoredEmptyBlocks": gate.withLock { $0.ignoredEmptyAudioBlocks },
                "invalidPCMBlocks": gate.withLock { $0.invalidAudioBlocks },
                "timestampDiscontinuities": s.audioTimestampDiscontinuities,
                "timestampJitterCount": s.audioTimestampDiscontinuities,
                "sourcePTSInvalidCount": s.audioSourcePTSInvalidCount,
                "sourcePTSNonIncreasingCount": s.audioSourcePTSNonIncreasingCount,
                "sourcePTSEpochChanges": s.audioSourcePTSEpochChanges,
                "possibleSourceGapCount": s.audioPossibleSourceGapCount,
                "maximumSourceResidualSeconds": s.audioMaximumSourceResidualSeconds,
                "maximumHostDeliveryGapSeconds": s.audioMaximumHostDeliveryGapSeconds,
                "timingDefinition": "Successfully written nonempty blocks only. Residual = source PTS delta minus previous PCM duration, computed with CMTime. Jitter = absolute residual > max(2 ms, 2 samples). Possible gap = positive residual > max(jitter tolerance, half previous chunk duration). Neither counter proves lost audio; timestampDiscontinuities is a compatibility alias for jitter.",
                "emptyBlockMeaning": "Zero-frame callback preserved as ignored_empty; no PCM bytes to write; not counted as an audio failure or dropped audio."],
      "motion": motionDiagnostics,
      "counts": ["audioChunksWritten": counts.audioChunks, "motionSamplesWritten": counts.motionSamples,
                 "deviceStateRowsWritten": counts.deviceStateChanges, "sidecarErrors": counts.errors,
                 "motionTimestampDiscontinuities": s.motionTimestampDiscontinuities],
      "queue": ["maximumPendingDataJobs": Self.maximumQueuedJobs, "maximumAudioChunkBytes": Self.maximumAudioChunkBytes,
                "droppedByModule": gate.withLock { $0.drops },
                "overflowDroppedByModule": gate.withLock { $0.queueOverflowDrops },
                "droppedByModuleMeaning": "Compatibility total: queue overflows plus rejected invalid PCM blocks. See overflowDroppedByModule and audio.invalidPCMBlocks separately.",
                "peakPendingDataJobs": gate.withLock { $0.peakQueued },
                "maximumResidenceSeconds": s.maximumQueueResidenceSeconds,
                "residenceMeaning": "App sidecar queue submission to execution; excludes SDK buffers, transport, and AsyncStream delivery."],
      "syncTest": sync, "events": s.events, "errors": s.errors,
      "files": ["video.mov", "frames.csv", "audio.wav", "audio_chunks.csv", "motion.csv", "device_state.csv", "session.json"],
      "experimental": ["camera_stream_audio", "motion"], "publishableWithExperimentalFeatures": false,
    ]
    if let endTime { manifest["endHostTime"] = RoboMemoHostClock.json(endTime) }
    manifest["mediaFilesPresent"] = ["video.mov", "frames.csv", "audio.wav", "audio_chunks.csv", "motion.csv", "device_state.csv"]
      .filter { FileManager.default.fileExists(atPath: directory.appendingPathComponent($0).path) }
    if let metadata = s.videoMetadata { manifest["videoDiagnostics"] = metadata }
    do {
      let data = try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys])
      try data.write(to: directory.appendingPathComponent("session.json"), options: .atomic)
      s.finalManifestError = nil
    } catch {
      s.finalManifestError = "Could not save session.json: \(error.localizedDescription)"
    }
  }

  private static func vectorFields(_ v: Vector3?) -> [String] {
    guard let v else { return ["", "", ""] }
    return [number(v.x), number(v.y), number(v.z)]
  }
  private static func number(_ value: Float) -> String { value.isFinite ? String(value) : "" }
  private static func csvEscape(_ field: String) -> String {
    field.contains(",") || field.contains("\"") || field.contains("\n") || field.contains("\r")
      ? "\"" + field.replacingOccurrences(of: "\"", with: "\"\"") + "\"" : field
  }

  private static func pcm16(_ buffer: AVAudioPCMBuffer) throws -> Data {
    let channels = Int(buffer.format.channelCount), frames = Int(buffer.frameLength)
    let interleaved = buffer.format.isInterleaved
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
    guard buffers.count >= (interleaved ? 1 : channels) else { throw Failure.message("Missing PCM channels.") }
    let bytesPerSample: Int
    switch buffer.format.commonFormat {
    case .pcmFormatInt16: bytesPerSample = 2
    case .pcmFormatInt32, .pcmFormatFloat32: bytesPerSample = 4
    case .pcmFormatFloat64: bytesPerSample = 8
    default: throw Failure.message("Unsupported PCM representation.")
    }
    for channel in 0..<(interleaved ? 1 : channels) {
      guard buffers[channel].mData != nil,
        Int(buffers[channel].mDataByteSize) >= frames * bytesPerSample * (interleaved ? channels : 1)
      else { throw Failure.message("PCM sample storage is shorter than frameLength.") }
    }
    var output = Data(capacity: frames * channels * 2)
    for frame in 0..<frames {
      for channel in 0..<channels {
        let raw = buffers[interleaved ? 0 : channel].mData!
        let index = interleaved ? frame * channels + channel : frame
        let sample: Int16
        switch buffer.format.commonFormat {
        case .pcmFormatInt16: sample = raw.assumingMemoryBound(to: Int16.self)[index]
        case .pcmFormatInt32: sample = Int16(clamping: raw.assumingMemoryBound(to: Int32.self)[index] >> 16)
        case .pcmFormatFloat32:
          sample = floatToInt16(Double(raw.assumingMemoryBound(to: Float.self)[index]))
        case .pcmFormatFloat64: sample = floatToInt16(raw.assumingMemoryBound(to: Double.self)[index])
        default: throw Failure.message("Unsupported PCM representation.")
        }
        var littleEndian = sample.littleEndian
        withUnsafeBytes(of: &littleEndian) { output.append(contentsOf: $0) }
      }
    }
    return output
  }

  private static func floatToInt16(_ value: Double) -> Int16 {
    guard value.isFinite else { return 0 }
    return Int16(clamping: Int((min(1, max(-1, value)) * 32_768).rounded()))
  }

  private static func wavHeader(bytes: UInt32, rate: UInt32, channels: UInt16) -> Data {
    var data = Data()
    func text(_ string: String) { data.append(contentsOf: string.utf8) }
    func u16(_ value: UInt16) { var x = value.littleEndian; withUnsafeBytes(of: &x) { data.append(contentsOf: $0) } }
    func u32(_ value: UInt32) { var x = value.littleEndian; withUnsafeBytes(of: &x) { data.append(contentsOf: $0) } }
    text("RIFF"); u32(36 + bytes); text("WAVEfmt "); u32(16); u16(1); u16(channels)
    u32(rate); u32(rate * UInt32(channels) * 2); u16(channels * 2); u16(16); text("data"); u32(bytes)
    return data
  }

  private enum Failure: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let message) = self { message } else { nil } }
  }
}
