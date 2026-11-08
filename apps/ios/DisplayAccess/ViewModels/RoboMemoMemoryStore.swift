import Foundation

struct RoboMemoObservation: Codable, Sendable, Identifiable {
  let frameIndex: Int
  let timestampSeconds: Double
  let text: String

  var id: Int { frameIndex }

  enum CodingKeys: String, CodingKey {
    case frameIndex = "frame_index"
    case timestampSeconds = "timestamp_seconds"
    case text
  }
}

struct RoboMemoHighlight: Codable, Sendable, Identifiable {
  let frameIndices: [Int]
  let timestampSeconds: Double
  let text: String

  var id: Int { frameIndices.first ?? -1 }

  enum CodingKeys: String, CodingKey {
    case frameIndices = "frame_indices"
    case timestampSeconds = "timestamp_seconds"
    case text
  }
}

struct RoboMemoMemoryRecord: Codable, Sendable {
  let schemaVersion: Int
  // Only the basename survives app-container changes; never persist a video URL.
  let clipFilename: String
  let clipCreatedAt: Date
  let analyzedAt: Date
  let duration: Double
  let summary: String
  let frameTimestamps: [Double]
  let observations: [RoboMemoObservation]
  let model: String?
  let promptVersion: String?
  let requestID: String
  let highlights: [RoboMemoHighlight]?
  let samplingVersion: String?

  init(
    schemaVersion: Int = 1,
    clipFilename: String,
    clipCreatedAt: Date,
    analyzedAt: Date,
    duration: Double,
    summary: String,
    frameTimestamps: [Double],
    observations: [RoboMemoObservation],
    model: String?,
    promptVersion: String?,
    requestID: String,
    highlights: [RoboMemoHighlight]? = nil,
    samplingVersion: String? = nil
  ) {
    self.schemaVersion = schemaVersion
    self.clipFilename = clipFilename
    self.clipCreatedAt = clipCreatedAt
    self.analyzedAt = analyzedAt
    self.duration = duration
    self.summary = summary
    self.frameTimestamps = frameTimestamps
    self.observations = observations
    self.model = model
    self.promptVersion = promptVersion
    self.requestID = requestID
    self.highlights = highlights
    self.samplingVersion = samplingVersion
  }

  enum CodingKeys: String, CodingKey {
    case schemaVersion = "schema_version"
    case clipFilename = "clip_filename"
    case clipCreatedAt = "clip_created_at"
    case analyzedAt = "analyzed_at"
    case duration, summary
    case frameTimestamps = "frame_timestamps"
    case observations, model
    case promptVersion = "prompt_version"
    case requestID = "request_id"
    case highlights
    case samplingVersion = "sampling_version"
  }
}

actor RoboMemoMemoryStore {
  struct LoadResult: Sendable {
    let records: [RoboMemoMemoryRecord]
    let unreadableCount: Int
  }

  enum StoreError: Error, LocalizedError {
    case unavailableDirectory
    case invalidRecord(String)
    case recordTooLarge
    case loadFailed
    case saveFailed
    case existingRecordUnreadable

    var errorDescription: String? {
      switch self {
      case .unavailableDirectory:
        return "The saved memory folder is unavailable."
      case .invalidRecord(let reason):
        return "This memory could not be saved. \(reason)"
      case .recordTooLarge:
        return "This memory is too large to save."
      case .loadFailed:
        return "Saved memories could not be loaded. The files have been kept."
      case .saveFailed:
        return "This memory could not be saved. Your previous memories have been kept."
      case .existingRecordUnreadable:
        return "The existing memory file could not be read. It has been kept unchanged."
      }
    }
  }

  private static let maximumRecordBytes = 512 * 1024
  private let directory: URL

  init(directory: URL) {
    self.directory = directory
  }

  // Resolving the location must not create files during view-model initialization.
  static func defaultDirectory() throws -> URL {
    guard let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
      throw StoreError.unavailableDirectory
    }
    return documents.appendingPathComponent("RoboMemoMemories", isDirectory: true)
  }

  static func validate(_ record: RoboMemoMemoryRecord) throws {
    guard record.schemaVersion == 1 else {
      throw StoreError.invalidRecord("The memory format is not supported.")
    }
    let filename = record.clipFilename
    guard filename.hasSuffix(".mov"), filename.count > 4, filename.utf8.count <= 240,
      !filename.contains("/"), !filename.contains("\\"), !filename.contains(":"),
      filename.rangeOfCharacter(from: .controlCharacters) == nil,
      (filename as NSString).lastPathComponent == filename
    else {
      throw StoreError.invalidRecord("The video filename is invalid.")
    }
    guard record.clipCreatedAt.timeIntervalSinceReferenceDate.isFinite,
      record.analyzedAt.timeIntervalSinceReferenceDate.isFinite
    else {
      throw StoreError.invalidRecord("The memory dates are invalid.")
    }
    guard record.duration.isFinite, record.duration > 0, record.duration <= 301 else {
      throw StoreError.invalidRecord("The video duration is invalid.")
    }
    guard !record.summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
      record.summary.count <= 10_000
    else {
      throw StoreError.invalidRecord("The summary is empty or too long.")
    }
    guard (1...150).contains(record.frameTimestamps.count) else {
      throw StoreError.invalidRecord("The sampled frame count is invalid.")
    }
    var previousTimestamp = -Double.infinity
    for timestamp in record.frameTimestamps {
      guard timestamp.isFinite, timestamp >= 0, timestamp <= record.duration,
        timestamp > previousTimestamp
      else {
        throw StoreError.invalidRecord("The sampled frame times are invalid or out of order.")
      }
      previousTimestamp = timestamp
    }
    guard record.observations.count <= record.frameTimestamps.count else {
      throw StoreError.invalidRecord("There are too many observations.")
    }
    var previousIndex = -1
    for observation in record.observations {
      guard record.frameTimestamps.indices.contains(observation.frameIndex),
        observation.frameIndex > previousIndex,
        observation.timestampSeconds.isFinite,
        observation.timestampSeconds >= 0, observation.timestampSeconds <= record.duration,
        abs(observation.timestampSeconds - record.frameTimestamps[observation.frameIndex]) < 0.001
      else {
        throw StoreError.invalidRecord("An observation does not match its sampled frame.")
      }
      guard !observation.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
        observation.text.count <= 300
      else {
        throw StoreError.invalidRecord("An observation is empty or too long.")
      }
      previousIndex = observation.frameIndex
    }
    if let highlights = record.highlights {
      guard highlights.count <= 5 else {
        throw StoreError.invalidRecord("There are too many highlights.")
      }
      let observationIndices = Set(record.observations.map(\.frameIndex))
      var previousGroupEnd = -1
      for highlight in highlights {
        guard let firstIndex = highlight.frameIndices.first else {
          throw StoreError.invalidRecord("A highlight has no supporting observations.")
        }
        var previousFrameIndex = previousGroupEnd
        for index in highlight.frameIndices {
          guard observationIndices.contains(index), index > previousFrameIndex else {
            throw StoreError.invalidRecord("Highlight references are invalid, overlapping, or out of order.")
          }
          previousFrameIndex = index
        }
        guard highlight.timestampSeconds.isFinite,
          highlight.timestampSeconds >= 0, highlight.timestampSeconds <= record.duration,
          abs(highlight.timestampSeconds - record.frameTimestamps[firstIndex]) < 0.001
        else {
          throw StoreError.invalidRecord("A highlight does not match its first sampled frame.")
        }
        guard !highlight.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          highlight.text.count <= 300
        else {
          throw StoreError.invalidRecord("A highlight is empty or too long.")
        }
        previousGroupEnd = previousFrameIndex
      }
    }
    if let samplingVersion = record.samplingVersion {
      guard !samplingVersion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
        samplingVersion.count <= 80,
        samplingVersion.rangeOfCharacter(from: .controlCharacters) == nil
      else {
        throw StoreError.invalidRecord("The sampling version is invalid.")
      }
    }
    guard !record.requestID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      throw StoreError.invalidRecord("The analysis request identifier is missing.")
    }
  }

  func load() throws -> LoadResult {
    guard directory.isFileURL else { throw StoreError.unavailableDirectory }
    let files: [URL]
    do {
      files = try FileManager.default.contentsOfDirectory(
        at: directory, includingPropertiesForKeys: nil
      )
    } catch {
      if Self.isMissingFile(error) { return LoadResult(records: [], unreadableCount: 0) }
      throw StoreError.loadFailed
    }

    var records: [RoboMemoMemoryRecord] = []
    var unreadableCount = 0
    for file in files where file.pathExtension.lowercased() == "json" {
      do {
        records.append(try Self.readRecord(at: file))
      } catch {
        // A single damaged or newer-format record must not hide other memories.
        unreadableCount += 1
      }
    }
    records.sort {
      if $0.analyzedAt == $1.analyzedAt { return $0.clipFilename < $1.clipFilename }
      return $0.analyzedAt > $1.analyzedAt
    }
    return LoadResult(records: records, unreadableCount: unreadableCount)
  }

  func save(_ record: RoboMemoMemoryRecord) throws {
    try Self.validate(record)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    encoder.dateEncodingStrategy = .millisecondsSince1970
    let data: Data
    do {
      data = try encoder.encode(record)
    } catch {
      throw StoreError.saveFailed
    }
    guard data.count <= Self.maximumRecordBytes else { throw StoreError.recordTooLarge }
    guard directory.isFileURL else { throw StoreError.unavailableDirectory }

    let file = directory.appendingPathComponent(record.clipFilename + ".json", isDirectory: false)
    do {
      try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
      // Do not overwrite a corrupt, unrecognized, or redirected existing file.
      do {
        _ = try FileManager.default.attributesOfItem(atPath: file.path)
        do {
          let existing = try Self.readRecord(at: file)
          // Retries and new analyses may arrive out of order across caller tasks.
          if existing.analyzedAt > record.analyzedAt { return }
        } catch {
          throw StoreError.existingRecordUnreadable
        }
      } catch {
        if !Self.isMissingFile(error) { throw error }
      }
      // Foundation writes a sibling temporary file before replacing the destination.
      // A failure therefore leaves the previous complete record intact.
      try data.write(to: file, options: .atomic)
    } catch let error as StoreError {
      throw error
    } catch {
      throw StoreError.saveFailed
    }
  }

  private static func readRecord(at file: URL) throws -> RoboMemoMemoryRecord {
    let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
    guard attributes[.type] as? FileAttributeType == .typeRegular,
      let byteCount = attributes[.size] as? NSNumber,
      byteCount.int64Value >= 0, byteCount.int64Value <= Int64(maximumRecordBytes)
    else {
      throw StoreError.recordTooLarge
    }
    let handle = try FileHandle(forReadingFrom: file)
    defer { try? handle.close() }
    // Also bound the read itself if a file grows after the size check.
    let data = try handle.read(upToCount: maximumRecordBytes + 1) ?? Data()
    guard data.count <= maximumRecordBytes else { throw StoreError.recordTooLarge }
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .millisecondsSince1970
    let record = try decoder.decode(RoboMemoMemoryRecord.self, from: data)
    try validate(record)
    guard file.lastPathComponent == record.clipFilename + ".json" else {
      throw StoreError.invalidRecord("The memory file does not match its video filename.")
    }
    return record
  }

  private static func isMissingFile(_ error: Error) -> Bool {
    guard let error = error as? CocoaError else { return false }
    return error.code == .fileNoSuchFile || error.code == .fileReadNoSuchFile
  }
}
