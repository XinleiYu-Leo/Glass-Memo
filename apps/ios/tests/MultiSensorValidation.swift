import AVFoundation
import Foundation
import MWDATCore
import MWDATMotion

@MainActor @main struct MultiSensorValidation {
  static func main() async {
    do {
      let out = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
      try await fullSession(out)
      try await auxiliaryFailure(out)
      try await formatChange(out)
      try await manifestWriteFailure(out)
      try await stopGate(out)
      try await invalidAndFloatAudio(out)
      try await emptySession(out)
      try await legacyVideo(out)
      try await repairedAudio(out)
      try await audioDisabledPersistence(out)
      try await timingDiagnostics(out)
      try await sourceTimeline(out)
      try await sourceTimestampRejections(out)
      try await sourceTimeDoesNotStopWallLimit(out)
      try await legacyTimelineIndependentOfSource(out)
      try memoryKeys(out)
      print("ALL MULTISENSOR PERSISTENCE CHECKS PASSED")
    } catch {
      print("FAIL \(error)")
      exit(1)
    }
  }

  static func makeParent(_ out: URL, _ name: String) throws -> URL {
    let url = out.appendingPathComponent(name, isDirectory: true)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
  }

  static func writeVideo(_ recorder: RoboMemoVideoRecorder) async throws -> RoboMemoVideoClip {
    recorder.markStreamStart()
    for index in 0..<4 {
      recorder.append(try makeVideoSample(index: index), hostTime: CMTime(value: 1234567890 + Int64(index), timescale: 1_000_000))
      try await Task.sleep(for: .milliseconds(145))
    }
    recorder.requestStop()
    let before = recorder.snapshot.frames
    recorder.append(try makeVideoSample(index: 99), hostTime: CMTime(value: 999, timescale: 1))
    try require(recorder.snapshot.frames == before && before > 0, "stop rejects late video callbacks")
    let clip = try await recorder.finish()
    try require(clip.bytes > 0 && clip.duration > 0, "actual synthetic MOV is playable")
    return clip
  }

  static func fullSession(_ out: URL) async throws {
    let parent = try makeParent(out, "full")
    let sensor = try RoboMemoSensorRecorder(parentDirectory: parent, syncTest: true)
    let video = RoboMemoVideoRecorder(directory: sensor.directory, requestedResolution: "high", frameRate: 7,
                                     sessionLayout: true, metadataSink: { sensor.setVideoMetadata($0) })
    let before = RoboMemoHostClock.now()
    let after = RoboMemoHostClock.now()
    try require(before.isValid && after >= before && before.seconds > 0, "shared host clock is valid and monotonic")
    sensor.setModule("audio", status: "starting", rateHz: 16000)
    sensor.setModule("motion", status: "starting", rateHz: 60)
    let original: [Int16] = [-32768, -24000, -1000, 0, 1, 1234, 20000, 32767]
    let buffer = try makeAudio(original)
    let sourcePTS = CMTime(value: 9007199254740993, timescale: 1_000_000_000, flags: .valid, epoch: 3)
    sensor.appendAudio(buffer, sourcePTS: sourcePTS, hostTime: CMTime(value: 321987654321, timescale: 1_000_000))
    // SDK may reuse its buffer as soon as the listener returns. The persisted
    // bytes must retain the snapshot taken by appendAudio, not this mutation.
    for i in original.indices { buffer.int16ChannelData![0][i] = 0 }
    sensor.appendAudio(try makeAudio(original), sourcePTS: .invalid,
                       hostTime: CMTime(value: 321987655321, timescale: 1_000_000))
    sensor.appendMotion(MotionSample(timestampNs: 9007199254740993,
      accelerometer: Vector3(x: 1.25, y: -2.5, z: 9.8), gyroscope: Vector3(x: -1, y: 2, z: 3),
      source: .glasses), hostTime: CMTime(value: 4000000001, timescale: 1_000_000), requestedRateHz: 60)
    sensor.appendMotion(MotionSample(timestampNs: 9007199254740994,
      orientation: Quaternion(x: 0, y: 0, z: 0, w: 1), source: .glasses),
      hostTime: CMTime(value: 4000016667, timescale: 1_000_000), requestedRateHz: 60)
    sensor.appendDeviceState(DeviceState(linkState: .connected, compatibility: .compatible,
      batteryLevel: 80, chargingState: .notCharging, donState: .donned, hingeState: .open, thermalLevel: .none),
      hostTime: CMTime(value: 5000000001, timescale: 1_000_000))
    sensor.appendDeviceState(DeviceState(linkState: .connected, compatibility: .compatible,
      batteryLevel: 79, chargingState: .charging, donState: .donned, hingeState: .open, thermalLevel: .light),
      hostTime: CMTime(value: 5000000002, timescale: 1_000_000))
    sensor.recordEvent("diagnostic,quoted", details: ["reason": "test \"quote\"\nnewline"])
    sensor.markSyncTestStart(hostTime: CMTime(value: 6000000001, timescale: 1_000_000))
    sensor.markSyncTestEnd(hostTime: CMTime(value: 6000000002, timescale: 1_000_000), reason: "completed")
    let clip = try await writeVideo(video)
    let finishError = await sensor.finish(video: clip, error: nil)
    try require(finishError == nil, "full session finalizes without error")
    let names = Set(try FileManager.default.contentsOfDirectory(atPath: sensor.directory.path))
    try require(names == Set(["video.mov", "frames.csv", "audio.wav", "audio_chunks.csv", "motion.csv", "device_state.csv", "session.json"]), "exact seven-file multi-sensor layout")
    let wave = try readWave(sensor.directory.appendingPathComponent("audio.wav"))
    try require(wave.format == 1 && wave.channels == 1 && wave.sampleRate == 16000 && wave.bits == 16, "WAV stores 16 kHz mono signed PCM")
    let expected = (original + original).flatMap { sample -> [UInt8] in
      let value = UInt16(bitPattern: sample); return [UInt8(truncatingIfNeeded: value), UInt8(truncatingIfNeeded: value >> 8)]
    }
    try require(wave.payload == Data(expected), "PCM bytes preserve every sample and copy SDK input immediately")
    let audio = try readCSV(sensor.directory.appendingPathComponent("audio_chunks.csv"))
    try require(audio.count == 2, "one timing row per audio chunk")
    try require(audio[0].values.contains("9007199254740993") && audio[0].values.contains("321987654321"), "audio SDK and phone raw clock integers remain exact beyond Double precision")
    try require(audio[1].values.contains("321987655321"), "invalid SDK audio timestamp still retains host arrival")
    let motion = try readCSV(sensor.directory.appendingPathComponent("motion.csv"))
    try require(motion.count == 2, "one timing row per motion sample")
    try require(motion[0].values.contains("9007199254740993") && motion[0].values.contains("4000000001"), "motion clocks keep exact raw integers")
    try require(motion[0].values.contains("1.25") && motion[0].values.contains("-2.5"), "accelerometer values retained")
    try require(motion[0].values.contains("") && motion[1].values.contains(""), "missing IMU components remain empty")
    let states = try readCSV(sensor.directory.appendingPathComponent("device_state.csv"))
    try require(states.count == 2 && states[0].values.contains("80") && states[1].values.contains("79"), "device state records both observed changes")
    let frames = try readCSV(sensor.directory.appendingPathComponent("frames.csv"))
    try require(frames.count == 4 && frames[0]["sourcePTSValue"] == "9007199254740993", "video diagnostics preserve raw source PTS")
    try require(frames[0].values.contains("1234567890"), "video diagnostics share phone host clock")
    let manifest = try readJSON(sensor.directory.appendingPathComponent("session.json"))
    let serialized = String(decoding: try JSONSerialization.data(withJSONObject: manifest, options: .sortedKeys), as: UTF8.self)
    try require(serialized.contains("1.0.1") && serialized.contains("6000000001") && serialized.contains("6000000002"), "manifest stores SDK version and sync test start/end clocks")
    let filesBefore = try Dictionary(uniqueKeysWithValues: names.map { ($0, try Data(contentsOf: sensor.directory.appendingPathComponent($0))) })
    sensor.appendAudio(try makeAudio(original), sourcePTS: .zero, hostTime: RoboMemoHostClock.now())
    sensor.appendMotion(MotionSample(timestampNs: 1), hostTime: RoboMemoHostClock.now(), requestedRateHz: 60)
    sensor.appendDeviceState(DeviceState(batteryLevel: 1), hostTime: RoboMemoHostClock.now())
    sensor.setModule("audio", status: "failed", reason: "late callback")
    sensor.recordEvent("late_event")
    try await Task.sleep(for: .milliseconds(100))
    for (name, data) in filesBefore {
      let current = try Data(contentsOf: sensor.directory.appendingPathComponent(name))
      try require(current == data, "finished \(name) rejects late callbacks")
    }
  }

  static func auxiliaryFailure(_ out: URL) async throws {
    let parent = try makeParent(out, "auxiliary-failure")
    let sensor = try RoboMemoSensorRecorder(parentDirectory: parent, syncTest: false)
    let video = RoboMemoVideoRecorder(directory: sensor.directory, requestedResolution: "high", frameRate: 7,
                                     sessionLayout: true, metadataSink: { sensor.setVideoMetadata($0) })
    sensor.setModule("audio", status: "failed", reason: "microphone_permission_denied")
    sensor.setModule("motion", status: "failed", reason: "sensorUnavailable after 60,30,24,15,10,5")
    let clip = try await writeVideo(video)
    let error = await sensor.finish(video: clip, error: nil)
    try require(error == nil && clip.bytes > 0, "auxiliary failure preserves completed video")
    let audio = try readWave(sensor.directory.appendingPathComponent("audio.wav"))
    try require(audio.payload.isEmpty, "unavailable audio leaves valid empty WAV")
    let serialized = try String(contentsOf: sensor.directory.appendingPathComponent("session.json"), encoding: .utf8)
    try require(serialized.contains("microphone_permission_denied") && serialized.contains("sensorUnavailable"), "module failure reasons survive successful video finalization")
    let motion = try readCSV(sensor.directory.appendingPathComponent("motion.csv"))
    try require(motion.isEmpty, "unavailable motion leaves header-only CSV")
  }

  static func emptySession(_ out: URL) async throws {
    let sensor = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "empty"), syncTest: true)
    let error = await sensor.finish(video: nil, error: "no_video_frames")
    try require(error == nil, "no-frame session can save diagnostics")
    let wave = try readWave(sensor.directory.appendingPathComponent("audio.wav"))
    try require(wave.payload.isEmpty, "empty recording keeps a structurally valid WAV")
    let manifest = try String(contentsOf: sensor.directory.appendingPathComponent("session.json"), encoding: .utf8)
    try require(manifest.contains("no_video_frames"), "empty recording records reason rather than claiming success")
    let parsed = try readJSON(sensor.directory.appendingPathComponent("session.json"))
    let audio = parsed["audio"] as! [String: Any]
    try require(audio["actualSampleRate"] is NSNull && audio["actualChannels"] is NSNull,
                "no-data audio does not claim an observed sample rate or channel count")
    try require(audio["wavHeaderSampleRate"] as? Int == 16000 && audio["wavHeaderChannels"] as? Int == 1,
                "empty WAV header defaults are explicit and separate from observed format")
  }

  static func formatChange(_ out: URL) async throws {
    let sensor = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "format-change"), syncTest: false)
    sensor.appendAudio(try makeAudio([100, -100, 300]), sourcePTS: .zero, hostTime: RoboMemoHostClock.now())
    sensor.appendAudio(try makeAudio([200, -200, 400], sampleRate: 48000), sourcePTS: CMTime(value: 1, timescale: 1000),
                       hostTime: RoboMemoHostClock.now())
    sensor.appendAudio(try makeAudio([500, -500]), sourcePTS: CMTime(value: 2, timescale: 1000), hostTime: RoboMemoHostClock.now())
    _ = await sensor.finish(video: nil, error: "test_no_video")
    let rows = try readCSV(sensor.directory.appendingPathComponent("audio_chunks.csv"))
    try require(rows.count == 3 && rows[1]["result"] == "not_written", "changed PCM format is explicitly rejected per chunk")
    try require(rows[0]["wavSampleOffset"] == "0" && rows[1]["wavSampleOffset"] == "3" && rows[2]["wavSampleOffset"] == "3", "rejected PCM chunk leaves later WAV offsets correct")
    let wav = try readWave(sensor.directory.appendingPathComponent("audio.wav"))
    try require(wav.payload.count == 10 && wav.sampleRate == 16000, "WAV remains valid original format after rejection")
    let manifest = try readJSON(sensor.directory.appendingPathComponent("session.json"))
    let modules = manifest["modules"] as! [String: [String: Any]]
    try require(modules["audio"]?["status"] as? String == "failed", "later valid chunk does not hide audio failure")
  }

  static func manifestWriteFailure(_ out: URL) async throws {
    let sensor = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "manifest-failure"), syncTest: false)
    let video = RoboMemoVideoRecorder(directory: sensor.directory, requestedResolution: "high", frameRate: 7,
                                     sessionLayout: true, metadataSink: { sensor.setVideoMetadata($0) })
    let clip = try await writeVideo(video)
    let json = sensor.directory.appendingPathComponent("session.json")
    try FileManager.default.removeItem(at: json)
    try FileManager.default.createDirectory(at: json, withIntermediateDirectories: false)
    try Data("keep".utf8).write(to: json.appendingPathComponent("sentinel"))
    let failure = await sensor.finish(video: clip, error: nil)
    try require(failure?.contains("session.json") == true, "manifest write failure returned to UI")
    let playable = try await RoboMemoVideoClip.read(clip.url)
    try require(playable.bytes > 0, "session metadata write failure never deletes valid video")
    try require(FileManager.default.fileExists(atPath: json.appendingPathComponent("sentinel").path), "atomic manifest failure does not destroy existing directory")
  }

  static func stopGate(_ out: URL) async throws {
    let sensor = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "stop-gate"), syncTest: true)
    let state = DeviceState(batteryLevel: 50)
    sensor.appendDeviceState(state, hostTime: RoboMemoHostClock.now())
    sensor.appendDeviceState(state, hostTime: RoboMemoHostClock.now())
    sensor.markSyncTestStart()
    sensor.stopAcceptingSamples()
    sensor.appendDeviceState(DeviceState(batteryLevel: 10), hostTime: RoboMemoHostClock.now())
    sensor.appendMotion(MotionSample(timestampNs: 42), hostTime: RoboMemoHostClock.now(), requestedRateHz: 60)
    sensor.appendAudio(try makeAudio([1, 2, 3]), sourcePTS: .zero, hostTime: RoboMemoHostClock.now())
    sensor.setModule("motion", status: "failed", reason: "cancelled_during_startup")
    sensor.setVideoMetadata(Data("{\"receivedFrames\":0,\"recordingStatus\":\"failed\"}".utf8))
    async let first = sensor.finish(video: nil, error: "cancelled")
    async let second = sensor.finish(video: nil, error: "cancelled")
    let failures = await [first, second]
    try require(failures.allSatisfy { $0 == nil }, "concurrent finish calls safely drain a single writer")
    let states = try readCSV(sensor.directory.appendingPathComponent("device_state.csv"))
    try require(states.count == 1 && states[0]["batteryLevel"] == "50", "identical states deduplicated and post-stop state rejected")
    let motion = try readCSV(sensor.directory.appendingPathComponent("motion.csv"))
    let audio = try readCSV(sensor.directory.appendingPathComponent("audio_chunks.csv"))
    try require(motion.isEmpty && audio.isEmpty, "stop gate rejects new samples before file finalization")
    let manifest = try readJSON(sensor.directory.appendingPathComponent("session.json"))
    try require(manifest["videoDiagnostics"] != nil, "stop gate still permits final video diagnostics")
    let sync = manifest["syncTest"] as! [String: Any]
    try require(sync["endHostTime"] != nil && sync["endReason"] as? String == "recording_stopped", "cancel during sync produces explicit bounded interval")
  }

  static func legacyVideo(_ out: URL) async throws {
    let directory = try makeParent(out, "legacy")
    let video = RoboMemoVideoRecorder(directory: directory, requestedResolution: "high", frameRate: 7)
    let clip = try await writeVideo(video)
    let names = try FileManager.default.contentsOfDirectory(atPath: directory.path)
    try require(names.count == 3 && clip.url.lastPathComponent.hasPrefix("RoboMemo-"), "disabled mode preserves legacy movie/JSON/CSV file layout")
    try require(names.contains(clip.url.deletingPathExtension().lastPathComponent + "_frames.csv"), "disabled mode keeps legacy frame sidecar filename")
    try require(clip.memoryKeyFilename == clip.url.lastPathComponent && clip.sensorDirectory == nil, "legacy memory identifier remains backward compatible")
  }

  static func memoryKeys(_ out: URL) throws {
    let a = RoboMemoVideoClip(url: out.appendingPathComponent("RoboMemo-Multi-one/video.mov"), createdAt: .now, duration: 1, bytes: 100)
    let b = RoboMemoVideoClip(url: out.appendingPathComponent("RoboMemo-Multi-two/video.mov"), createdAt: .now, duration: 1, bytes: 100)
    try require(a.memoryKeyFilename != b.memoryKeyFilename, "two nested video.mov files have distinct memory keys")
    try require(a.memoryKeyFilename == "RoboMemo-Multi-one.mov", "multi-sensor memory key stable across container path relocation")
    try require(a.sensorDirectory?.lastPathComponent == "RoboMemo-Multi-one", "multi-sensor clip exposes correct export folder")
  }

  static func invalidAndFloatAudio(_ out: URL) async throws {
    let invalid = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "invalid-pcm"), syncTest: false)
    let empty = AVAudioPCMBuffer(pcmFormat: AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!, frameCapacity: 32)!
    empty.frameLength = 0
    invalid.appendAudio(empty, sourcePTS: CMTime(value: 80000000000000001, timescale: 1_000_000_000),
                        hostTime: CMTime(value: 70000000000000001, timescale: 1_000_000_000))
    _ = await invalid.finish(video: nil, error: "test_no_video")
    let rejected = try readCSV(invalid.directory.appendingPathComponent("audio_chunks.csv"))
    try require(rejected.count == 1 && rejected[0]["result"] == "ignored_empty", "empty audio block keeps a diagnostic row without an error")
    try require(rejected[0]["sourcePTSValue"] == "80000000000000001" && rejected[0]["hostTimeValue"] == "70000000000000001", "rejected audio retains both original timestamps")
    let float = try RoboMemoSensorRecorder(parentDirectory: makeParent(out, "float-pcm"), syncTest: false)
    let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
    let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 3)!
    buffer.frameLength = 3
    for (i, sample) in [Float(-1), 0.5, .nan].enumerated() { buffer.floatChannelData![0][i] = sample }
    for (i, sample) in [Float(1), -0.5, 0].enumerated() { buffer.floatChannelData![1][i] = sample }
    float.appendAudio(buffer, sourcePTS: .zero, hostTime: RoboMemoHostClock.now())
    _ = await float.finish(video: nil, error: "test_no_video")
    let wave = try readWave(float.directory.appendingPathComponent("audio.wav"))
    let samples: [Int16] = [-32768, 32767, 16384, -16384, 0, 0]
    let expected = samples.flatMap { sample -> [UInt8] in
      let value = UInt16(bitPattern: sample); return [UInt8(truncatingIfNeeded: value), UInt8(truncatingIfNeeded: value >> 8)]
    }
    try require(wave.channels == 2 && wave.sampleRate == 48000 && wave.payload == Data(expected), "planar float PCM interleaves channels, clips endpoint and handles NaN safely")
  }
}
