/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

//
// SettingsView.swift
//
// Registration and device connection screen. Shows glasses registration state
// and lists connected devices with real-time link state updates.
//

import MWDATCore
import SwiftUI

struct SettingsViewModel {
  let registrationState: RegistrationState
  let deviceItemStates: [DeviceItemState]
  let requiresFirmwareUpdate: Bool
  let requiresDATAppUpdate: Bool
  let isRegistrationActionInFlight: Bool
  let registrationEvents: [String]
  private let connectGlassesAction: () -> Void
  private let disconnectGlassesAction: () -> Void
  private let openFirmwareUpdateAction: () -> Void
  private let openDATGlassesAppUpdateAction: () -> Void

  init(
    registrationState: RegistrationState,
    deviceItemStates: [DeviceItemState],
    requiresFirmwareUpdate: Bool,
    requiresDATAppUpdate: Bool,
    isRegistrationActionInFlight: Bool = false,
    registrationEvents: [String] = [],
    connectGlasses: @escaping () -> Void,
    disconnectGlasses: @escaping () -> Void,
    openFirmwareUpdate: @escaping () -> Void,
    openDATGlassesAppUpdate: @escaping () -> Void
  ) {
    self.registrationState = registrationState
    self.deviceItemStates = deviceItemStates
    self.requiresFirmwareUpdate = requiresFirmwareUpdate
    self.requiresDATAppUpdate = requiresDATAppUpdate
    self.isRegistrationActionInFlight = isRegistrationActionInFlight
    self.registrationEvents = registrationEvents
    self.connectGlassesAction = connectGlasses
    self.disconnectGlassesAction = disconnectGlasses
    self.openFirmwareUpdateAction = openFirmwareUpdate
    self.openDATGlassesAppUpdateAction = openDATGlassesAppUpdate
  }

  var hasCompatibilityIssue: Bool {
    requiresFirmwareUpdate || requiresDATAppUpdate
  }

  func connectGlasses() {
    connectGlassesAction()
  }

  func disconnectGlasses() {
    disconnectGlassesAction()
  }

  func openFirmwareUpdate() {
    openFirmwareUpdateAction()
  }

  func openDATGlassesAppUpdate() {
    openDATGlassesAppUpdateAction()
  }
}

struct SettingsView: View {
  let viewModel: SettingsViewModel

  var body: some View {
    List {
      systemSection
      devicesSection
      if !viewModel.registrationEvents.isEmpty {
        Section {
          DisclosureGroup("Connection details") {
            Text(viewModel.registrationEvents.joined(separator: "\n\n"))
              .font(.footnote)
              .textSelection(.enabled)
          }
        } footer: {
          Text("If registration fails, these details help identify which step did not finish.")
        }
      }
    }
    .navigationTitle("Settings")
    .navigationBarTitleDisplayMode(.inline)
  }

  // MARK: - System

  private var systemSection: some View {
    Section("System") {
      HStack {
        registrationIcon
        Text(registrationLabel)
          .foregroundStyle(registrationColor)
        Spacer()
        registrationAction
          .disabled(viewModel.isRegistrationActionInFlight)
      }

      if viewModel.hasCompatibilityIssue {
        CompatibilityIssueCard(
          showFirmwareUpdate: viewModel.requiresFirmwareUpdate,
          showDATAppUpdate: viewModel.requiresDATAppUpdate,
          onOpenFirmwareUpdate: viewModel.openFirmwareUpdate,
          onOpenDATAppUpdate: viewModel.openDATGlassesAppUpdate
        )
      }
    }
  }

  private var registrationIcon: some View {
    Group {
      switch viewModel.registrationState {
      case .unavailable:
        Image(systemName: "xmark.circle.fill")
          .foregroundStyle(.red)
      case .available:
        Image(systemName: "xmark.circle.fill")
          .foregroundStyle(.yellow)
      case .registering:
        Image(systemName: "ellipsis.circle.fill")
          .foregroundStyle(.orange)
      case .registered:
        Image(systemName: "checkmark.circle.fill")
          .foregroundStyle(.green)
      @unknown default:
        Image(systemName: "questionmark.circle.fill")
          .foregroundStyle(.gray)
      }
    }
  }

  private var registrationLabel: String {
    switch viewModel.registrationState {
    case .unavailable:
      "Unavailable"
    case .available:
      "Available"
    case .registering:
      "Registering..."
    case .registered:
      "Registered"
    @unknown default:
      "Unknown"
    }
  }

  private var registrationColor: Color {
    switch viewModel.registrationState {
    case .registered:
      .green
    case .registering:
      .orange
    case .unavailable:
      .red
    case .available:
      .yellow
    @unknown default:
      .gray
    }
  }

  @ViewBuilder
  private var registrationAction: some View {
    switch viewModel.registrationState {
    case .registered:
      SwiftUI.Button {
        viewModel.disconnectGlasses()
      } label: {
        Image(systemName: "trash")
          .foregroundStyle(.red)
      }
      .buttonStyle(.plain)
    case .unavailable, .available:
      SwiftUI.Button {
        viewModel.connectGlasses()
      } label: {
        Text("Register")
          .font(.subheadline.weight(.semibold))
          .foregroundStyle(.white)
          .padding(.horizontal, 20)
          .padding(.vertical, 8)
          .background(.blue, in: Capsule())
      }
      .buttonStyle(.plain)
    case .registering:
      ProgressView()
    @unknown default:
      EmptyView()
    }
  }

  // MARK: - Devices

  private var devicesSection: some View {
    Section("Devices") {
      if viewModel.deviceItemStates.isEmpty {
        Text("No devices found")
          .foregroundStyle(.secondary)
      } else {
        ForEach(viewModel.deviceItemStates) { state in
          DeviceRow(state: state)
        }
      }
    }
  }
}

// MARK: - DeviceRow

private struct DeviceRow: View {
  var state: DeviceItemState

  var body: some View {
    HStack {
      VStack(alignment: .leading) {
        Text(state.deviceName)
          .font(.headline)
        Text(state.deviceTypeValue)
          .font(.subheadline)
          .foregroundStyle(.secondary)
        Text(state.identifier)
          .font(.caption)
          .foregroundStyle(.secondary)
      }
      Spacer()
      statusLabel
    }
  }

  private var statusLabel: some View {
    Text(statusText)
      .font(.subheadline)
      .fontWeight(.medium)
      .foregroundStyle(statusColor)
  }

  private var statusText: String {
    if state.compatibility == .deviceUpdateRequired {
      return "Update required"
    }

    switch state.linkState {
    case .disconnected:
      return "Disconnected"
    case .connecting:
      return "Connecting"
    case .connected:
      return "Connected"
    @unknown default:
      return "Unknown"
    }
  }

  private var statusColor: Color {
    if state.compatibility == .deviceUpdateRequired {
      return CompatibilityIssueCard.issueColor
    }

    switch state.linkState {
    case .disconnected:
      return .red
    case .connecting:
      return .yellow
    case .connected:
      return .green
    @unknown default:
      return .gray
    }
  }
}

// MARK: - CompatibilityIssueCard

private struct CompatibilityIssueCard: View {
  static let issueColor = Color(red: 0.54, green: 0.29, blue: 0.0)

  var showFirmwareUpdate: Bool
  var showDATAppUpdate: Bool
  var onOpenFirmwareUpdate: () -> Void
  var onOpenDATAppUpdate: () -> Void

  private static let issueBackground = Color(red: 1.0, green: 0.96, blue: 0.84)

  var body: some View {
    VStack(alignment: .leading, spacing: 14) {
      HStack(alignment: .top, spacing: 12) {
        Image(systemName: "exclamationmark.triangle.fill")
          .font(.title2)
          .foregroundStyle(Self.issueColor)

        VStack(alignment: .leading, spacing: 4) {
          Text(issueTitle)
            .font(.title3.weight(.semibold))
            .foregroundStyle(Self.issueColor)
          Text(issueMessage)
            .font(.subheadline)
            .foregroundStyle(Self.issueColor)
        }
      }

      VStack(spacing: 12) {
        if showFirmwareUpdate {
          compatibilityActionButton(updateFirmwareTitle, action: onOpenFirmwareUpdate)
        }

        if showDATAppUpdate {
          compatibilityActionButton(updateDATAppTitle, action: onOpenDATAppUpdate)
        }
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .padding(20)
    .background(Self.issueBackground, in: RoundedRectangle(cornerRadius: 26))
  }

  private var issueTitle: String {
    "Compatibility issue"
  }

  private var issueMessage: String {
    switch (showFirmwareUpdate, showDATAppUpdate) {
    case (true, true):
      return "Your glasses firmware and app need updates before Display Access can start."
    case (true, false):
      return "Your glasses firmware needs an update before Display Access can start."
    case (false, true):
      return "The app on your glasses needs an update before Display Access can start."
    case (false, false):
      return ""
    }
  }

  private var updateFirmwareTitle: String {
    "Update firmware"
  }

  private var updateDATAppTitle: String {
    "Update app on glasses"
  }

  private func compatibilityActionButton(_ title: String, action: @escaping () -> Void) -> some View {
    SwiftUI.Button(action: action) {
      Text(title)
        .font(.body.weight(.semibold))
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 12)
        .background(Self.issueColor, in: Capsule())
    }
    .buttonStyle(.plain)
  }
}
