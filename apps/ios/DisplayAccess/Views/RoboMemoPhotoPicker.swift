import CoreTransferable
import ImageIO
import PhotosUI
import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct RoboMemoPhotoPicker: View {
  @Binding var photoData: Data?
  @Binding var isLoading: Bool
  var showsLoadingProgress = true
  @State private var selectedItem: PhotosPickerItem?
  @State private var errorMessage: String?

  var body: some View {
    let pickerTitle = photoData == nil ? "Choose photo" : "Change photo"
    return VStack(alignment: .leading, spacing: 8) {
      HStack(spacing: 14) {
        Group {
          if let photoData, let image = UIImage(data: photoData) {
            Image(uiImage: image)
              .resizable()
              .scaledToFill()
          } else {
            Image(systemName: "photo")
              .font(.title)
              .foregroundStyle(.blue)
          }
        }
        .frame(width: 64, height: 64)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .accessibilityLabel(photoData == nil ? "No photo selected" : "Selected photo")

        VStack(alignment: .leading, spacing: 6) {
          PhotosPicker(
            selection: $selectedItem,
            matching: .images,
            preferredItemEncoding: .current
          ) {
            Label(pickerTitle, systemImage: "photo.on.rectangle")
          }
          .buttonStyle(.bordered)
          .disabled(isLoading)

          if isLoading && showsLoadingProgress {
            HStack {
              ProgressView("Loading photo…")
                .font(.caption)
              Button("Cancel") { selectedItem = nil }
                .font(.caption)
            }
          } else if photoData != nil {
            Button("Remove photo") {
              selectedItem = nil
              photoData = nil
              errorMessage = nil
            }
            .font(.caption)
          } else {
            Text("Choose a photo to analyze or preview.")
              .font(.caption)
              .foregroundStyle(.secondary)
          }
        }
        Spacer(minLength: 0)
      }

      if let errorMessage {
        Text(errorMessage)
          .font(.caption)
          .foregroundStyle(.red)
      }
    }
    .padding(14)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
    .task(id: selectedItem) {
      await loadSelectedPhoto()
    }
  }

  private func loadSelectedPhoto() async {
    guard let selection = selectedItem else {
      isLoading = false
      return
    }
    isLoading = true
    errorMessage = nil
    defer {
      if selectedItem == selection { isLoading = false }
    }

    do {
      guard let photo = try await selection.loadTransferable(type: RoboMemoImportedPhoto.self) else {
        throw RoboMemoPhotoLoadingError.unreadable
      }
      try Task.checkCancellation()
      guard selectedItem == selection else { return }
      photoData = photo.thumbnailData
      isLoading = false
      selectedItem = nil
    } catch {
      guard !Task.isCancelled, selectedItem == selection else { return }
      errorMessage = "Could not load the photo. Choose a photo that is downloaded to this iPhone."
    }
  }

}

private enum RoboMemoPhotoLoadingError: Error {
  case unreadable
}

private struct RoboMemoImportedPhoto: Transferable, Sendable {
  let thumbnailData: Data

  static var transferRepresentation: some TransferRepresentation {
    FileRepresentation(importedContentType: .image) { received in
      // The received file stays valid until this importing closure returns.
      // Only the small JPEG leaves this scope, never the original image bytes.
      let thumbnail = try await RoboMemoPhotoThumbnailer.shared.thumbnail(from: received.file)
      return Self(thumbnailData: thumbnail)
    }
  }
}

actor RoboMemoPhotoThumbnailer {
  static let shared = RoboMemoPhotoThumbnailer()

  // A synchronous actor method serializes ImageIO work across cancelled/replaced selections.
  func thumbnail(from url: URL) throws -> Data {
    try Task.checkCancellation()
    guard let source = CGImageSourceCreateWithURL(
      url as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary
    ) else { throw RoboMemoPhotoLoadingError.unreadable }
    return try thumbnail(from: source)
  }

  func thumbnail(from data: Data) throws -> Data {
    try Task.checkCancellation()
    guard !data.isEmpty, data.count <= 25 * 1024 * 1024,
      let source = CGImageSourceCreateWithData(
        data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary
      ) else { throw RoboMemoPhotoLoadingError.unreadable }
    return try thumbnail(from: source)
  }

  private func thumbnail(from source: CGImageSource) throws -> Data {
    let options: [CFString: Any] = [
      kCGImageSourceCreateThumbnailFromImageAlways: true,
      kCGImageSourceCreateThumbnailWithTransform: true,
      kCGImageSourceThumbnailMaxPixelSize: 600,
    ]
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
      throw RoboMemoPhotoLoadingError.unreadable
    }
    try Task.checkCancellation()
    let output = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(
      output, UTType.jpeg.identifier as CFString, 1, nil
    ) else { throw RoboMemoPhotoLoadingError.unreadable }
    CGImageDestinationAddImage(
      destination, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary
    )
    guard CGImageDestinationFinalize(destination) else { throw RoboMemoPhotoLoadingError.unreadable }
    try Task.checkCancellation()
    return output as Data
  }
}
