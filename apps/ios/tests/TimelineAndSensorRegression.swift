import AVFoundation
import Foundation
import MWDATMotion

extension MultiSensorValidation {
  static func audioDisabledPersistence(_ out: URL) async throws {
    let sensor = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "audio-off-video-motion"),
                                            syncTest: true, requestedMotionRateHz: 30, requestedAudioEnabled: false)
    let video = RoboMemoVideoRecorder(directory: sensor.directory, requestedResolution: "high", frameRate: 7,
      sessionLayout: true, metadataSink: { sensor.setVideoMetadata($0) })
    sensor.appendAudio(try makeAudio([1,2,3,4]), sourcePTS: .zero, hostTime: RoboMemoHostClock.now())
    // Even malformed input is ignored before validation when audio was not requested.
    sensor.appendAudio(try makeAudio(Array(repeating: 1, count: 131073)), sourcePTS: .zero,
                       hostTime: RoboMemoHostClock.now())
    sensor.appendMotion(MotionSample(timestampNs: 1_000_000_000,
      accelerometer: Vector3(x: 0,y: 0,z: 9.8), source: .glasses),
      hostTime: RoboMemoHostClock.now(), requestedRateHz: 30)
    sensor.markSyncTestStart(hostTime: RoboMemoHostClock.now())
    sensor.markSyncTestEnd(hostTime: RoboMemoHostClock.now(), reason: "user_finished")
    let clip = try await writeVideo(video)
    let error = await sensor.finish(video: clip, error: nil)
    try require(error == nil && sensor.snapshot.audioChunks == 0 && sensor.snapshot.motionSamples == 1,
                "audio OFF retains a playable video and actual Motion samples")
    try require(sensor.snapshot.errors == 0 && sensor.snapshot.droppedJobs == 0,
                "unrequested PCM creates neither input errors nor fake audio drops")
    let names = Set(try FileManager.default.contentsOfDirectory(atPath: sensor.directory.path))
    try require(names == Set(["video.mov","frames.csv","audio.wav","audio_chunks.csv","motion.csv","device_state.csv","session.json"]),
                "audio OFF preserves the seven-file export contract")
    let wave = try readWave(sensor.directory.appendingPathComponent("audio.wav"))
    let wavBytes = try Data(contentsOf: sensor.directory.appendingPathComponent("audio.wav"))
    try require(wave.payload.isEmpty && wavBytes.count == 44,
                "audio OFF stores only a valid empty WAV header without silent fake samples")
    let audioRows = try readCSV(sensor.directory.appendingPathComponent("audio_chunks.csv"))
    try require(audioRows.isEmpty,
                "audio OFF leaves an audio CSV header with no invented received chunks")
    let json = try readJSON(sensor.directory.appendingPathComponent("session.json"))
    let config = json["configuration"] as! [String: Any]
    let modules = json["modules"] as! [String: [String: Any]]
    let audio = json["audio"] as! [String: Any]
    try require(config["requestedAudioEnabled"] as? Bool == false,
                "audio opt-out is frozen explicitly into session configuration")
    try require(modules["audio"]?["status"] as? String == "disabled"
      && modules["audio"]?["reason"] as? String == "user_disabled"
      && modules["audio"]?["writtenRows"] as? Int == 0,
                "intentionally absent audio is disabled rather than failed or no_data")
    try require(audio["actualSampleRate"] is NSNull && audio["actualChannels"] is NSNull
      && audio["placeholderOnly"] as? Bool == true,
                "empty audio placeholder is not reported as an observed microphone format")
    try require(modules["video"]?["status"] as? String == "complete"
      && modules["motion"]?["status"] as? String == "complete",
                "audio opt-out does not downgrade completed video or Motion modules")
  }

  static func repairedAudio(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "empty-audio-recovery"), syncTest: false)
    let empty = AVAudioPCMBuffer(pcmFormat: AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!, frameCapacity: 32)!
    empty.frameLength = 0
    recorder.appendAudio(empty, sourcePTS: .zero, hostTime: CMTime(value: 100, timescale: 1))
    let payload = Array(repeating: Int16(31), count: 1024)
    recorder.appendAudio(try makeAudio(payload), sourcePTS: CMTime(value: 1000000, timescale: 1000000),
                         hostTime: CMTime(value: 100064, timescale: 1000))
    _ = await recorder.finish(video: nil, error: "test_no_video")
    let rows = try readCSV(recorder.directory.appendingPathComponent("audio_chunks.csv"))
    try require(rows.count == 2 && rows[0]["result"] == "ignored_empty" && rows[1]["result"] == "written",
                "empty first audio callback is diagnosed then valid PCM writes normally")
    let json = try readJSON(recorder.directory.appendingPathComponent("session.json"))
    let modules = json["modules"] as! [String: [String: Any]]
    let audio = json["audio"] as! [String: Any]
    let queue = json["queue"] as! [String: Any]
    let drops = queue["droppedByModule"] as! [String: Int]
    try require(modules["audio"]?["status"] as? String == "complete", "empty first audio callback does not mark valid audio failed")
    try require(audio["ignoredEmptyBlocks"] as? Int == 1 && (drops["audio"] ?? 0) == 0,
                "empty audio has its own counter without pretending samples were lost")
    let wave = try readWave(recorder.directory.appendingPathComponent("audio.wav"))
    try require(wave.payload.count == payload.count * 2, "ignored empty audio does not insert silence")

    let invalid = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "oversized-audio"), syncTest: false)
    invalid.appendAudio(try makeAudio(Array(repeating: 1, count: 131073)), sourcePTS: .zero, hostTime: RoboMemoHostClock.now())
    invalid.appendAudio(try makeAudio(payload), sourcePTS: CMTime(value: 1, timescale: 1), hostTime: RoboMemoHostClock.now())
    _ = await invalid.finish(video: nil, error: "test_no_video")
    let invalidJSON = try readJSON(invalid.directory.appendingPathComponent("session.json"))
    let invalidModules = invalidJSON["modules"] as! [String: [String: Any]]
    try require(invalidModules["audio"]?["status"] as? String == "failed", "oversized positive-length PCM remains a real persistent error")
    let invalidRows = try readCSV(invalid.directory.appendingPathComponent("audio_chunks.csv"))
    try require(invalidRows.first?["result"] == "not_written_invalid_pcm", "real PCM rejection remains individually traceable")
  }

  static func sourceTimeline(_ out: URL) async throws {
    let directory = try makeParent(out, "source-timeline")
    let recorder = RoboMemoVideoRecorder(directory: directory, requestedResolution: "high", frameRate: 7, sessionLayout: true)
    // Deliberately unrelated phone-arrival spacing and SDK capture spacing.
    let source: [CMTime] = [CMTime(value: 100000000, timescale: 1000000),
                           CMTime(value: 100200000, timescale: 1000000),
                           CMTime(value: 100800000, timescale: 1000000)]
    recorder.markStreamStart()
    for (i, pts) in source.enumerated() {
      recorder.append(try makeVideoSample(index: i, sourcePTS: pts), hostTime: CMTime(value: 5000000 + Int64(i) * 1000, timescale: 1000000))
      try await Task.sleep(for: .milliseconds(55))
    }
    let wall = recorder.snapshot.elapsed
    let clip = try await recorder.finish()
    let frames = try await readMoviePTS(clip.url)
    try require(frames.count == 3, "all synthetic capture-time frames survive encoding")
    try require(zip(frames, [0.0, 0.2, 0.8]).allSatisfy { abs($0 - $1) < 0.00001 },
                "actual MOV retains SDK intervals despite compressed phone arrivals")
    try require(wall < 0.5 && clip.duration > 0.93 && clip.duration < 0.96,
                "wall recording timer stays independent and MOV ends after the final source frame")
    let json = try readJSON(directory.appendingPathComponent("video.json"))
    try require(json["timelineBasis"] as? String == "sdk_video_pts_relative_to_first_written_frame", "source timeline origin is explicit in metadata")
    try require(json["schemaVersion"] as? Int == 3, "multi-sensor timeline metadata increments schema version")
    let rows = try readCSV(directory.appendingPathComponent("frames.csv"))
    let origin = json["sourcePTSOrigin"] as! [String: Any]
    try require(origin["value"] as? Int == 100000000 && rows[1]["outputPTSValue"] != nil,
                "source origin and exact output rational times are exported")
  }

  static func sourceTimestampRejections(_ out: URL) async throws {
    let directory = try makeParent(out, "source-rejections")
    let recorder = RoboMemoVideoRecorder(directory: directory, requestedResolution: "high", frameRate: 7, sessionLayout: true)
    let pts: [CMTime] = [.invalid,
      CMTime(value: 100, timescale: 1),
      CMTime(value: 100, timescale: 1),
      CMTime(value: 99, timescale: 1),
      CMTime(value: 101, timescale: 1, flags: .valid, epoch: 1),
      CMTime(value: 1000, timescale: 1),
      CMTime(value: 101, timescale: 1)]
    for (i, time) in pts.enumerated() {
      recorder.append(try makeVideoSample(index: i, sourcePTS: time))
      try await Task.sleep(for: .milliseconds(55))
    }
    let clip = try await recorder.finish()
    let rows = try readCSV(directory.appendingPathComponent("frames.csv"))
    let results = rows.map { $0["result"]! }
    try require(results == ["dropped_invalid_source_pts", "written", "dropped_nonmonotonic_source_pts",
                            "dropped_nonmonotonic_source_pts", "dropped_source_epoch_change", "dropped_source_pts_ahead_of_wall", "written"],
                "invalid, duplicate, regressive and changed-epoch SDK timestamps have explicit rejection reasons")
    let times = try await readMoviePTS(clip.url)
    try require(times.count == 2 && abs(times[0]) < 0.00001 && abs(times[1] - 1) < 0.00001,
                "rejected timestamps do not corrupt earlier video or switch to a phone clock")
    let json = try readJSON(directory.appendingPathComponent("video.json"))
    try require(json["dropped"] as? Int == 5 && json["recordingStatus"] as? String == "completed",
                "rejected source times are counted while completed video is preserved")
  }

  static func sourceTimeDoesNotStopWallLimit(_ out: URL) async throws {
    let directory = try makeParent(out, "wall-limit")
    let recorder = RoboMemoVideoRecorder(directory: directory, requestedResolution: "high", frameRate: 7,
                                         maximumDuration: 0.25, sessionLayout: true)
    recorder.append(try makeVideoSample(index: 0, sourcePTS: CMTime(value: 1000, timescale: 10)))
    try await Task.sleep(for: .milliseconds(55))
    recorder.append(try makeVideoSample(index: 1, sourcePTS: CMTime(value: 1001, timescale: 10)))
    try await Task.sleep(for: .milliseconds(230))
    recorder.append(try makeVideoSample(index: 2, sourcePTS: CMTime(value: 10015, timescale: 100)))
    try require(recorder.snapshot.stopped && recorder.snapshot.frames == 2, "elapsed phone time stops recording even when source time stays below limit")
    let clip = try await recorder.finish()
    try require(clip.duration > 0.24 && clip.duration <= 0.251, "wall limit retains accepted source frame and bounded final duration")

    let sourceDirectory = try makeParent(out, "source-limit")
    let source = RoboMemoVideoRecorder(directory: sourceDirectory, requestedResolution: "high", frameRate: 7,
                                      maximumDuration: 0.5, sessionLayout: true)
    for (i, value) in [1000, 1003, 1006].enumerated() {
      source.append(try makeVideoSample(index: i, sourcePTS: CMTime(value: Int64(value), timescale: 10)))
      try await Task.sleep(for: .milliseconds(40))
    }
    try require(source.snapshot.stopped && source.snapshot.elapsed < 0.5 && source.snapshot.frames == 2,
                "source duration limit independently stops a compressed delivery burst")
    let sourceClip = try await source.finish()
    try require(sourceClip.duration > 0.43 && sourceClip.duration <= 0.5, "source limit preserves valid earlier movie frames")
    let rows = try readCSV(sourceDirectory.appendingPathComponent("frames.csv"))
    try require(rows.last?["result"] == "source_limit_reached", "source duration limit has its own diagnostic reason")
  }

  static func legacyTimelineIndependentOfSource(_ out: URL) async throws {
    let directory = try makeParent(out, "legacy-timeline")
    let recorder = RoboMemoVideoRecorder(directory: directory, requestedResolution: "high", frameRate: 7)
    for (i, time) in [CMTime(value: 10, timescale: 1), .invalid, CMTime(value: 5, timescale: 1)].enumerated() {
      recorder.append(try makeVideoSample(index: i, sourcePTS: time))
      try await Task.sleep(for: .milliseconds(70))
    }
    let clip = try await recorder.finish()
    let times = try await readMoviePTS(clip.url)
    try require(times.count == 3 && times[1] > 0.04 && times[2] > times[1] && times[2] < 0.5,
                "ordinary recording retains arrival timeline even with invalid/regressive SDK timestamps")
    let json = try readJSON(clip.url.deletingPathExtension().appendingPathExtension("json"))
    try require(json["schemaVersion"] as? Int == 2 && json["timelineBasis"] as? String == "host_arrival_elapsed",
                "ordinary recording preserves schema2 with explicitly documented original clock")
  }
}

extension MultiSensorValidation {
  static func timingDiagnostics(_ out: URL) async throws {
    let recorder = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "timing-diagnostics"), syncTest: false)
    let origin: Int64 = 9007199254740993
    let micros: [Int64] = [0, 64000, 130000, 196001, 326001, 320001]
    var timestamps = micros.map { CMTime(value: origin + $0 * 1000, timescale: 1000000000) }
    timestamps.append(.invalid)
    timestamps.append(CMTime(value: origin + 500000000, timescale: 1000000000))
    timestamps.append(CMTime(value: origin + 600000000, timescale: 1000000000, flags: .valid, epoch: 1))
    for (i, pts) in timestamps.enumerated() {
      recorder.appendAudio(try makeAudio(Array(repeating: 1, count: 1024)), sourcePTS: pts,
                           hostTime: CMTime(value: 100000 + Int64(i) * 10, timescale: 1000))
    }
    let motionPTS: [Int64] = [0, 16666667, 50000000, 33333333, 50000000, 133333333]
    let arrivals: [Int64] = [1000000, 1020000, 2500000, 2500100, 2500200, 2510000]
    for i in motionPTS.indices {
      recorder.appendMotion(MotionSample(timestampNs: origin + motionPTS[i], source: .glasses),
                            hostTime: CMTime(value: arrivals[i], timescale: 1000000), requestedRateHz: 60)
    }
    _ = await recorder.finish(video: nil, error: "test_no_video")
    let json = try readJSON(recorder.directory.appendingPathComponent("session.json"))
    let audio = json["audio"] as! [String: Any]
    try require(audio["timestampJitterCount"] as? Int == 2, "exact2 ms audio residual is not jitter but2.001 ms and66 ms are")
    try require(audio["possibleSourceGapCount"] as? Int == 1, "audio timing jitter is distinct from a possible coverage gap")
    try require(audio["sourcePTSNonIncreasingCount"] as? Int == 1 && audio["sourcePTSInvalidCount"] as? Int == 1
                && audio["sourcePTSEpochChanges"] as? Int == 1, "nonincreasing, invalid and changed-epoch audio clocks are independently counted")
    let motion = json["motion"] as! [String: Any]
    try require(motion["outOfOrderArrivalCount"] as? Int == 1 && motion["duplicateTimestampCount"] as? Int == 1,
                "IMU reordered and duplicate samples have separate counters")
    try require(motion["sortedUniqueGapCount"] as? Int == 1
                && abs((motion["maximumSortedUniqueGapSeconds"] as? Double ?? -1) - 0.083333333) < 1e-9,
                "IMU coverage gaps are measured after sorting and removing duplicates")
    try require(abs((motion["maximumHostDeliveryGapSeconds"] as? Double ?? -1) - 1.48) < 1e-9,
                "IMU host delivery stalls are separate from source-time coverage gaps")
    let rows = try readCSV(recorder.directory.appendingPathComponent("motion.csv"))
    try require(rows.map { Int64($0["timestampNs"]!)! } == motionPTS.map { origin + $0 },
                "IMU CSV preserves original arrival order instead of rewriting the observations")
    let queue = json["queue"] as! [String: Any]
    try require((queue["peakPendingDataJobs"] as? Int ?? 0) > 0
                && (queue["maximumResidenceSeconds"] as? Double ?? -1) >= 0,
                "app-side queue pressure diagnostics are recorded")
  }
}
