# iOS 录制层回归测试（在 macOS 执行）

这两个 runner 编译本目录旁的实际 App 源码，在 macOS AVFoundation 上生成临时 MOV/WAV；SDK 类型与异步行为用本目录的测试替身。它们检查录制、输出时间轴、失败隔离、队列和音频开关，不连接眼镜，不验证硬件采样、传输或物理同步。真实 iOS SDK API 链接另由 App 的 Xcode 构建验证。

需要 Python 3、完整 Xcode。默认从 `DEVELOPER_DIR` 或 `xcode-select -p` 找工具链；自动寻找 SDK，不固定电脑路径或 SDK 目录名。Intel/Apple Silicon 使用各自主机架构，交付验证实际在 Apple Silicon 上完成。

从 `apps/ios` 执行：

```sh
python3 tests/run_validation.py
python3 tests/run_controller_validation.py
```

可用 `--xcode /Applications/Xcode.app` 选择另一个 Xcode，`--app /path/to/ios` 指定待测 App，`--output /path/to/new-results` 指定一个尚不存在的输出目录。默认生成到忽略的 `tests/.artifacts/`，每次独立子目录，测试编译产物也留在该目录，不污染源码。

通过时输出 `passed: true`、断言数和被测源码 SHA-256，并写 `validation.json` / `checks.log`。录制器套件预期 195 项，控制器套件 37 项，共 232 项；新增行为后以实际报告和断言含义为准。`DisplayAccess/__tests__` 是保留的其他示例测试源码，当前上述 runner 不执行那些文件。

测试生成的媒体是合成测试信号，不是用户录像。不要提交 `.artifacts`、SDK 二进制或编译缓存。交付验证见 [VALIDATION.md](../VALIDATION.md)。
