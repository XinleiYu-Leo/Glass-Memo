import Foundation
import Observation

struct RoboMemoVideoSummary: Sendable {
  let clipID: URL
  let record: RoboMemoMemoryRecord
  var isSaved: Bool
  var createdAt: Date { record.clipCreatedAt }
  var text: String { record.summary }
  var frameCount: Int { record.frameTimestamps.count }
}

@Observable
@MainActor
final class RoboMemoCaptionViewModel {
  var photoData: Data? {
    didSet { invalidatePhoto() }
  }
  var isPhotoLoading = false {
    didSet {
      if isPhotoLoading { invalidatePhoto() }
    }
  }
  var serverURL: String = UserDefaults.standard.string(forKey: "RoboMemo.serverURL") ?? "" {
    didSet {
      UserDefaults.standard.set(serverURL, forKey: "RoboMemo.serverURL")
      connectionChanged()
    }
  }
  // This only authorizes the local Mac bridge. Provider credentials stay on the Mac.
  var pairingCode = "" {
    didSet { connectionChanged() }
  }
  private(set) var videoSummaries: [URL: RoboMemoVideoSummary] = [:]
  private(set) var memoryMessage: String?
  private(set) var memorySaveErrors: [URL: String] = [:]
  private(set) var isLoadingMemories = false
  @ObservationIgnored private var memoryStore: RoboMemoMemoryStore?
  @ObservationIgnored private var memoryLoadID: UUID?

  init(memoryDirectory: URL? = nil) {
    do {
      memoryStore = RoboMemoMemoryStore(directory: try memoryDirectory ?? RoboMemoMemoryStore.defaultDirectory())
    } catch {
      memoryMessage = "Saved memories are unavailable. New results will remain in this session only."
    }
  }

  func loadMemories(for clips: [RoboMemoVideoClip]) async {
    guard let memoryStore else { return }
    let id = UUID()
    memoryLoadID = id
    isLoadingMemories = true
    defer { if memoryLoadID == id { isLoadingMemories = false } }
    do {
      let loaded = try await memoryStore.load()
      guard !Task.isCancelled, memoryLoadID == id else { return }
      let byName = Dictionary(clips.map { ($0.memoryKeyFilename, $0) }, uniquingKeysWith: { first, _ in first })
      for record in loaded.records {
        guard let clip = byName[record.clipFilename] else { continue }
        // A disk read may finish after a new analysis. Never replace a newer in-memory result.
        if let current = videoSummaries[clip.id], current.record.analyzedAt >= record.analyzedAt { continue }
        videoSummaries[clip.id] = RoboMemoVideoSummary(clipID: clip.id, record: record, isSaved: true)
      }
      memoryMessage = loaded.unreadableCount == 0 ? nil
        : "\(loaded.unreadableCount) saved memory file(s) could not be read. The files and videos have been kept."
    } catch {
      guard !Task.isCancelled, memoryLoadID == id else { return }
      memoryMessage = "Saved memories could not be loaded. Your videos have been kept."
    }
  }

  func saveMemoryAgain(_ clipID: URL) async {
    guard let summary = videoSummaries[clipID], !summary.isSaved, let memoryStore else { return }
    do {
      try await memoryStore.save(summary.record)
      if videoSummaries[clipID]?.record.analyzedAt == summary.record.analyzedAt {
        videoSummaries[clipID]?.isSaved = true
        memorySaveErrors[clipID] = nil
      }
    } catch {
      if videoSummaries[clipID]?.record.analyzedAt == summary.record.analyzedAt {
        memorySaveErrors[clipID] = error.localizedDescription
      }
    }
  }
  private(set) var selectedVideoSummaryID: URL?
  private(set) var analyzingVideoID: URL?
  private(set) var analysisMessage = "Analyzing photo…"
  var selectedVideoSummary: RoboMemoVideoSummary? {
    selectedVideoSummaryID.flatMap { videoSummaries[$0] }
  }
  var hasConnectionSettings: Bool {
    !serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !pairingCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
  }

  func selectVideoSummary(_ clipID: URL) {
    guard !isAnalyzing, videoSummaries[clipID] != nil else { return }
    selectedVideoSummaryID = clipID
  }

  func selectPhotoForDisplay() { selectedVideoSummaryID = nil }

  private(set) var caption: String?
  private(set) var requestID: String?
  private(set) var errorMessage: String?
  private(set) var connectionMessage: String?
  private(set) var isConnected = false
  private(set) var isAnalyzing = false
  private(set) var isCheckingConnection = false

  @ObservationIgnored private var photoRevision = UUID()
  @ObservationIgnored private var analysisID: UUID?
  @ObservationIgnored private var connectionID: UUID?
  @ObservationIgnored private var analysisTask: Task<Void, Never>?
  @ObservationIgnored private var connectionTask: Task<Void, Never>?

  var canAnalyze: Bool {
    photoData != nil && !isPhotoLoading && !isAnalyzing
      && !serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !pairingCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
  }

  func analyzePhoto() {
    guard let data = photoData, !isPhotoLoading, !isAnalyzing else { return }
    cancelAnalysis()
    connectionTask?.cancel()
    finishConnectionCheck()
    caption = nil
    selectedVideoSummaryID = nil
    analysisMessage = "Analyzing photo…"
    requestID = nil
    errorMessage = nil
    let id = UUID()
    let revision = photoRevision
    let address = serverURL
    let code = pairingCode
    analysisID = id
    isAnalyzing = true
    analysisTask = Task { [weak self] in
      do {
        let result = try await RoboMemoCaptionClient.caption(data, serverURL: address, pairingCode: code)
        guard let self, !Task.isCancelled, self.analysisID == id,
          self.photoRevision == revision else { return }
        self.caption = result.caption
        self.requestID = result.requestID
        self.isConnected = true
        self.connectionMessage = "Connected to your Mac."
        self.finishAnalysis()
      } catch {
        guard let self, !Task.isCancelled, self.analysisID == id,
          self.photoRevision == revision else { return }
        self.errorMessage = error.localizedDescription
        self.finishAnalysis()
      }
    }
  }

  func analyzeVideo(_ clip: RoboMemoVideoClip) {
    guard !isAnalyzing, !isPhotoLoading, hasConnectionSettings else { return }
    connectionTask?.cancel()
    finishConnectionCheck()
    errorMessage = nil
    let id = UUID()
    let address = serverURL
    let code = pairingCode
    analysisID = id
    analyzingVideoID = clip.id
    analysisMessage = "Preparing video frames…"
    isAnalyzing = true
    analysisTask = Task { [weak self] in
      do {
        // Only small JPEGs cross back to the main actor; decoding is off the UI thread.
        let extraction = Task.detached(priority: .userInitiated) {
          try await RoboMemoVideoFrames.extract(from: clip.url)
        }
        let video = try await withTaskCancellationHandler {
          try await extraction.value
        } onCancel: { extraction.cancel() }
        try Task.checkCancellation()
        guard let self, self.analysisID == id else { return }
        self.analysisMessage = "Analyzing \(video.frames.count) video frames…"
        let result = try await RoboMemoCaptionClient.videoSummary(video, serverURL: address, pairingCode: code)
        guard !Task.isCancelled, self.analysisID == id else { return }
        let record = RoboMemoMemoryRecord(
          clipFilename: clip.memoryKeyFilename, clipCreatedAt: clip.createdAt, analyzedAt: .now,
          duration: video.duration, summary: result.caption,
          frameTimestamps: video.frames.map(\.timestampSeconds), observations: result.observations ?? [],
          model: result.model, promptVersion: result.promptVersion, requestID: result.requestID,
          highlights: result.highlights, samplingVersion: RoboMemoVideoFrames.samplingVersion
        )
        // Validate references against the actual decoded frames, even when the Mac has checked them.
        try RoboMemoMemoryStore.validate(record)
        self.videoSummaries[clip.id] = RoboMemoVideoSummary(clipID: clip.id, record: record, isSaved: false)
        self.memorySaveErrors[clip.id] = nil
        self.selectedVideoSummaryID = clip.id
        self.requestID = result.requestID
        self.isConnected = true
        self.connectionMessage = "Connected to your Mac."
        self.finishAnalysis()
        // A completed result belongs to this clip. Persist independently of later UI cancellation.
        await self.saveMemoryAgain(clip.id)
      } catch {
        guard let self, !Task.isCancelled, self.analysisID == id else { return }
        self.errorMessage = error.localizedDescription
        self.finishAnalysis()
      }
    }
  }

  func checkConnection() {
    connectionTask?.cancel()
    let id = UUID()
    let address = serverURL
    let code = pairingCode
    connectionID = id
    connectionMessage = nil
    isConnected = false
    isCheckingConnection = true
    connectionTask = Task { [weak self] in
      do {
        let status = try await RoboMemoCaptionClient.status(serverURL: address, pairingCode: code)
        guard let self, !Task.isCancelled, self.connectionID == id else { return }
        self.isConnected = status.ready
        let fallback = status.ready ? "Connected to your Mac." : "Sign in to ChatGPT on the Mac dashboard first."
        self.connectionMessage = status.message.isEmpty ? fallback : status.message
        if status.ready, let model = status.model, !model.isEmpty {
          self.connectionMessage = "Connected · \(model)"
        }
        self.finishConnectionCheck()
      } catch {
        guard let self, !Task.isCancelled, self.connectionID == id else { return }
        self.connectionMessage = error.localizedDescription
        self.finishConnectionCheck()
      }
    }
  }

  func cancelAnalysis() {
    analysisTask?.cancel()
    finishAnalysis()
  }

  func cancelRequests() {
    cancelAnalysis()
    connectionTask?.cancel()
    finishConnectionCheck()
  }

  private func invalidatePhoto() {
    photoRevision = UUID()
    selectedVideoSummaryID = nil
    cancelAnalysis()
    caption = nil
    requestID = nil
    errorMessage = nil
  }

  private func connectionChanged() {
    cancelRequests()
    isConnected = false
    connectionMessage = nil
    errorMessage = nil
  }

  private func finishAnalysis() {
    analysisID = nil
    analyzingVideoID = nil
    analysisTask = nil
    isAnalyzing = false
  }

  private func finishConnectionCheck() {
    connectionID = nil
    connectionTask = nil
    isCheckingConnection = false
  }
}

private struct RoboMemoBridgeStatus: Decodable, Sendable {
  let ready: Bool
  let model: String?
  let message: String
}

private struct RoboMemoCaptionResponse: Decodable, Sendable {
  let caption: String
  let requestID: String
  var model: String? = nil
  var promptVersion: String? = nil
  var observations: [RoboMemoObservation]? = nil
  var highlights: [RoboMemoHighlight]? = nil

  enum CodingKeys: String, CodingKey {
    case caption
    case requestID = "request_id"
    case model, observations, highlights
    case promptVersion = "prompt_version"
  }
}

private struct RoboMemoBridgeErrorResponse: Decodable {
  let error: String
  let message: String
}

private enum RoboMemoCaptionClient {
  static func status(serverURL: String, pairingCode: String) async throws -> RoboMemoBridgeStatus {
    let request = try makeRequest(serverURL: serverURL, pairingCode: pairingCode, endpoint: "status")
    return try await perform(request)
  }

  static func caption(
    _ imageData: Data, serverURL: String, pairingCode: String
  ) async throws -> RoboMemoCaptionResponse {
    guard !imageData.isEmpty, imageData.count <= 2 * 1024 * 1024 else {
      throw RoboMemoConnectionError.message("Choose a photo smaller than 2 MB after resizing.")
    }
    var request = try makeRequest(serverURL: serverURL, pairingCode: pairingCode, endpoint: "caption")
    request.httpMethod = "POST"
    request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
    request.httpBody = imageData
    let result: RoboMemoCaptionResponse = try await perform(request)
    let caption = result.caption.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !caption.isEmpty, caption.count <= 10_000 else {
      throw RoboMemoConnectionError.message("The Mac returned an invalid description. Please try again.")
    }
    return RoboMemoCaptionResponse(caption: caption, requestID: result.requestID)
  }

  static func videoSummary(
    _ video: RoboMemoVideoFrames, serverURL: String, pairingCode: String
  ) async throws -> RoboMemoCaptionResponse {
    struct Frame: Encodable {
      let timestamp_seconds: Double
      let jpeg_base64: String
    }
    struct Body: Encodable {
      let duration_seconds: Double
      let frames: [Frame]
      let include_memory = true
      let include_highlights = true
    }
    var request = try makeRequest(serverURL: serverURL, pairingCode: pairingCode, endpoint: "video-summary")
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try JSONEncoder().encode(Body(
      duration_seconds: video.duration,
      frames: video.frames.map { Frame(timestamp_seconds: $0.timestampSeconds, jpeg_base64: $0.jpegData.base64EncodedString()) }
    ))
    guard let size = request.httpBody?.count, size <= 18 * 1024 * 1024 else {
      throw RoboMemoConnectionError.message("The video frames are too large. Try a shorter recording.")
    }
    let result: RoboMemoCaptionResponse = try await perform(request)
    let summary = result.caption.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !summary.isEmpty, summary.count <= 10_000 else {
      throw RoboMemoConnectionError.message("The Mac returned an invalid video summary. Please try again.")
    }
    // Old bridges remain usable; their plain summary is saved without invented observations or provenance.
    return RoboMemoCaptionResponse(caption: summary, requestID: result.requestID,
                                  model: result.model, promptVersion: result.promptVersion,
                                  observations: result.observations, highlights: result.highlights)
  }

  private static func makeRequest(
    serverURL: String, pairingCode: String, endpoint: String
  ) throws -> URLRequest {
    let address = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
    guard let components = URLComponents(string: address),
      let scheme = components.scheme?.lowercased(), ["http", "https"].contains(scheme),
      let host = components.host, !host.isEmpty,
      components.user == nil, components.password == nil,
      components.query == nil, components.fragment == nil,
      let baseURL = components.url else {
      throw RoboMemoConnectionError.message("Enter the full server address shown on your Mac dashboard.")
    }
    let normalizedHost = host.lowercased()
    if scheme == "http",
      !normalizedHost.hasSuffix(".local"),
      (normalizedHost.contains(".") || normalizedHost.contains(":")) {
      throw RoboMemoConnectionError.message("For local HTTP, use the .local address shown on your Mac, not its IP address.")
    }
    let code = pairingCode.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !code.isEmpty, code.count <= 4096,
      code.rangeOfCharacter(from: .whitespacesAndNewlines) == nil else {
      throw RoboMemoConnectionError.message("Paste the pairing code shown on your Mac dashboard.")
    }
    let url = baseURL.appendingPathComponent("v1").appendingPathComponent(endpoint)
    var request = URLRequest(url: url)
    request.setValue("Bearer \(code)", forHTTPHeaderField: "Authorization")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.timeoutInterval = endpoint == "status" ? 20 : endpoint == "video-summary" ? 330 : 90
    return request
  }

  private static func perform<Result: Decodable & Sendable>(_ request: URLRequest) async throws -> Result {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.waitsForConnectivity = true
    configuration.timeoutIntervalForResource = request.url?.lastPathComponent == "video-summary" ? 360 : 120
    configuration.urlCache = nil
    configuration.httpCookieStorage = nil
    configuration.urlCredentialStorage = nil
    let session = URLSession(configuration: configuration, delegate: RoboMemoNoRedirects(), delegateQueue: nil)
    defer { session.invalidateAndCancel() }
    do {
      let (data, response) = try await session.data(for: request)
      try Task.checkCancellation()
      let maximumResponseBytes = request.url?.lastPathComponent == "video-summary" ? 512 * 1024 : 256 * 1024
      guard let response = response as? HTTPURLResponse, data.count <= maximumResponseBytes else {
        throw RoboMemoConnectionError.message("The Mac returned an invalid response.")
      }
      guard (200..<300).contains(response.statusCode) else {
        let error = try? JSONDecoder().decode(RoboMemoBridgeErrorResponse.self, from: data)
        let detail = error?.message.trimmingCharacters(in: .whitespacesAndNewlines)
        let fallback: String
        switch response.statusCode {
        case 401: fallback = "Pairing code not accepted. Copy it again from your Mac dashboard."
        case 413: fallback = "The selected media is too large. Try a smaller photo or shorter video."
        case 429: fallback = "The usage limit was reached. Check your Mac dashboard and try again later."
        case 503: fallback = "The Mac is not ready. Open its dashboard and sign in to ChatGPT."
        case 504: fallback = "The analysis timed out. Please try again."
        case 502: fallback = "The Mac could not get a description from OpenAI. Please try again."
        default: fallback = "The Mac returned HTTP \(response.statusCode)."
        }
        if let detail, !detail.isEmpty {
          throw RoboMemoConnectionError.message(String(detail.prefix(500)))
        }
        throw RoboMemoConnectionError.message(fallback)
      }
      do {
        return try JSONDecoder().decode(Result.self, from: data)
      } catch {
        throw RoboMemoConnectionError.message("The Mac returned an unexpected response. Check that the RoboMemo bridge is running.")
      }
    } catch let error as URLError {
      switch error.code {
      case .cancelled: throw CancellationError()
      case .timedOut:
        throw RoboMemoConnectionError.message("The request timed out. Keep the Mac bridge open and try again.")
      case .appTransportSecurityRequiresSecureConnection:
        throw RoboMemoConnectionError.message("Use the .local server address shown on your Mac dashboard.")
      case .cannotFindHost, .cannotConnectToHost, .notConnectedToInternet, .networkConnectionLost:
        throw RoboMemoConnectionError.message("Cannot reach your Mac. Check the address, use the same Wi-Fi, and allow Local Network access in iPhone Settings.")
      default:
        throw RoboMemoConnectionError.message("The connection failed. Check the Mac dashboard and try again.")
      }
    }
  }
}

private enum RoboMemoConnectionError: LocalizedError {
  case message(String)

  var errorDescription: String? {
    switch self {
    case .message(let message): message
    }
  }
}

private final class RoboMemoNoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
    completionHandler: @escaping @Sendable (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}
