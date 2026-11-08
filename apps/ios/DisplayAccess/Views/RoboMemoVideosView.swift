import AVKit
import Combine
import SwiftUI

struct RoboMemoVideosView: View {
  let clips: [RoboMemoVideoClip]
  let isCameraBusy: Bool
  @Bindable var model: RoboMemoCaptionViewModel
  let showSummary: (URL) -> Void
  let isDisplayBusy: Bool
  @State private var pendingAnalysis: RoboMemoVideoClip?
  @State private var playingClip: RoboMemoPlaybackSelection?
  @State private var recallRange = RoboMemoRecallRange.lastHour
  @State private var memorySearch = ""

  var body: some View {
    if !clips.isEmpty {
      VStack(alignment: .leading, spacing: 14) {
        recentMemory
        Divider()
        Label("Saved videos", systemImage: "film.stack")
          .font(.headline)
        ForEach(clips) { clip in
          VStack(alignment: .leading, spacing: 8) {
            Text(recordingLabel(clip))
              .font(.subheadline.weight(.semibold))
            Text(String(format: "%.1f seconds · %@ · No audio", clip.duration,
                        ByteCountFormatter.string(fromByteCount: clip.bytes, countStyle: .file)))
              .font(.caption)
              .foregroundStyle(.secondary)
            if clip.sensorDirectory != nil {
              Text("多传感器录制：音频和诊断数据在同一文件夹。此处只播放/分享视频；导出全套请使用文件 App。")
                .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
              Button { playingClip = RoboMemoPlaybackSelection(clip: clip, seconds: 0) } label: {
                Label("Play", systemImage: "play.fill")
              }
              ShareLink(item: clip.url) {
                Label("Share / Save", systemImage: "square.and.arrow.up")
              }
            }
            .buttonStyle(.bordered)
            .disabled(isCameraBusy || model.isAnalyzing)
            if model.analyzingVideoID == clip.id {
              HStack {
                ProgressView(model.analysisMessage)
                Spacer()
                Button("Cancel", action: model.cancelAnalysis)
              }
            } else {
              Button {
                pendingAnalysis = clip
              } label: {
                Label(model.videoSummaries[clip.id] == nil ? "Analyze video" : "Analyze video again", systemImage: "sparkles")
              }
              .buttonStyle(.borderedProminent)
              .disabled(isCameraBusy || isDisplayBusy || model.isAnalyzing || model.isPhotoLoading || !model.hasConnectionSettings)
            }
            if let summary = model.videoSummaries[clip.id] {
              Text("AI video summary").font(.subheadline.weight(.semibold))
              Text(summary.text).textSelection(.enabled)
              memorySaveStatus(summary)
              Text("Based on \(summary.frameCount) sampled frames. Brief actions may be missed. No audio was analyzed.")
                .font(.caption).foregroundStyle(.secondary)
              Button {
                showSummary(clip.id)
              } label: {
                Label("Show summary on glasses", systemImage: "eyeglasses")
              }
              .buttonStyle(.bordered)
              .disabled(isCameraBusy || isDisplayBusy || model.isAnalyzing || model.isPhotoLoading)
            }
          }
          if clip.id != clips.last?.id { Divider() }
        }
        Text("Analyze video sends up to 150 sampled frames to OpenAI through your Mac. Saved recaps, key moments, and observations remain on this phone after restarting the app.")
          .font(.caption)
          .foregroundStyle(.secondary)
        if !model.hasConnectionSettings {
          Text("Enter your Mac connection details below to analyze a video.")
            .font(.caption).foregroundStyle(.secondary)
        }
        Text("Use Share / Save to export a video copy. Deleting this app removes its videos and saved memories.")
          .font(.caption)
          .foregroundStyle(.secondary)
      }
      .padding(14)
      .frame(maxWidth: .infinity, alignment: .leading)
      .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
      .task(id: clips.map(\.id)) { await model.loadMemories(for: clips) }
      .sheet(item: $playingClip) { selection in
        RoboMemoVideoPlayback(clip: selection.clip, seconds: selection.seconds)
      }
      .confirmationDialog("Analyze this video?", isPresented: Binding(
        get: { pendingAnalysis != nil },
        set: { if !$0 { pendingAnalysis = nil } }
      ), titleVisibility: .visible) {
        if let clip = pendingAnalysis {
          Button("Send frames and analyze") {
            pendingAnalysis = nil
            model.analyzeVideo(clip)
          }
        }
        Button("Cancel", role: .cancel) { pendingAnalysis = nil }
      } message: {
        Text("Up to 150 frames from this recording will be sent to OpenAI through your Mac for an English recap, key moments, and timestamped observations, using your connected account’s allowance. Results will be saved on this phone. The full video and audio are not sent.")
      }
    }
  }

  private var recentMemory: some View {
    VStack(alignment: .leading, spacing: 12) {
      Label("Recent memory", systemImage: "clock.arrow.circlepath").font(.headline)
      Text("What did I just see?").font(.subheadline.weight(.semibold))
      Picker("Memory time range", selection: $recallRange) {
        ForEach(RoboMemoRecallRange.allCases) { range in Text(range.rawValue).tag(range) }
      }
      .pickerStyle(.segmented)
      TextField("Find a word, object, or sign", text: $memorySearch)
        .textFieldStyle(.roundedBorder)
        .autocorrectionDisabled()
        .accessibilityLabel("Search saved recaps, key moments, and observations")
      Text("Search matches saved English text on this phone. It does not analyze more footage.")
        .font(.caption).foregroundStyle(.secondary)
      if model.isLoadingMemories { ProgressView("Loading saved memories…") }
      if let message = model.memoryMessage {
        Text(message).font(.caption).foregroundStyle(.orange)
      }
      TimelineView(.periodic(from: .now, by: 60)) { context in
        let inRange = clips.filter { recallRange.contains($0.createdAt, now: context.date) }
        let recalled = inRange.filter { clip in
          guard let summary = model.videoSummaries[clip.id] else { return false }
          let query = memorySearch.trimmingCharacters(in: .whitespacesAndNewlines)
          return query.isEmpty || summary.text.localizedCaseInsensitiveContains(query)
            || summary.record.observations.contains { $0.text.localizedCaseInsensitiveContains(query) }
            || summary.record.highlights?.contains { $0.text.localizedCaseInsensitiveContains(query) } == true
        }.sorted { $0.createdAt < $1.createdAt }
        let analyzed = inRange.filter { model.videoSummaries[$0.id] != nil }.count
        VStack(alignment: .leading, spacing: 12) {
          Text("\(analyzed) of \(inRange.count) recordings have a recap in this range.")
            .font(.caption).foregroundStyle(.secondary)
          if recalled.isEmpty {
            Text(inRange.isEmpty ? "No recordings in this time range. Choose All to review older videos."
                 : "No matching saved recap. Analyze a recording below or try another word. Missing evidence does not mean an event did not happen.")
              .font(.subheadline).foregroundStyle(.secondary)
          }
          ForEach(recalled) { clip in
            if let summary = model.videoSummaries[clip.id] {
              VStack(alignment: .leading, spacing: 8) {
                Text(recordingLabel(clip))
                  .font(.subheadline.weight(.semibold))
                Text(summary.text).textSelection(.enabled)
                memorySaveStatus(summary)
                if summary.record.observations.isEmpty {
                  Text(summary.record.promptVersion == nil
                       ? "This recap has no timestamped observations. Update the Mac helper and analyze again to add them."
                       : "No useful timestamped observations were returned for these sampled frames.")
                    .font(.caption).foregroundStyle(.secondary)
                }
                memoryEvidence(clip: clip, record: summary.record)
              }
              if clip.id != recalled.last?.id { Divider() }
            }
          }
        }
      }
      Text("Brief events between sampled frames can be missed. Tap a key moment to play its earliest evidence, or open its source moments to check each point in the original video.")
        .font(.caption).foregroundStyle(.secondary)
    }
  }

  @ViewBuilder
  private func memoryEvidence(clip: RoboMemoVideoClip, record: RoboMemoMemoryRecord) -> some View {
    if let highlights = record.highlights {
      if highlights.isEmpty {
        Text("No clear key moments were returned. You can still review the observations below.")
          .font(.caption).foregroundStyle(.secondary)
      } else {
        Text("Key moments").font(.subheadline.weight(.semibold))
        ForEach(highlights) { highlight in
          VStack(alignment: .leading, spacing: 6) {
            Button {
              playingClip = RoboMemoPlaybackSelection(clip: clip, seconds: highlight.timestampSeconds)
            } label: {
              HStack(alignment: .top, spacing: 8) {
                Image(systemName: "play.circle.fill")
                Text(String(format: "%.1fs", highlight.timestampSeconds)).monospacedDigit()
                Text(highlight.text).frame(maxWidth: .infinity, alignment: .leading)
              }
              .font(.subheadline)
            }
            .buttonStyle(.bordered)
            .disabled(isCameraBusy || model.isAnalyzing)
            .accessibilityLabel("Play key moment from \(String(format: "%.1f", highlight.timestampSeconds)) seconds: \(highlight.text)")
            DisclosureGroup("Source moments (\(highlight.frameIndices.count))") {
              ForEach(record.observations.filter { highlight.frameIndices.contains($0.frameIndex) }) { observation in
                observationButton(clip: clip, observation: observation)
              }
            }
            .font(.caption)
          }
        }
      }
    } else if !record.observations.isEmpty {
      Text("This saved recap has detailed observations. Analyze again to add key moments.")
        .font(.caption).foregroundStyle(.secondary)
    }

    let query = memorySearch.trimmingCharacters(in: .whitespacesAndNewlines)
    let matches = record.observations.filter { $0.text.localizedCaseInsensitiveContains(query) }
    if !query.isEmpty, !matches.isEmpty {
      Text("Matching observations").font(.subheadline.weight(.semibold))
      ForEach(matches) { observation in
        observationButton(clip: clip, observation: observation)
      }
    }
    if !record.observations.isEmpty {
      DisclosureGroup("All observations (\(record.observations.count))") {
        ForEach(record.observations) { observation in
          observationButton(clip: clip, observation: observation)
        }
      }
      .font(.subheadline)
    }
  }

  private func observationButton(clip: RoboMemoVideoClip, observation: RoboMemoObservation) -> some View {
    Button {
      playingClip = RoboMemoPlaybackSelection(clip: clip, seconds: observation.timestampSeconds)
    } label: {
      HStack(alignment: .top, spacing: 8) {
        Image(systemName: "play.circle")
        Text(String(format: "%.1fs", observation.timestampSeconds)).monospacedDigit()
        Text(observation.text).frame(maxWidth: .infinity, alignment: .leading)
      }
      .font(.subheadline)
    }
    .buttonStyle(.bordered)
    .disabled(isCameraBusy || model.isAnalyzing)
    .accessibilityLabel("Play evidence at \(String(format: "%.1f", observation.timestampSeconds)) seconds: \(observation.text)")
  }

  /// Display a stable recording identifier without exposing its calendar date.
  /// Keep the underlying timestamps for recall filtering and synchronization.
  private func recordingLabel(_ clip: RoboMemoVideoClip) -> String {
    let stem = (clip.memoryKeyFilename as NSString).deletingPathExtension
    let identifier = stem.hasPrefix("RoboMemo-Multi-") ? String(stem.dropFirst("RoboMemo-Multi-".count))
      : stem.hasPrefix("RoboMemo-") ? String(stem.dropFirst("RoboMemo-".count)) : stem
    return "录像 · \(identifier.prefix(8))"
  }

  @ViewBuilder
  private func memorySaveStatus(_ summary: RoboMemoVideoSummary) -> some View {
    if summary.isSaved {
      Label("Saved on this phone", systemImage: "checkmark.circle")
        .font(.caption).foregroundStyle(.secondary)
    } else {
      if let error = model.memorySaveErrors[summary.clipID] {
        Text(error).font(.caption).foregroundStyle(.orange)
      }
      HStack {
        Text("Not saved yet — keep the app open.").font(.caption).foregroundStyle(.orange)
        Button("Save again") { Task { await model.saveMemoryAgain(summary.clipID) } }
          .font(.caption)
      }
    }
  }
}

private enum RoboMemoRecallRange: String, CaseIterable, Identifiable {
  case lastHour = "Last hour", today = "Today", all = "All"
  var id: Self { self }
  func contains(_ date: Date, now: Date) -> Bool {
    switch self {
    case .lastHour: date >= now.addingTimeInterval(-3_600) && date <= now
    case .today: Calendar.current.isDate(date, inSameDayAs: now) && date <= now
    case .all: true
    }
  }
}

private struct RoboMemoPlaybackSelection: Identifiable {
  let id = UUID()
  let clip: RoboMemoVideoClip
  let seconds: Double
}

private struct RoboMemoVideoPlayback: View {
  @Environment(\.dismiss) private var dismiss
  @State private var player: AVPlayer
  @State private var item: AVPlayerItem
  @State private var didSeek = false
  @State private var playbackTask: Task<Void, Never>?
  @State private var playbackError: String?
  let seconds: Double

  init(clip: RoboMemoVideoClip, seconds: Double) {
    self.seconds = seconds
    let item = AVPlayerItem(url: clip.url)
    _item = State(initialValue: item)
    _player = State(initialValue: AVPlayer(playerItem: item))
  }

  var body: some View {
    NavigationStack {
      VideoPlayer(player: player)
        .overlay(alignment: .bottom) {
          if let playbackError {
            Text(playbackError).padding().background(.regularMaterial)
          }
        }
        .navigationTitle("Recorded video")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
    }
    .onReceive(item.publisher(for: \.status).receive(on: RunLoop.main)) { status in
      if status == .failed {
        playbackError = "This video could not be played. Try opening it again."
      } else if status == .readyToPlay, !didSeek {
        didSeek = true
        playbackTask = Task {
          let reached = await player.seek(to: CMTime(seconds: seconds, preferredTimescale: 600),
                                          toleranceBefore: .zero, toleranceAfter: .zero)
          guard !Task.isCancelled else { return }
          if reached { player.play() }
          else { playbackError = "Could not seek to this moment. Try opening the video again." }
        }
      }
    }
    .onDisappear {
      playbackTask?.cancel()
      item.cancelPendingSeeks()
      player.pause()
      player.replaceCurrentItem(with: nil)
    }
  }
}
