/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

//
// DisplayAccessApp.swift
//
// Main entry point for the DisplayAccess app demonstrating the Meta Wearables DAT SDK
// Display module. This app shows how to send visual content to wearable display devices.
//

import MWDATCore
import SwiftUI

private enum AppTab: Hashable {
  case samples
  case settings
}

@main
struct DisplayAccessApp: App {
  @State private var wearablesViewModel: WearablesViewModel?
  @State private var displayViewModel: DisplayViewModel?
  @State private var selectedTab: AppTab = .samples
  private let configurationError: String?

  init() {
    var setupError: WearablesError?
    do {
      try Wearables.configure()
    } catch .alreadyConfigured {
      // SwiftUI can recreate the app value; the existing SDK is ready to use.
    } catch {
      setupError = error
    }

    if let setupError {
      configurationError = "\(setupError.description) (WearablesError \(setupError.rawValue))"
      self._wearablesViewModel = State(wrappedValue: nil)
      self._displayViewModel = State(wrappedValue: nil)
    } else {
      configurationError = nil
      let wearables = Wearables.shared
      self._wearablesViewModel = State(wrappedValue: WearablesViewModel(wearables: wearables))
      self._displayViewModel = State(wrappedValue: DisplayViewModel(wearables: wearables))
    }
  }

  var body: some Scene {
    WindowGroup {
      if let wearablesViewModel, let displayViewModel {
        appContent(wearablesViewModel: wearablesViewModel, displayViewModel: displayViewModel)
      } else {
        ContentUnavailableView(
          "Unable to start Display Access",
          systemImage: "exclamationmark.triangle",
          description: Text(configurationError ?? "The glasses connection could not be initialized.")
        )
      }
    }
  }

  private func appContent(
    wearablesViewModel: WearablesViewModel,
    displayViewModel: DisplayViewModel
  ) -> some View {
    @Bindable var errorState = wearablesViewModel
    return TabView(selection: $selectedTab) {
      NavigationStack {
        SampleAppsView(
          displayViewModel: displayViewModel,
          developerPreviewMode: wearablesViewModel.developerPreviewMode,
          isDeveloperPreviewChanging: wearablesViewModel.isDeveloperPreviewChanging,
          phonePreviewDisplay: wearablesViewModel.phonePreviewDisplay,
          phonePreviewDeviceIdentifier: wearablesViewModel.phonePreviewDeviceIdentifier,
          chromePreviewURL: wearablesViewModel.chromePreviewURL,
          developerPreviewErrorMessage: wearablesViewModel.developerPreviewErrorMessage,
          setDeveloperPreviewMode: { mode in
            await wearablesViewModel.setDeveloperPreviewMode(
              mode,
              stopDisplaySession: displayViewModel.stopSession
            )
          }
        )
      }
      .tabItem {
        Label("Samples", systemImage: "eyeglasses")
      }
      .tag(AppTab.samples)

      NavigationStack {
        SettingsView(
          viewModel: SettingsViewModel(
            registrationState: wearablesViewModel.registrationState,
            deviceItemStates: wearablesViewModel.deviceItemStates,
            requiresFirmwareUpdate: wearablesViewModel.requiresFirmwareUpdate,
            requiresDATAppUpdate: displayViewModel.requiresDATAppUpdate,
            isRegistrationActionInFlight: wearablesViewModel.isRegistrationActionInFlight,
            registrationEvents: wearablesViewModel.registrationEvents,
            connectGlasses: {
              Task {
                await wearablesViewModel.connectGlasses()
              }
            },
            disconnectGlasses: {
              Task {
                await wearablesViewModel.disconnectGlasses()
              }
            },
            openFirmwareUpdate: {
              wearablesViewModel.openFirmwareUpdate()
            },
            openDATGlassesAppUpdate: {
              wearablesViewModel.openDATGlassesAppUpdate()
            }
          )
        )
      }
      .tabItem {
        Label("Settings", systemImage: "gearshape")
      }
      .tag(AppTab.settings)
    }
    .alert("Error", isPresented: $errorState.showError) {
      Button("OK") { wearablesViewModel.dismissError() }
    } message: {
      Text(wearablesViewModel.errorMessage)
    }
    .modifier(RegistrationView(viewModel: wearablesViewModel))
  }
}
