/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

//
// RegistrationView.swift
//
// Handles deep link callbacks from the Meta AI app during DAT SDK registration
// and permission flows.
//

import MWDATCore
import SwiftUI

struct RegistrationView: ViewModifier {
  var viewModel: WearablesViewModel

  func body(content: Content) -> some View {
    content
      .onOpenURL { url in
        Task {
          await viewModel.handleRegistrationURL(url)
        }
      }
  }
}
