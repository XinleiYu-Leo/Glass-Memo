import Foundation

public protocol AnyListenerToken: Sendable { func cancel() async }
public protocol Announcer<Element>: Sendable {
  associatedtype Element: Sendable
  func listen(_ listener: @escaping @Sendable (Element) -> Void) -> any AnyListenerToken
}
public final class TestToken: AnyListenerToken, @unchecked Sendable {
  private let action: @Sendable () -> Void
  public init(_ action: @escaping @Sendable () -> Void) { self.action = action }
  public func cancel() async { action() }
}
public final class TestAnnouncer<Element: Sendable>: Announcer, @unchecked Sendable {
  private let lock = NSLock()
  private var listeners: [UUID: @Sendable (Element) -> Void] = [:]
  public init() {}
  public var listenerCount: Int { lock.withLock { listeners.count } }
  public func listen(_ listener: @escaping @Sendable (Element) -> Void) -> any AnyListenerToken {
    let id = UUID()
    lock.withLock { listeners[id] = listener }
    return TestToken { [weak self] in _ = self?.lock.withLock { self?.listeners.removeValue(forKey: id) } }
  }
  public func emit(_ value: Element) { for listener in lock.withLock({ Array(listeners.values) }) { listener(value) } }
}
public enum DeviceSessionError: Error, Sendable { case capabilityAlreadyActive, capabilityNotFound }
public final class DeviceSession: @unchecked Sendable {
  // Accessed only on the controller's MainActor in tests.
  public var plans: [String]
  public var attempts: [Int] = []
  public var activeMotion = false
  public var removals = 0
  public init(plans: [String]) { self.plans = plans }
}
public final class Device: Sendable {
  public let statePublisher = TestAnnouncer<DeviceState>()
  public init() {}
  public func addDeviceStateListener(_ listener: @escaping @Sendable (DeviceState) -> Void) -> any AnyListenerToken {
    listener(DeviceState(batteryLevel: 90))
    return statePublisher.listen(listener)
  }
}
