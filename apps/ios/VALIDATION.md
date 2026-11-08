# 交付副本验证

验证针对本目录中的脱敏源码，未安装到任何手机，未修改原工作项目。环境：macOS / Apple Silicon，Xcode 26.6（17F113），Meta Wearables DAT 1.0.1。

| 检查 | 结果与范围 |
|---|---|
| 真实 iOS SDK 编译 | `DisplayAccess` Debug / generic iOS / `CODE_SIGNING_ALLOWED=NO`，BUILD SUCCEEDED |
| 录制器回归 | 195 项通过；真实录制器源码 + macOS AVFoundation + SDK 数据替身 |
| 控制器回归 | 37 项通过；真实控制器/录制器源码 + 异步 SDK 行为替身 |
| 生成 App 配置 | Bundle ID 和 URL scheme 为 `org.example.robomemo`；AppLink 为 `org.example.robomemo://`；Team/ClientToken 为空、MetaAppID 为 0 |
| 隐私检查 | 未发现原作者姓名/用户名、主目录路径、签名团队、个人 bundle ID、私网地址或密钥值；无用户媒体/设备日志/SDK 二进制 |

录制器最初在受限进程中因 macOS 无法分配合成视频缓冲而退出；允许正常 AVFoundation 系统访问后完整 195 项通过。不能把第一次环境失败解读为业务逻辑通过，也没有改断言绕过它。

复制后的核心源码与当前工作版一致；改变的 App 内容是 bundle/签名/回调配置以及一个诊断日志 subsystem（统一为示例 ID）。另增加可移植测试 runner 与协作说明。测试运行日志、合成媒体、DerivedData 留在交付目录外，不打包。以下核心源码 SHA-256 可用于核对；不包含原电脑路径。

| 文件 | SHA-256 |
|---|---|
| RoboMemoVideoRecorder.swift | `409a0b68dc16aa1a6e76f6de4c4b823365c6deadbb37cc4588c9a234a64c300b` |
| RoboMemoSensorRecorder.swift | `6e781ddca0a20beebebdd0107d1786acd7fa3d23713ed27b1e31bc4d8e027db6` |
| RoboMemoMultiSensorController.swift | `0b390546d947f7a2b1e367a749fa0223500cd3995104b2b497c5f634f7355f6b` |

通过这些检查说明项目能编译且回归场景通过；不代表签名安装、眼镜固件能力、IMU 连续性、跨传感器物理同步或 App Store 发布已通过。
