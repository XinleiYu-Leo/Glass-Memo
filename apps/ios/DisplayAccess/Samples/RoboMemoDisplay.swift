import MWDATDisplay
import UIKit

enum RoboMemoDisplay {
  static func photo(
    image: UIImage,
    confirmed: Bool,
    hasCaption: Bool = false,
    onConfirm: @escaping @Sendable () -> Void
  ) -> FlexBox {
    // A fixed aspect ratio leaves room for the title and button even for portrait photos.
    let canvasSize = CGSize(width: 560, height: 280)
    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    format.opaque = true
    let canvas = UIGraphicsImageRenderer(size: canvasSize, format: format).image { context in
      UIColor.black.setFill()
      context.fill(CGRect(origin: .zero, size: canvasSize))
      let scale = min(canvasSize.width / image.size.width, canvasSize.height / image.size.height)
      let imageSize = CGSize(width: image.size.width * scale, height: image.size.height * scale)
      image.draw(in: CGRect(
        x: (canvasSize.width - imageSize.width) / 2,
        y: (canvasSize.height - imageSize.height) / 2,
        width: imageSize.width,
        height: imageSize.height
      ))
    }

    return FlexBox(direction: .column, spacing: 12) {
      Text(confirmed ? "Photo confirmed" : "RoboMemo · Photo", style: .heading)
      Image(image: canvas, sizePreset: .fill, cornerRadius: .medium)
      Text(hasCaption ? "AI description available" : "Photo preview · Not analyzed yet", style: .meta, color: .secondary)
      Button(
        label: hasCaption ? "Read description" : (confirmed ? "Back to photo" : "Confirm photo"),
        style: .primary,
        onClick: onConfirm
      )
      .actionRole(.primary)
    }
    .padding(16)
  }

  static func caption(
    text: String,
    page: Int,
    pageCount: Int,
    onPhoto: @escaping @Sendable () -> Void,
    onPrevious: @escaping @Sendable () -> Void,
    onNext: @escaping @Sendable () -> Void
  ) -> FlexBox {
    FlexBox(direction: .column, spacing: 16) {
      Text("RoboMemo", style: .heading)
      Text("AI description · \(page + 1)/\(pageCount)", style: .meta, color: .secondary)
      Text(text, style: .body)
      ButtonGroup {
        if page + 1 < pageCount {
          Button(label: "Photo", style: .secondary, onClick: onPhoto)
        } else {
          Button(label: "Photo", style: .primary, onClick: onPhoto)
            .actionRole(.primary)
        }
        if page > 0 {
          Button(label: "Previous", style: .secondary, onClick: onPrevious)
        }
        if page + 1 < pageCount {
          Button(label: "Next", style: .primary, onClick: onNext)
            .actionRole(.primary)
        }
      }
    }
    .padding(24)
  }

  static func videoSummary(
    text: String,
    page: Int,
    pageCount: Int,
    onPrevious: @escaping @Sendable () -> Void,
    onNext: @escaping @Sendable () -> Void,
    onFirst: @escaping @Sendable () -> Void
  ) -> FlexBox {
    FlexBox(direction: .column, spacing: 16) {
      Text("RoboMemo · Video", style: .heading)
      Text("AI summary · Sampled frames · \(page + 1)/\(pageCount)", style: .meta, color: .secondary)
      Text(text, style: .body)
      ButtonGroup {
        if page > 0 {
          Button(label: "Previous", style: .secondary, onClick: onPrevious)
        }
        if page + 1 < pageCount {
          Button(label: "Next", style: .primary, onClick: onNext).actionRole(.primary)
        } else {
          Button(label: "First page", style: .primary, onClick: onFirst).actionRole(.primary)
        }
      }
    }
    .padding(24)
  }

  static func hello(
    confirmed: Bool,
    onConfirm: @escaping @Sendable () -> Void
  ) -> FlexBox {
    FlexBox(direction: .column, spacing: 16) {
      FlexBox(direction: .column, spacing: 12) {
        Text(confirmed ? "Tap received" : "Hello RoboMemo", style: .heading)
        Text(
          confirmed ? "RoboMemo received your tap." : "My first glasses interface",
          style: .body
        )
        Text(
          confirmed ? "Tap below to try again." : "Tap Confirm to see the screen change.",
          style: .meta,
          color: .secondary
        )
      }
      .padding(24)
      .background(.card)

      Button(
        label: confirmed ? "Try again" : "Confirm",
        style: .primary,
        onClick: onConfirm
      )
      .actionRole(.primary)
    }
    .padding(16)
  }
}
