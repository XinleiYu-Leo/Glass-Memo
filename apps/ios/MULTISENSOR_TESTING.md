# 多传感器实现、时钟与实验边界

本说明对应当前 SDK 1.0.1 实现；普通录像默认不启用多传感器。

## API 与失败处理

- 依赖精确锁定 1.0.1（revision `0bd369298eef6a980ad0bdb6c8a5502f2c02a08c`），新增 `MWDATMotion` 产品。iOS 1.0.1 更新日志是 “No developer-facing changes”；HEVC 冻结修复是 Android 更新日志条目，不能用于声称本 iOS 升级修复了冻结。当前视频仍是 SDK `.raw` → App H.264 MOV。
- iOS 流内音频是 `StreamConfiguration.audioCodec`、`.pcm(sampleRate: .rate16000, numberOfChannels: 1)` 和 `Stream.audioFramePublisher`。`audioStream` 是 Android 名称。这里没有 HFP、手机麦克风替代、额外音频会话或 App 波束成形；不能据此声称设备内部 DSP 已关闭。
- 同一 `DeviceSession` 的 Camera 进入 `.streaming` 后才 `addMotion(configuration:)`。默认请求 60 Hz，可选 30 Hz。只有 `.sensorUnavailable` 才按更低档 30 → 24 → 15 → 10 → 5 Hz 尝试；其他错误/超时单独记录。每档最多等待 12 秒，`.started` 与实际样本都到达才算准备成功。
- 视频 streaming 后 15 秒仍没有非空 PCM 时记录音频失败，视频继续。第一块零帧 PCM 记 `ignored_empty`，不误报失败，也不能凭空块认定音频启动成功。
- 关闭音频在开录前固定：不申请麦克风权限、`audioCodec:nil`、不订阅音频、不启动音频 watchdog。JSON 记录禁用及实际格式为空；空 WAV 不是录音。
- 设备状态来自 `Device.addDeviceStateListener`，只记录状态变化；SDK 未提供状态采集时间。Motion 字段可能为空。加速度单位 m/s²、角速度 rad/s、四元数 x/y/z/w；存在的磁场为 µT。

## 时间轴不能直接等同

| 数据 | SDK 原始时间 | 手机时间记录点 |
|---|---|---|
| 视频 | `CMSampleBuffer` PTS，完整 CMTime value/timescale/epoch/flags | 视频回调入口，排队前 |
| 音频 | `AudioFrame.presentationTimeStamp`，完整 CMTime | 音频回调入口，写盘前 |
| IMU | `MotionSample.timestampNs`，Int64 纳秒 | `Motion.samples` 交付给 App 时 |
| 状态 | 未暴露，留空 | 状态回调入口 |

所有手机单调时间统一由 `CMClockGetTime(CMClockGetHostTimeClock())` 读取，保留完整 CMTime 与秒数。墙钟只用于会话描述，不用于同步。UI 隐藏日历日期/时刻，但原始文件仍保留审计和同步时间；它们不是匿名数据。

公开说明及维护者答复指出音视频时间戳来自眼镜，流内音频可能存在编码延迟；未确认 IMU 与视频/音频共用零点或准确曝光时刻。**同一个 DeviceSession 不等于各传感器已经同步。** 不硬减固定偏移，不用到达手机的时间冒充采集时间。Motion AsyncStream 保留最新 256 个样本，交付时刻还包含 SDK 调度/缓冲影响。

多传感器 MOV 使用源视频 PTS 减首个成功写入帧的 PTS；普通录像保留原到达时间轴。无效、重复、倒退、更换 epoch 或超前墙钟超过 15 秒的源 PTS 拒收并写诊断；不偷偷换时钟。任何正常源时间轴或实际录制经过 300 秒即结束。拒收不删除已录视频，但可能造成录像不完整，必须看 CSV。

WAV 拼接实际收到的 PCM，不插静音，不按传输抖动伸缩音频。CSV 保留每块原始时间与样本位置。直接给 MOV 加 WAV 不能自动保证内容对齐：展示用合轨需先核对拍手事件，并保留使用的偏移/截取说明。

## 每次验收

1. 检查 `session.json` 的 `configuration`、`modules`、`errors`，确认请求配置与成功/失败/禁用匹配。
2. 检查 `frames.csv` 原始分辨率、拒收数、源/输出 PTS；抽查 MOV 实际帧时间，确认播放时长不是由批量回调压缩。
3. 音频先看非空块数、实际格式、采样总数与 WAV 时长。`timestampJitterCount` 与 `possibleSourceGapCount` 是不同诊断，两者都不等于确定丢音频数。
4. Motion 按原始 SDK 时间排序去重后，统计超过 2.5 个标称周期的间隔；另列原到达顺序的倒退、重复和主机交付停顿。CSV 保持原到达顺序，不插值。汇总时间列表最多 65,536 条，超出会注明；CSV 不因此裁剪。
5. 写盘队列最多 256 个待处理任务，检查队列峰值、排队时间、按模块丢弃计数；据此区分 App 排队与更上游问题，但不能单凭“无 App 排队”确定设备/传输根因。
6. 用可见拍手和舒适转头做相对同步检查。同步 JSON 标记表示提示开始/用户点击完成，**不是动作自动定位**。7 fps 单帧约 143 ms，不能据此宣称毫秒级同步。

60 Hz/30 Hz，以及 30 Hz 音频开/关是隔离对照；只改变一个选项。现阶段 IMU 曾出现长缺口，根因和连续采集能力未解决；麦克风开关不预先保证修复它。自有录像的完整端到端筛选与长期功耗也尚未验证。

## Developer Mode 与不可发布边界

具体安装步骤见 [README](README.md)。音频流和 Motion 使用实验能力，依赖设备、固件与授权。当前版本仅用于 Developer Mode 下的研究/内部设备测试，**不要把该组合当成已获准上架或可公开发布的功能**。生产发布需按对应版本的 Meta 能力可用性、审核和条款重新确认，不能仅把实验开关默认关闭就视为满足发布条件。

SDK 参考（对应核对依据，不代表已验证未来版本）：

- [iOS 1.0.1 更新日志](https://github.com/meta/meta-wearables-dat-ios/blob/1.0.1/CHANGELOG.md)
- [维护者关于音视频时间戳的答复](https://github.com/meta/meta-wearables-dat-ios/issues/347)
- [DAT 开发文档](https://wearables.developer.meta.com/docs/develop/)
