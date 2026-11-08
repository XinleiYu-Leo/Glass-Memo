# RoboMemo iOS：录制、传感器诊断与眼镜显示

这是当前 App 的可协作源码副本，基于 Meta 的 DisplayAccess 示例。包含普通视频录制、逐帧诊断、多传感器录制（实验）、照片/视频分析入口及眼镜显示。**采集在本机进行；多传感器默认关闭。** 小模型尚未在这个 iOS App 内部署，不能把本 App 当成已经完成端到端筛选的版本。

## 开发环境与首次运行

需要 Mac、完整 Xcode、iOS 17.2+ 的 iPhone、受支持且已在 Meta AI 配对的眼镜。此次交付在 Xcode 26.6 / Apple Silicon 上验证；上游示例要求 Xcode 26.4+、Swift 6.3+。Windows 可编辑源码，不能直接构建 iOS App。

1. 在本目录执行 `cp Config.example.xcconfig Config.local.xcconfig`。填写你自己的 `DEVELOPMENT_TEAM`，为 `PRODUCT_BUNDLE_IDENTIFIER` 和 `ROBOMEMO_CALLBACK_SCHEME` 选择自己的唯一值。例子使用 `org.example.robomemo`；它不是可共用的签名身份。`Config.local.xcconfig` 已加入忽略规则，不提交自己的配置。
2. 用 Xcode 打开 `DisplayAccess.xcodeproj`，等待 Swift Package 解析完成。依赖锁定 **Meta Wearables DAT 1.0.1**，包括 Core、Camera、Motion、Display、MockDevice；不用手动复制 SDK 二进制。首次取依赖需要联网。
3. 在 Xcode 登录自己的 Apple 开发者账户，选择 `DisplayAccess` scheme 和连接的 iPhone。签名使用自己的 Team；如果 Xcode 提示 provisioning，按自己的账户创建。交付包不包含证书、描述文件或原作者的签名配置。
4. iPhone 如有要求，开启「设置 → 隐私与安全性 → 开发者模式」并重启。Meta AI 中也要开启眼镜的 **Settings → Your glasses → Developer Mode**；这是两个不同的设置。
5. Developer Mode 下保留 `META_APP_ID = 0`、`CLIENT_TOKEN` 为空。`Info.plist` 的 URL scheme 和 `MWDAT.AppLinkURLScheme` 都从 `ROBOMEMO_CALLBACK_SCHEME` 生成，修改时保持一致。使用 Meta 配置的测试发布渠道时，换成该应用自己的 ID/token，并核对其能力授权。
6. `Cmd+R` 安装运行。首次在 App 点 **Register** 完成 Meta AI 注册，允许蓝牙/摄像头；打开多传感器且开启录音时才申请眼镜麦克风权限。如 iOS 提示开发者未受信任，在「设置 → 通用 → VPN 与设备管理」完成自己的开发者验证。

仅检查编译、不安装手机（在本目录运行；确保 `xcode-select -p` 指向完整 Xcode，或设置 `DEVELOPER_DIR`）：

```sh
xcodebuild -project DisplayAccess.xcodeproj -scheme DisplayAccess \
  -configuration Debug -destination 'generic/platform=iOS' \
  -derivedDataPath .build/DerivedData CODE_SIGNING_ALLOWED=NO build
```

安装签名失效时用同一 bundle ID/Team 重新 Build & Run；不要为修复签名先删除 App，否则本机录像可能被一并删除。

## 怎么录

进入 **RoboMemo → Glasses video**。保持 App 在前台、眼镜佩戴正确、手机靠近眼镜、光线足够。录制最多 5 分钟，也可以提前 **Stop and save**。预览按钮只预览，不等于开始录制。

| 模式 | 配置与输出 |
|---|---|
| 普通录像（默认） | 可选 High@15 / High@7 / Medium@15，输出视频、JSON、逐帧 CSV。画质档位是请求值，实际原始尺寸必须看 CSV。 |
| 多传感器（实验） | 打开开关；视频固定 High@7；IMU 可选 60 Hz（默认）/30 Hz；录制音频默认开启，可独立关闭。 |
| 同步测试 | 打开后等待准备提示；开音频时双手入镜拍手 3 次、每次间隔约 2 秒，再舒适地左右转头 3 轮；关闭音频时只提示转头。做完点「同步测试完成」。 |

建议初次验证：先录普通录像 10 秒；再录多传感器 75–90 秒，静止 5 秒 → 同步动作 → 点完成 → 拿放物品约 20 秒 → 静止 10 秒 → 保存。要比较 60/30 Hz 或音频开/关，每次只改一个选项并保持环境一致。请求 30/60 Hz 不保证样本实际连续，原始 CSV 才是判断依据。

多传感器每次生成独立目录：

```text
Documents/RoboMemoVideos/RoboMemo-Multi-UUID/
  video.mov          # 仅视频轨；直接播放没有声音是预期
  frames.csv         # 原始帧尺寸、源 PTS、到达时间、输出 PTS、拒收诊断
  audio.wav          # 流内 PCM，非 HFP；不与 MOV 自动合轨
  audio_chunks.csv   # 音频原始 PTS、到达时间、写入位置、空块/错误
  motion.csv         # SDK 时间、到达时间、加速度/角速度及可空姿态
  device_state.csv   # 电量、充电、佩戴、镜腿、温度等状态变化
  session.json       # 配置、模块结果、时间标记、实际计数和错误摘要
```

在 iPhone「文件 → 我的 iPhone → Display Access」导出完整会话目录，或通过 Finder 文件共享取得 `RoboMemoVideos`。**只发 MOV 会丢失音频和诊断。** 关闭录音时 WAV 是合法 44 字节空文件头，CSV 只有表头，JSON 写 `disabled/user_disabled`；这是主动关闭，不是录音失败。音频或 IMU 失败时继续保存视频，并记录失败原因。

## 分析与显示

本地录制不需要 Mac 服务。使用照片/视频分析功能时，按仓库中相应服务的 README 启动后端；在 App 的 **Mac connection** 填该电脑的局域网地址与本次配对码，再点 **Test connection**。示例 `your-mac.local` 必须替换成实际主机。iPhone 上的 `127.0.0.1` 指向 iPhone 自己。服务地址会保存在手机，配对码只保留在内存。

选择照片/录像后显式点分析才会发到服务；先确认该服务采用的模型、账户和数据权限。摄像头与眼镜显示会话互斥，先停止录制/预览再 **Show on glasses**。Phone preview/MockDevice 可辅助 UI 开发，不能验证真实传感器。

## 代码入口与验证

| 文件 | 职责 |
|---|---|
| `RoboMemoGlassesCameraViewModel.swift` | 摄像头生命周期、模式/画质、授权、开始/停止与同步 UI |
| `RoboMemoVideoRecorder.swift` | 编码 MOV、逐帧 CSV、原始尺寸与源/输出时间诊断 |
| `RoboMemoSensorRecorder.swift` | 单调主机时钟、WAV/CSV/JSON、有界后台写盘与诊断 |
| `RoboMemoMultiSensorController.swift` | 同一 DeviceSession 的音频、Motion、设备状态、失败隔离与降档 |
| `RoboMemoAIView.swift` / `RoboMemoVideosView.swift` | 录制界面、保存/导出、分析入口 |
| `tests/` | 实际录制器/控制器源码的 macOS 回归测试；SDK 用测试替身 |

实现入口位于 `DisplayAccess/ViewModels` 和 `DisplayAccess/Views`。测试运行见 [tests/README.md](tests/README.md)，时间与已知限制见 [MULTISENSOR_TESTING.md](MULTISENSOR_TESTING.md)。测试替身不能证明眼镜传输或时间同步正确。

## 分发内容与许可

本目录不包含用户录像、录音、设备日志、证书、私人账号配置、模型权重、构建产物或 SDK 二进制。运行后生成的私人数据不能直接提交到 GitHub。

上游来源为 Meta Wearables DAT 的 DisplayAccess 示例；原有版权头保留，原仓库的 [LICENSE](LICENSE) 与 [NOTICE](NOTICE) 随本目录提供。LICENSE 指向 Meta Developer Terms，本目录**没有另行将这些上游代码/SDK 声明为 MIT 或 Apache 许可**。App 图标和汽车示例图片随上游示例保留。新增/修改部分的再分发许可由项目维护者明确后再添加。
