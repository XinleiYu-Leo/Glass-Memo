// Test-only transport values matching SDK 1.0.1 public field shapes.
// No behavior, networking, sensor simulation, or replacement production code.
public enum LinkState: Sendable, Equatable { case disconnected, connecting, connected }
public enum Compatibility: Sendable, Equatable { case undefined, compatible, deviceUpdateRequired, sdkUpdateRequired }
public enum ChargingState: Sendable, Equatable { case unknown, charging, notCharging }
public enum DonState: Sendable, Equatable { case unknown, doffed, donned }
public enum HingeState: Sendable, Equatable { case unknown, closed, open }
public enum ThermalLevel: Sendable, Equatable { case unknown, none, light, moderate, severe, critical, emergency, shutdown }
public struct DeviceState: Sendable, Equatable {
  public var linkState: LinkState
  public var compatibility: Compatibility
  public var batteryLevel: Int?
  public var chargingState: ChargingState
  public var donState: DonState
  public var hingeState: HingeState
  public var thermalLevel: ThermalLevel
  public init(linkState: LinkState = .disconnected, compatibility: Compatibility = .undefined,
              batteryLevel: Int? = nil, chargingState: ChargingState = .unknown,
              donState: DonState = .unknown, hingeState: HingeState = .unknown,
              thermalLevel: ThermalLevel = .unknown) {
    self.linkState = linkState; self.compatibility = compatibility; self.batteryLevel = batteryLevel
    self.chargingState = chargingState; self.donState = donState; self.hingeState = hingeState
    self.thermalLevel = thermalLevel
  }
}
