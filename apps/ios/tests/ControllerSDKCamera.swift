import AVFoundation
import MWDATCore

public struct AudioFrame: @unchecked Sendable {
  public let pcmBuffer: AVAudioPCMBuffer
  public let presentationTimeStamp: CMTime
  public init(_ buffer: AVAudioPCMBuffer) { pcmBuffer = buffer; presentationTimeStamp = .zero }
}
public final class Stream: Sendable {
  public let audioFramePublisher = TestAnnouncer<AudioFrame>()
  public init() {}
}
