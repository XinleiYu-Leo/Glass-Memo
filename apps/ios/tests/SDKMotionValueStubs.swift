// Test-only SDK value types. The real app build checks actual SDK linkage/API.
public struct Vector3: Sendable {
  public let x: Float; public let y: Float; public let z: Float
  public init(x: Float, y: Float, z: Float) { self.x = x; self.y = y; self.z = z }
}
public struct Quaternion: Sendable {
  public let x: Float; public let y: Float; public let z: Float; public let w: Float
  public init(x: Float, y: Float, z: Float, w: Float) { self.x = x; self.y = y; self.z = z; self.w = w }
}
public enum MotionSource: Sendable { case glasses, neuralBand, unknown }
public struct MotionSample: Sendable {
  public let timestampNs: Int64
  public let accelerometer: Vector3?
  public let gyroscope: Vector3?
  public let magnetometer: Vector3?
  public let orientation: Quaternion?
  public let source: MotionSource
  public init(timestampNs: Int64, accelerometer: Vector3? = nil, gyroscope: Vector3? = nil,
              magnetometer: Vector3? = nil, orientation: Quaternion? = nil, source: MotionSource = .unknown) {
    self.timestampNs = timestampNs; self.accelerometer = accelerometer; self.gyroscope = gyroscope
    self.magnetometer = magnetometer; self.orientation = orientation; self.source = source
  }
}
