import Foundation
import MWDATCore

public enum MotionSamplingRate: Int, Sendable { case hz5 = 5, hz10 = 10, hz15 = 15, hz24 = 24, hz30 = 30, hz60 = 60 }
public struct MotionConfiguration: Sendable {
  public let samplingRate: MotionSamplingRate
  public init(samplingRate: MotionSamplingRate) { self.samplingRate = samplingRate }
}
public enum MotionState: Sendable { case stopped, starting, started, stopping
  public var description: String { String(reflecting: self) }
}
public enum MotionError: Error, Sendable { case sensorUnavailable, capabilityClosed, deviceDisconnected }
public final class Motion: @unchecked Sendable {
  public let statePublisher = TestAnnouncer<MotionState>()
  public let errorPublisher = TestAnnouncer<MotionError>()
  public let samples: AsyncStream<MotionSample>
  private let continuation: AsyncStream<MotionSample>.Continuation
  private let lock = NSLock()
  private var currentState: MotionState = .stopped
  private var cancelled = false
  private let plan: String
  public var state: MotionState { lock.withLock { currentState } }
  public init(plan: String) {
    self.plan = plan
    (samples, continuation) = AsyncStream<MotionSample>.makeStream()
  }
  private func transition(_ value: MotionState) {
    lock.withLock { currentState = value }; statePublisher.emit(value)
  }
  public func start() {
    transition(.starting)
    DispatchQueue.global().asyncAfter(deadline: .now() + 0.025) { [self] in
      guard !lock.withLock({ cancelled }) else { return }
      switch plan {
      case "unavailable":
        errorPublisher.emit(.sensorUnavailable); transition(.stopped)
      case "stopped_before_error":
        transition(.stopped)
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.075) { [self] in errorPublisher.emit(.sensorUnavailable) }
      case "never_started": break
      case "starts_then_unavailable":
        transition(.started)
        continuation.yield(MotionSample(timestampNs: 10, accelerometer: Vector3(x: 1, y: 2, z: 3), source: .glasses))
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.25) { [self] in
          guard !lock.withLock({ cancelled }) else { return }
          errorPublisher.emit(.sensorUnavailable); transition(.stopped)
        }
      default:
        transition(.started)
        continuation.yield(MotionSample(timestampNs: 10, accelerometer: Vector3(x: 1, y: 2, z: 3), source: .glasses))
      }
    }
  }
  public func stop() {
    lock.withLock { cancelled = true }
    transition(.stopped); continuation.finish()
  }
}
extension DeviceSession {
  public func addMotion(configuration: MotionConfiguration) throws(DeviceSessionError) -> Motion? {
    guard !activeMotion else { throw .capabilityAlreadyActive }
    activeMotion = true
    attempts.append(configuration.samplingRate.rawValue)
    return Motion(plan: plans.isEmpty ? "success" : plans.removeFirst())
  }
  public func removeMotion() throws(DeviceSessionError) { activeMotion = false; removals += 1 }
}
