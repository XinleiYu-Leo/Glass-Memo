/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

import MWDATCore
import MWDATMockDevice
import SwiftUI
import UIKit

enum SampleAppItem: String, CaseIterable, Identifiable {
  case roboMemo = "robo-memo"
  case carMaintenance = "car-maintenance"

  var id: String { rawValue }

  var title: String {
    switch self {
    case .roboMemo: "RoboMemo"
    case .carMaintenance: "Car maintenance guide"
    }
  }

  var description: String {
    switch self {
    case .roboMemo:
      "Capture a photo or a short video, get an English AI summary, and read it on your glasses."
    case .carMaintenance:
      "A sample of a display experience where you follow a step by step guide to complete car maintenance task."
    }
  }

  var iconName: String {
    switch self {
    case .roboMemo: "text.bubble.fill"
    case .carMaintenance: "car.fill"
    }
  }

  var iconBackground: Color {
    switch self {
    case .roboMemo: Color(red: 0.10, green: 0.28, blue: 0.56)
    case .carMaintenance: Color(red: 0.32, green: 0.10, blue: 0.10)
    }
  }
}

struct SampleAppsView: View {
  var displayViewModel: DisplayViewModel
  var developerPreviewMode: DeveloperPreviewMode?
  var isDeveloperPreviewChanging: Bool
  var phonePreviewDisplay: (any MockDisplayKit)?
  var phonePreviewDeviceIdentifier: DeviceIdentifier?
  var chromePreviewURL: URL?
  var developerPreviewErrorMessage: String?
  var setDeveloperPreviewMode: (DeveloperPreviewMode?) async -> Bool

  @State private var item: SampleAppItem = .roboMemo
  @State private var isDeveloperPreviewPresented = false
  @State private var isDeveloperPreviewTransitioning = false
  @State private var selectedDeveloperPreviewMode = DeveloperPreviewMode.defaultMode
  @State private var copiedChromePreviewURL: URL?
  @State private var roboMemo = RoboMemoCaptionViewModel()
  @State private var glassesCamera = RoboMemoGlassesCameraViewModel()
  @State private var glassesCaptureTask: Task<Void, Never>?
  @State private var glassesPreviewTask: Task<Void, Never>?

  var body: some View {
    GeometryReader { geometry in
      ScrollView {
        mainContent
          .frame(minHeight: geometry.size.height)
      }
    }
    .toolbar(.hidden, for: .navigationBar)
    .sheet(isPresented: $isDeveloperPreviewPresented) {
      developerPreviewSheet
    }
    .onChange(of: item) { _, selectedItem in
      if selectedItem != .roboMemo {
        roboMemo.cancelRequests()
        cancelGlassesCapture()
      }
    }
    .onReceive(NotificationCenter.default.publisher(for: UIApplication.didEnterBackgroundNotification)) { _ in
      glassesCamera.stopForBackground()
    }
    .onDisappear {
      roboMemo.cancelRequests()
      cancelGlassesCapture()
    }
  }

  private var mainContent: some View {
    VStack(spacing: 16) {
      Picker("Sample", selection: $item) {
        Text("RoboMemo").tag(SampleAppItem.roboMemo)
        Text("Car guide").tag(SampleAppItem.carMaintenance)
      }
      .pickerStyle(.segmented)
      .padding(.top, 24)

      Image(systemName: item.iconName)
        .font(.system(size: 32, weight: .semibold))
        .foregroundStyle(.white)
        .frame(width: 72, height: 72)
        .background(item.iconBackground, in: RoundedRectangle(cornerRadius: 16))
        .padding(.top, 16)

      Text(item.title)
        .font(.title3.weight(.semibold))
        .foregroundStyle(.primary)
        .multilineTextAlignment(.center)

      Text(item.description)
        .font(.subheadline)
        .foregroundStyle(.secondary)
        .multilineTextAlignment(.center)

      if item == .roboMemo {
        RoboMemoAIView(
          model: roboMemo,
          glassesCamera: glassesCamera,
          isCameraBusy: isGlassesCameraBusy,
          isPhotoCaptureBusy: glassesCaptureTask != nil || glassesCamera.isCapturing,
          isLivePreviewBusy: glassesPreviewTask != nil || glassesCamera.isPreviewing,
          startLivePreview: { startGlassesLivePreview() },
          startRecording: { startGlassesLivePreview(recordVideo: true) },
          stopLivePreview: cancelGlassesCapture,
          captureUnavailable: isRoboMemoBusy || isDeveloperPreviewSetupBusy || displayViewModel.isSending,
          capturePhoto: capturePhotoFromGlasses,
          cancelCapture: cancelGlassesCapture,
          showVideoSummary: showVideoSummaryOnGlasses,
          isDisplayBusy: isDeveloperPreviewSetupBusy || displayViewModel.isSending
        )
      }

      Spacer()

      developerPreviewEntry
      if let standaloneDisplayErrorMessage {
        VStack(spacing: 4) {
          Label("Couldn’t start display", systemImage: "exclamationmark.circle.fill")
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.red)
          Text(verbatim: standaloneDisplayErrorMessage)
            .font(.caption)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true)
        }
      }
      if item == .roboMemo, roboMemo.selectedVideoSummary != nil {
        Text("Ready for glasses: video summary")
          .font(.caption).foregroundStyle(.secondary)
      }
      tryItButton
    }
    .padding(.horizontal, 24)
    .padding(.bottom, 16)
    .frame(maxWidth: .infinity)
  }

  private var developerPreviewEntry: some View {
    SwiftUI.Button(action: presentDeveloperPreview) {
      HStack(spacing: 14) {
        Image(systemName: "hammer.fill")
          .font(.title3)
          .foregroundStyle(.blue)
          .frame(width: 32)

        VStack(alignment: .leading, spacing: 3) {
          Text("Developer preview")
            .font(.body.weight(.semibold))
          Text("Preview and test the experience without physical glasses.")
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        }

        Spacer(minLength: 8)

        if isDeveloperPreviewSetupBusy {
          ProgressView()
        } else {
          Image(systemName: "chevron.right")
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.tertiary)
        }
      }
      .frame(maxWidth: .infinity, alignment: .leading)
      .padding(18)
      .background(
        Color(uiColor: .systemGroupedBackground),
        in: RoundedRectangle(cornerRadius: 20)
      )
    }
    .buttonStyle(.plain)
    .disabled(isDeveloperPreviewSetupBusy || isRoboMemoBusy)
  }

  private var developerPreviewSheet: some View {
    NavigationStack {
      ScrollView {
        VStack(spacing: 20) {
          Picker(
            "Preview surface",
            selection: Binding(
              get: { selectedDeveloperPreviewMode },
              set: { changeDeveloperPreviewMode($0) }
            )
          ) {
            ForEach(DeveloperPreviewMode.availableModes) { mode in
              Text(mode.title)
                .tag(mode)
            }
          }
          .pickerStyle(.segmented)
          .disabled(isDeveloperPreviewControlBusy)

          developerPreviewStage
          developerPreviewContentControl
        }
        .frame(maxWidth: 560)
        .padding(20)
        .frame(maxWidth: .infinity)
      }
      .background(Color(uiColor: .systemGroupedBackground))
      .navigationTitle("Developer preview")
      .navigationBarTitleDisplayMode(.inline)
      .toolbar {
        ToolbarItem(placement: .confirmationAction) {
          SwiftUI.Button("Done", action: dismissDeveloperPreview)
            .disabled(isDeveloperPreviewControlBusy)
        }
      }
    }
    .interactiveDismissDisabled(developerPreviewMode != nil || isDeveloperPreviewControlBusy)
  }

  @ViewBuilder
  private var developerPreviewStage: some View {
    switch selectedDeveloperPreviewMode {
    case .inApp:
      inAppStage
    case .chrome:
      chromeStage
    }
  }

  @ViewBuilder
  private var inAppStage: some View {
    if let phonePreviewDisplay {
      previewSurfaceCard {
        Label("In-app simulator", systemImage: "iphone")
          .font(.subheadline.weight(.semibold))

        GeometryReader { geometry in
          let side = min(geometry.size.width, geometry.size.height)
          PhoneDisplayPreview(display: phonePreviewDisplay)
            .id(phonePreviewDeviceIdentifier)
            .frame(width: side, height: side)
            .background(.black)
            .clipShape(RoundedRectangle(cornerRadius: 20))
            .overlay {
              RoundedRectangle(cornerRadius: 20)
                .stroke(.white.opacity(0.16))
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityLabel("In-app display simulator")
        }
      }
    } else {
      previewSurfaceCard {
        Label("In-app simulator", systemImage: "iphone")
          .font(.subheadline.weight(.semibold))

        Spacer()

        VStack(spacing: 12) {
          Image(systemName: "iphone")
            .font(.system(size: 42))
            .foregroundStyle(.blue)
          Text("Tap Preview in app to start.")
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)

        Spacer()
      }
    }
  }

  private var chromeStage: some View {
    previewSurfaceCard {
      Label("Preview in Chrome", systemImage: "desktopcomputer")
        .font(.subheadline.weight(.semibold))

      VStack(alignment: .leading, spacing: 20) {
        instructionRow(number: 1, text: "Start the preview server.")
        instructionRow(number: 2, text: "Open the preview link in Chrome.") {
          if let chromePreviewURL {
            let isCopied = copiedChromePreviewURL == chromePreviewURL
            HStack(spacing: 8) {
              Text(verbatim: chromePreviewURL.absoluteString)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)

              SwiftUI.Button {
                UIPasteboard.general.url = chromePreviewURL
                copiedChromePreviewURL = chromePreviewURL
              } label: {
                Label(
                  isCopied ? "Copied" : "Copy",
                  systemImage: isCopied ? "checkmark" : "doc.on.doc"
                )
              }
              .buttonStyle(.bordered)
              .controlSize(.small)
              .accessibilityHint("Copies the Chrome simulator URL")
              .task(id: copiedChromePreviewURL) {
                guard copiedChromePreviewURL != nil else { return }
                do {
                  try await Task.sleep(for: .seconds(2))
                } catch {
                  return
                }
                copiedChromePreviewURL = nil
              }
            }
          } else {
            Text("Available after the server starts.")
              .font(.caption)
              .foregroundStyle(.secondary)
          }
        }
        instructionRow(
          number: 3,
          text: "Enable the Meta Ray-Ban Display Simulator extension in Chrome."
        )
      }
      .padding(.leading, 8)

      Spacer(minLength: 0)
    }
  }

  private func previewSurfaceCard<Content: View>(
    @ViewBuilder content: () -> Content
  ) -> some View {
    VStack(alignment: .leading, spacing: 16) {
      content()
    }
    .padding(20)
    .frame(maxWidth: 392)
    .aspectRatio(1, contentMode: .fit)
    .background(.background, in: RoundedRectangle(cornerRadius: 24))
    .frame(maxWidth: .infinity)
  }

  private func instructionRow(
    number: Int,
    text: String
  ) -> some View {
    instructionRow(number: number, text: text) {
      EmptyView()
    }
  }

  private func instructionRow<Detail: View>(
    number: Int,
    text: String,
    @ViewBuilder detail: () -> Detail
  ) -> some View {
    HStack(alignment: .top, spacing: 12) {
      Text(verbatim: "\(number).")
        .font(.subheadline.monospacedDigit().weight(.semibold))
        .foregroundStyle(.secondary)
        .frame(width: 20, alignment: .trailing)

      VStack(alignment: .leading, spacing: 10) {
        Text(text)
          .font(.subheadline)
          .fixedSize(horizontal: false, vertical: true)
        detail()
      }
      .frame(maxWidth: .infinity, alignment: .leading)
    }
  }

  private var tryItButton: some View {
    SwiftUI.Button {
      Task { await sendSample(item, deviceIdentifier: nil) }
    } label: {
      Text(item == .roboMemo ? "Show on glasses" : "Try it")
        .font(.body.weight(.semibold))
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 20)
        .background(
          LinearGradient(
            colors: [Color(red: 0.30, green: 0.45, blue: 0.95), Color(red: 0.15, green: 0.25, blue: 0.85)],
            startPoint: .leading,
            endPoint: .trailing
          ),
          in: Capsule()
        )
    }
    .buttonStyle(.plain)
    .disabled(isRoboMemoBusy || displayViewModel.isSending)
  }

  @ViewBuilder
  private var developerPreviewContentControl: some View {
    if let statusTitle = developerPreviewStatusTitle {
      HStack(spacing: 10) {
        Image(systemName: "checkmark.circle.fill")
        Text(statusTitle)
          .font(.body.weight(.semibold))
      }
      .foregroundStyle(.green)
      .frame(maxWidth: .infinity)
      .padding(.vertical, 14)
      .background(.green.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
      .accessibilityElement(children: .combine)
    } else {
      VStack(spacing: 12) {
        if let selectedPreviewErrorMessage {
          VStack(spacing: 4) {
            Label("Couldn’t start preview", systemImage: "exclamationmark.circle.fill")
              .font(.subheadline.weight(.semibold))
              .foregroundStyle(.red)
            Text(verbatim: selectedPreviewErrorMessage)
              .font(.caption)
              .foregroundStyle(.secondary)
              .multilineTextAlignment(.center)
              .fixedSize(horizontal: false, vertical: true)
          }
        }

        SwiftUI.Button {
          Task { await startSelectedDeveloperPreview() }
        } label: {
          HStack(spacing: 10) {
            if isDeveloperPreviewControlBusy {
              ProgressView()
                .tint(.white)
            }
            Text(developerPreviewButtonTitle)
              .font(.body.weight(.semibold))
          }
          .frame(maxWidth: .infinity)
          .padding(.vertical, 8)
        }
        .buttonStyle(.borderedProminent)
        .disabled(!canLoadDeveloperPreview)
      }
    }
  }

  private var isDeveloperPreviewSetupBusy: Bool {
    isDeveloperPreviewTransitioning || isDeveloperPreviewChanging
  }

  private var isRoboMemoBusy: Bool {
    isGlassesCameraBusy || (item == .roboMemo && (roboMemo.isPhotoLoading || roboMemo.isAnalyzing))
  }

  private var isGlassesCameraBusy: Bool {
    glassesCaptureTask != nil || glassesPreviewTask != nil || glassesCamera.isBusy
  }

  private func capturePhotoFromGlasses() {
    guard glassesCaptureTask == nil, !isRoboMemoBusy, !displayViewModel.isSending else { return }
    roboMemo.cancelRequests()
    roboMemo.isPhotoLoading = true
    glassesCaptureTask = Task { @MainActor in
      defer {
        roboMemo.isPhotoLoading = false
        glassesCaptureTask = nil
      }
      guard await setDeveloperPreviewMode(nil), !Task.isCancelled else { return }
      if let data = await glassesCamera.capturePhoto(stopDisplaySession: displayViewModel.stopSession),
        !Task.isCancelled {
        roboMemo.photoData = data
      }
    }
  }

  private func startGlassesLivePreview(recordVideo: Bool = false) {
    guard !isRoboMemoBusy, !isDeveloperPreviewSetupBusy, !displayViewModel.isSending else { return }
    glassesPreviewTask = Task { @MainActor in
      defer { glassesPreviewTask = nil }
      guard await setDeveloperPreviewMode(nil), !Task.isCancelled else { return }
      await glassesCamera.startLivePreview(recordVideo: recordVideo, stopDisplaySession: displayViewModel.stopSession)
    }
  }

  private func showVideoSummaryOnGlasses(_ clipID: URL) {
    guard !isRoboMemoBusy, !isDeveloperPreviewSetupBusy, !displayViewModel.isSending else { return }
    roboMemo.selectVideoSummary(clipID)
    Task { @MainActor in
      guard await setDeveloperPreviewMode(nil) else { return }
      await sendSample(.roboMemo, deviceIdentifier: nil)
    }
  }

  private func cancelGlassesCapture() {
    glassesCamera.cancel()
    glassesCaptureTask?.cancel()
    glassesPreviewTask?.cancel()
  }

  private var isDeveloperPreviewControlBusy: Bool {
    isDeveloperPreviewSetupBusy || displayViewModel.isSending
  }

  private func presentDeveloperPreview() {
    selectedDeveloperPreviewMode = developerPreviewMode ?? DeveloperPreviewMode.defaultMode
    isDeveloperPreviewPresented = true
  }

  private func changeDeveloperPreviewMode(_ mode: DeveloperPreviewMode) {
    guard mode != selectedDeveloperPreviewMode else { return }
    let previousSelectedMode = selectedDeveloperPreviewMode
    selectedDeveloperPreviewMode = mode
    guard developerPreviewMode != nil else { return }

    isDeveloperPreviewTransitioning = true
    Task {
      let didDisablePreview = await setDeveloperPreviewMode(nil)
      isDeveloperPreviewTransitioning = false
      if !didDisablePreview {
        selectedDeveloperPreviewMode = developerPreviewMode ?? previousSelectedMode
      }
    }
  }

  private func dismissDeveloperPreview() {
    guard developerPreviewMode != nil else {
      isDeveloperPreviewPresented = false
      return
    }

    isDeveloperPreviewTransitioning = true
    Task {
      let didDisablePreview = await setDeveloperPreviewMode(nil)
      isDeveloperPreviewTransitioning = false
      if didDisablePreview {
        isDeveloperPreviewPresented = false
      }
    }
  }

  private var canLoadDeveloperPreview: Bool {
    guard !isDeveloperPreviewControlBusy, !hasLoadedSelectedPreview else { return false }

    guard developerPreviewErrorMessage == nil else { return true }
    guard let developerPreviewMode else { return true }
    guard developerPreviewMode == selectedDeveloperPreviewMode else { return false }

    switch selectedDeveloperPreviewMode {
    case .inApp:
      return phonePreviewDeviceIdentifier != nil && phonePreviewDisplay != nil
    case .chrome:
      return phonePreviewDeviceIdentifier != nil && chromePreviewURL != nil
    }
  }

  private var developerPreviewButtonTitle: String {
    if isDeveloperPreviewSetupBusy {
      return selectedDeveloperPreviewMode.preparingTitle
    }
    if displayViewModel.isSending {
      return selectedDeveloperPreviewMode.preparingTitle
    }
    if selectedPreviewErrorMessage != nil {
      return "Try again"
    }
    return selectedDeveloperPreviewMode.actionTitle
  }

  private var hasLoadedSelectedPreview: Bool {
    guard let phonePreviewDeviceIdentifier else { return false }
    return developerPreviewMode == selectedDeveloperPreviewMode
      && displayViewModel.lastSentPreviewDeviceIdentifier == phonePreviewDeviceIdentifier
  }

  private var developerPreviewStatusTitle: String? {
    if hasLoadedSelectedPreview {
      return selectedDeveloperPreviewMode.activeTitle
    }
    if selectedPreviewErrorMessage == nil,
      selectedDeveloperPreviewMode == .chrome,
      developerPreviewMode == .chrome,
      chromePreviewURL != nil
    {
      return "Preview server running"
    }
    return nil
  }

  private var standaloneDisplayErrorMessage: String? {
    guard !isDeveloperPreviewPresented, developerPreviewMode == nil else { return nil }
    return displayViewModel.errorMessage
  }

  private var selectedPreviewErrorMessage: String? {
    if let developerPreviewErrorMessage {
      return developerPreviewErrorMessage
    }
    guard developerPreviewMode == selectedDeveloperPreviewMode else { return nil }
    return displayViewModel.errorMessage
  }

  private func startSelectedDeveloperPreview() async {
    isDeveloperPreviewTransitioning = true
    let didPreparePreview = await setDeveloperPreviewMode(selectedDeveloperPreviewMode)
    isDeveloperPreviewTransitioning = false
    guard didPreparePreview else { return }

    await sendSample(item, deviceIdentifier: phonePreviewDeviceIdentifier)
  }

  private func sendSample(
    _ item: SampleAppItem,
    deviceIdentifier: DeviceIdentifier?
  ) async {
    switch item {
    case .roboMemo:
      if let summary = roboMemo.selectedVideoSummary {
        await displayViewModel.sendRoboMemoVideoSummary(summary.text, deviceIdentifier: deviceIdentifier)
      } else if let photoData = roboMemo.photoData {
        if let caption = roboMemo.caption {
          await displayViewModel.sendRoboMemoCaption(
            imageData: photoData, caption: caption, deviceIdentifier: deviceIdentifier
          )
        } else {
          await displayViewModel.sendRoboMemoPhoto(
            imageData: photoData, deviceIdentifier: deviceIdentifier
          )
        }
      } else {
        await displayViewModel.sendRoboMemoHello(deviceIdentifier: deviceIdentifier)
      }
    case .carMaintenance:
      await displayViewModel.sendCarMaintenanceTutorialList(
        deviceIdentifier: deviceIdentifier
      )
    }
  }

}

private extension DeveloperPreviewMode {
  static var availableModes: [Self] {
    #if targetEnvironment(simulator)
    allCases
    #else
    [.inApp]
    #endif
  }

  static var defaultMode: Self {
    availableModes[0]
  }

  var title: String {
    switch self {
    case .inApp: "In app"
    case .chrome: "Chrome"
    }
  }

  var actionTitle: String {
    switch self {
    case .inApp: "Preview in app"
    case .chrome: "Start preview server"
    }
  }

  var preparingTitle: String {
    switch self {
    case .inApp: "Starting in-app preview…"
    case .chrome: "Starting preview server…"
    }
  }

  var activeTitle: String {
    switch self {
    case .inApp: "Previewing in app"
    case .chrome: "Preview ready in Chrome"
    }
  }
}

private struct PhoneDisplayPreview: UIViewRepresentable {
  let display: any MockDisplayKit

  func makeUIView(context: Context) -> UIView {
    display.createPreviewView()
  }

  func updateUIView(_ uiView: UIView, context: Context) {}
}
