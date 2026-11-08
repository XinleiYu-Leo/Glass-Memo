# 多传感器检查与有声导出

输入是 App 导出的**完整会话目录**，含七个文件。此工具不会上传、转写或改动原始录像；建议将数据放在仓库外。共享仓库只有去标识汇总数字，没有真实会话。

## 检查音频、视频时间轴与 IMU

Python ≥3.10，基本 CSV/WAV 检查只用标准库。从仓库根目录：

```sh
python3 tools/multisensor/audit_session.py /path/to/private-session \
  --output /path/to/local-results/session_audit.json
```

若要确认 MOV 实际解码帧与 CSV 一致，创建独立 Python 环境、安装依赖，再加 `--decode-video`：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r tools/multisensor/requirements.txt
python tools/multisensor/audit_session.py /path/to/private-session \
  --decode-video --output /path/to/local-results/session_audit.json
```

Windows PowerShell 激活环境使用 `.venv\Scripts\Activate.ps1`；WSL 使用上面的 Linux/macOS 语法。路径占位符必须换成自己的路径。

输出只含配置、计数、间隔统计和结构检查，不复制会话 UUID、绝对输入路径、原始时间戳或设备错误文本。非零退出码表示结构检查失败或无法解析，先读结果再继续。`all_structural_checks_passed` 只代表文件/时间轴结构检查，**不代表无源缺口、无延迟或物理同步准确**。

Motion 源缺口定义：同 source 内，按 SDK 时间排序去重，间隔超过相邻样本较低请求频率的 2.5 个周期。缺口超额时间比例不是硬件丢包率。主机间隔按收到的顺序统计，晚到补回与源缺失分开报告。不合并不同 Motion source 时钟。

音频根据 CSV 中实际写入块与 WAV 样本偏移核对；空块不视为失败。音频 PTS 抖动和疑似源缺口分开统计，不能将每次毫秒抖动都称为丢块。

## 合成用于演示的有声视频

原 `video.mov` 只有视频，声音单独在 `audio.wav`。以下生成新 MP4，保留原文件：

```sh
python tools/multisensor/export_av.py /path/to/private-session \
  /path/to/local-results/presentation.mp4
```

视频 H.264 包直接复制，音频按写入块的 SDK PTS 与 WAV 样本偏移拟合映射后编码为 AAC；会附带 `.alignment.json`。有明显音频源间隙、时钟异常、格式不支持或已有输出时会停止，避免掩盖问题或覆盖原文件。

**导出会保留录下的所有谈话。** 去掉文件元数据不等于消除画面或语音里的隐私，演示或分享前自行播放检查。输出不属于可提交的合成示例。

这个映射不是物理采集延迟标定；仍应以拍手等可见/可听事件检查内容对齐。IMU 与媒体时钟未确认共用原点，本工具不做硬编码偏移或自动拼接 IMU。

## 测试

```sh
python3 -m unittest discover -s tools/multisensor -p 'test_*.py'
```

测试只使用临时合成数据；带视频的测试需要 PyAV/NumPy。最新汇总对照见 [CURRENT_STATUS.md](../../docs/CURRENT_STATUS.md)。
