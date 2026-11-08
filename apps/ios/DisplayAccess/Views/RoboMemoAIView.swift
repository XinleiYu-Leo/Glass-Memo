import SwiftUI

struct RoboMemoAIView: View {
  @Bindable var model: RoboMemoCaptionViewModel
  var glassesCamera: RoboMemoGlassesCameraViewModel
  var isCameraBusy: Bool
  var isPhotoCaptureBusy: Bool
  var isLivePreviewBusy: Bool
  var startLivePreview: () -> Void
  var startRecording: () -> Void
  var stopLivePreview: () -> Void
  var captureUnavailable: Bool
  var capturePhoto: () -> Void
  var cancelCapture: () -> Void
  var showVideoSummary: (URL) -> Void
  var isDisplayBusy: Bool
  @State private var isConnectionExpanded = true

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      RoboMemoPhotoPicker(
        photoData: $model.photoData,
        isLoading: $model.isPhotoLoading,
        showsLoadingProgress: !isCameraBusy
      )
        .disabled(isCameraBusy || model.isAnalyzing)
      if model.photoData != nil, model.selectedVideoSummary != nil {
        Button("Use photo for glasses", action: model.selectPhotoForDisplay)
          .disabled(model.isAnalyzing || isCameraBusy || isDisplayBusy)
      }

      VStack(alignment: .leading, spacing: 10) {
        if isPhotoCaptureBusy {
          HStack {
            ProgressView(glassesCamera.statusMessage ?? "Taking photo…")
            Spacer()
            Button("Cancel", action: cancelCapture)
          }
        } else {
          Button(action: capturePhoto) {
            Label("Take photo with glasses", systemImage: "camera")
              .frame(maxWidth: .infinity)
          }
          .buttonStyle(.bordered)
          .disabled(captureUnavailable)
        }
        Text("Look at what you want to capture. Your photo stays on this phone until you tap Analyze photo.")
          .font(.caption)
          .foregroundStyle(.secondary)
        if let message = glassesCamera.errorMessage {
          Text(message)
            .font(.caption)
            .foregroundStyle(.red)
        } else if !isCameraBusy, let message = glassesCamera.statusMessage {
          Text(message)
            .font(.caption)
            .foregroundStyle(.secondary)
        }
      }
      .padding(14)
      .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))

      RoboMemoLivePreviewView(
        camera: glassesCamera,
        isBusy: isLivePreviewBusy,
        startUnavailable: captureUnavailable,
        start: startLivePreview,
        record: startRecording,
        stop: stopLivePreview
      )

      RoboMemoVideosView(clips: glassesCamera.recordedClips, isCameraBusy: isCameraBusy, model: model,
                         showSummary: showVideoSummary, isDisplayBusy: isDisplayBusy)
      if let error = glassesCamera.videoLibraryError {
        Text(error).font(.caption).foregroundStyle(.red)
      }

      VStack(alignment: .leading, spacing: 14) {
        DisclosureGroup("Mac connection", isExpanded: $isConnectionExpanded) {
          VStack(alignment: .leading, spacing: 12) {
            Text("Keep the RoboMemo dashboard open on your Mac. Use the same Wi-Fi on both devices.")
              .font(.caption)
              .foregroundStyle(.secondary)
            TextField("http://your-mac.local:8766", text: $model.serverURL)
              .textFieldStyle(.roundedBorder)
              .keyboardType(.URL)
              .textContentType(.URL)
              .textInputAutocapitalization(.never)
              .autocorrectionDisabled()
              .accessibilityLabel("Mac server address")
            SecureField("Pairing code from Mac dashboard", text: $model.pairingCode)
              .textFieldStyle(.roundedBorder)
              .textInputAutocapitalization(.never)
              .autocorrectionDisabled()
              .accessibilityLabel("Mac pairing code")
            HStack {
              Button("Test connection", action: model.checkConnection)
                .buttonStyle(.bordered)
                .disabled(model.isCheckingConnection || model.serverURL.isEmpty || model.pairingCode.isEmpty)
              if model.isCheckingConnection {
                ProgressView()
              }
            }
          }
          .padding(.top, 10)
        }

        if let message = model.connectionMessage {
          Label(message, systemImage: model.isConnected ? "checkmark.circle" : "info.circle")
            .font(.caption)
            .foregroundStyle(model.isConnected ? Color.green : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
        }

        Text("Analyze photo sends your selected photo to OpenAI through your Mac for an English description.")
          .font(.caption)
          .foregroundStyle(.secondary)
          .fixedSize(horizontal: false, vertical: true)

        if model.isAnalyzing {
          HStack {
            ProgressView(model.analysisMessage)
            Spacer()
            Button("Cancel", action: model.cancelAnalysis)
          }
        } else {
          Button(action: model.analyzePhoto) {
            Label(model.caption == nil ? "Analyze photo" : "Analyze again", systemImage: "sparkles")
              .frame(maxWidth: .infinity)
          }
          .buttonStyle(.borderedProminent)
          .disabled(!model.canAnalyze || isCameraBusy || isDisplayBusy)
        }

        if let message = model.errorMessage {
          Text(message)
            .font(.caption)
            .foregroundStyle(.red)
            .fixedSize(horizontal: false, vertical: true)
        }
      }
      .padding(16)
      .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))

      if let caption = model.caption {
        VStack(alignment: .leading, spacing: 10) {
          Label("AI description", systemImage: "text.bubble")
            .font(.headline)
          Text(caption)
            .font(.body)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
          Text("Tap Show on glasses to read this description on your glasses, or use Developer preview.")
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
      }
    }
  }
}

/// Only this small view reads previewImage, isolating per-frame redraws from
/// photo selection, connection fields, and description controls.
private struct RoboMemoLivePreviewView: View {
  var camera: RoboMemoGlassesCameraViewModel
  var isBusy: Bool
  var startUnavailable: Bool
  var start: () -> Void
  var record: () -> Void
  var stop: () -> Void

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      Label("Glasses video", systemImage: "video")
        .font(.headline)

      Toggle("多传感器录制（实验）", isOn: Binding(
        get: { camera.multiSensorEnabled },
        set: { camera.setMultiSensorEnabled($0) }
      ))
      .disabled(isBusy || camera.isBusy || startUnavailable)
      if camera.multiSensorEnabled {
        Text("High @7 视频 + IMU + 设备状态，可选择同时录制眼镜音频。可选传感器不可用时继续录视频。")
          .font(.caption).foregroundStyle(.secondary)
        Toggle("录制音频", isOn: Binding(
          get: { camera.sensorAudioEnabled },
          set: { camera.setSensorAudioEnabled($0) }
        ))
        .disabled(isBusy || camera.isBusy || startUnavailable)
        Text(camera.sensorAudioEnabled
          ? "录制眼镜麦克风的声音；首次使用会请求权限。"
          : "只录视频、IMU 和设备状态。用于检查同时录音是否影响 IMU；同步测试仍可转头。")
          .font(.caption).foregroundStyle(.secondary)
        Picker("IMU 采样率", selection: Binding(
          get: { camera.motionRateHz },
          set: { camera.setMotionRateHz($0) }
        )) {
          Text("60 Hz（默认）").tag(60)
          Text("30 Hz（对照）").tag(30)
        }
        .pickerStyle(.segmented)
        .disabled(isBusy || camera.isBusy || startUnavailable)
        Text(camera.sensorAudioEnabled
          ? "可对比 60 Hz 和 30 Hz 的数据连续性。"
          : "本轮对照选 30 Hz，其他设置与上一段保持一致。")
          .font(.caption).foregroundStyle(.secondary)
        Toggle("同步测试", isOn: Binding(
          get: { camera.syncTestEnabled },
          set: { camera.setSyncTestEnabled($0) }
        ))
        .disabled(isBusy || camera.isBusy || startUnavailable)
      }

      if !camera.multiSensorEnabled { VStack(alignment: .leading, spacing: 6) {
        Text("Recording quality")
          .font(.subheadline)
        Picker("Recording quality", selection: Binding(
          get: { camera.selectedRecordingPreset },
          set: { camera.selectRecordingPreset($0) }
        )) {
          ForEach(RoboMemoGlassesCameraViewModel.RecordingPreset.allCases) { preset in
            Text(preset.label).tag(preset)
          }
        }
        .pickerStyle(.segmented)
        .disabled(isBusy || camera.isBusy || startUnavailable)
      } }

      if isBusy {
        ZStack {
          Color.black
          if let image = camera.previewImage {
            Image(uiImage: image)
              .resizable()
              .scaledToFit()
              .accessibilityLabel("Live image from your glasses camera")
          } else {
            VStack(spacing: 12) {
              ProgressView().tint(.white)
              Text(camera.isSavingVideo ? "Saving video…" : camera.previewState == .stopping ? "Stopping preview…" : "Waiting for camera image…")
                .font(.caption)
                .foregroundStyle(.white)
            }
          }
        }
        .frame(height: 300)
        .clipShape(RoundedRectangle(cornerRadius: 12))

        if camera.isRecording {
          let remaining = max(0, Int(RoboMemoVideoRecorder.defaultMaximumDuration) - camera.recordingSeconds)
          Label(String(format: "%d:%02d remaining", remaining / 60, remaining % 60), systemImage: "record.circle")
            .font(.headline.monospacedDigit())
            .foregroundStyle(.red)
          ProgressView(value: Double(camera.recordingSeconds), total: RoboMemoVideoRecorder.defaultMaximumDuration)
            .tint(.red)
        }

        if camera.syncTestInProgress {
          VStack(alignment: .leading, spacing: 8) {
            Text(camera.sensorAudioEnabled ? "拍手 3 次、快速左右甩头 3 次" : "在舒适范围内左右转头 3 轮")
              .font(.headline)
            Text("在舒适范围内转头；做完后点下面按钮，标记同步测试结束。")
              .font(.caption)
            Button("同步测试完成", action: camera.finishSyncTest)
              .buttonStyle(.bordered)
          }
          .padding(12)
          .background(Color.yellow.opacity(0.15), in: RoundedRectangle(cornerRadius: 10))
        }

        if let message = camera.statusMessage {
          Text(message)
            .font(.caption)
            .foregroundStyle(camera.previewState == .paused ? Color.orange : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
        }
        if let message = camera.sensorStatusMessage {
          Text(message).font(.caption).foregroundStyle(.secondary)
        }
        Button(action: stop) {
          Label(camera.isRecording ? "Stop and save" : "Stop preview", systemImage: "stop.fill")
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .disabled(camera.previewState == .stopping)
      } else {
        Button(action: record) {
          Label(camera.multiSensorEnabled ? "开始多传感器录制（最多 5 分钟）" : "Record 5-minute video", systemImage: "record.circle")
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .disabled(startUnavailable)

        Button(action: start) {
          Label("Start live preview", systemImage: "video")
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .disabled(startUnavailable)
      }
      Text(camera.multiSensorEnabled
           ? "请保持本页打开。停止后保存在 RoboMemoVideos 的独立文件夹，可从 iPhone 文件 App 或 Mac 的文件共享导出。视频播放本身无声；开启录音时，声音在单独的 WAV 文件。多传感器数据不会自动上传。仅开启预览不会录制传感器。"
           : "Record up to 5 minutes without audio. Tap Stop and save to finish early. Videos stay in this app for playback or sharing. Tap Analyze video separately to send sampled frames to AI. Keep this screen open while recording. Live preview alone does not save video.")
        .font(.caption)
        .foregroundStyle(.secondary)
        .fixedSize(horizontal: false, vertical: true)
      if !isBusy, let folder = camera.lastSensorFolder {
        Text("最近的多传感器文件夹：\(folder.lastPathComponent)")
          .font(.caption).textSelection(.enabled)
      }
    }
    .padding(14)
    .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
  }
}
