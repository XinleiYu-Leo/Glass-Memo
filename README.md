# RoboMemo

本项目的共享仓库名为 [Glass-Memo](https://github.com/XinleiYu-Leo/Glass-Memo)，代码和 App 沿用 RoboMemo 名称。同组成员可直接 clone 本仓库，按下面的入口开始。

用眼镜记录物品线索，在需要时回答“东西放在哪里”。这是一个研究原型的协作仓库，包含 iPhone 采集 App、本地找物 demo、多传感器检查工具、训练代码和已保存模型的使用入口。

**第一次接手：先运行离线 demo，再按分工进入 iOS 或训练目录。当前尚未完成“小模型筛选 → 大模型 → 找物问答”的端到端评测，也未将训练模型部署到手机。**

English overview: [README_EN.md](README_EN.md). 当前基准：2026-11-06。

## 五分钟看懂项目

| 入口 | 做什么 | 当前状态 |
|---|---|---|
| [apps/demo](apps/demo/README.md) | 本地找物界面、事件记忆、视频证据、自动分析原型 | 随包的示例完全虚构；可离线运行 |
| [apps/ios](apps/ios/README.md) | 眼镜录像、多传感器采集、逐帧诊断 | SDK 1.0.1；需要自己的签名与真机配置 |
| [services/bridge](services/bridge/README.md) | 可选的本地模型连接服务 | 实验性账号适配器；需自己的账号与可用权限 |
| [tools/multisensor](tools/multisensor/README.md) | 检查视频时间轴、音频块、IMU 连续性；导出有声视频 | 本地处理，不上传原始素材 |
| [training/fho](training/fho/README.md) | Ego4D FHO 弱监督拿放识别、10 帧模型、消融与结果 | 当前训练主线；数据需自行获取授权 |
| [models](models/README.md) | 已保存权重清单、校验和安装 | 权重在单独的 `local-models` 包中 |
| [baselines/hand_object](baselines/hand_object/README.md) | 100DOH 手与接触物体基线 | 不是拿起/放下动作分类器 |
| [training/historical](training/historical/README.md) | 较早的人工复核、teacher 和训练工具 | 历史参考，不是当前主模型 |

## 先运行离线 demo

安装 Node.js 22 或更新版本，在仓库根目录运行：

```sh
cd apps/demo
node server.mjs
```

浏览器打开 **http://127.0.0.1:8787/**。随包只含几何图形构造的虚构示例，不需要密钥、登录或云端调用。真实视频自动分析是另外的可选流程，见该目录 README。连接服务不是标准 OpenAI API 通用客户端；不要把账号套餐等同于免费 API 额度。

## 看进度，再选工作

- [当前结果与未解决问题](docs/CURRENT_STATUS.md)：区分真实采集结果、弱标签模型结果和未完成事项。
- [系统与数据流](docs/ARCHITECTURE.md)：代码入口、文件格式、尚未接通的环节。
- [同组接手与下一步](docs/TEAM_HANDOFF.md)：按采集、模型、端到端三条线开始。
- [如何上传 GitHub](docs/GITHUB_UPLOAD.md)：上传展开后的源码目录，模型单独共享。
- [文件整理范围](docs/INVENTORY.md)：什么已收录、什么保留在本机、哪些文件不存在。
- [验证记录](VALIDATION.json)：此次整理实际运行的检查；不是全系统或真机稳定性证明。

## 当前可以怎样描述结果

已完成眼镜视频、流内 PCM 音频、IMU 和设备状态采集，并修复音频空块误报与多传感器视频时间轴。最新一次 30 Hz 关音频测试的 IMU 源时间缺口变小，但仍出现约 8 秒的延迟交付后集中补回；实时稳定性尚未解决。

训练主线使用 Ego4D 人工动作标注作弱标签，冻结 MobileNetV3-Small 后训练时序分类头。10 帧模型在当前检查集上的 AP 约 0.74–0.75；**AP 不是准确率**，结果不能直接代表自有眼镜数据或找物性能。详见当前状态和训练目录中的原始汇总指标。

## 运行环境

| 工作 | 环境 |
|---|---|
| 离线 demo / bridge 测试 | macOS、Windows 或 Linux；Node.js ≥22 |
| iOS 编译 / 设备安装 | macOS + Xcode；自己的 Apple 签名；眼镜 Developer Mode |
| 多传感器 CSV/WAV 检查 | Python ≥3.10；解码/有声导出额外安装 PyAV、NumPy |
| 已保存分类头推理 | Python + NumPy；需要自己的特征或合成自测 |
| 训练 / 特征提取 | 推荐 Windows WSL2/Linux + NVIDIA GPU；依赖见训练目录 |

每个模块单独配置依赖，没有一个命令能自动完成全部硬件、账号和数据集设置。

## 分享范围与授权

本仓库不含私人家中录像/录音、真实物品记忆、设备标识、账号状态、签名证书、密钥或 Ego4D 原始素材。示例媒体均为人工构造。个人数据、运行输出和模型本地目录有忽略规则；新增文件仍需检查。

本仓库是公开的；只包含经过检查的协作源码、说明和合成素材，不应提交真实录像、账号状态或模型附件。仓库含不同来源的代码，不提供覆盖全部内容的统一开源许可。Meta 示例/SDK 和手物检测代码保留各自条款，模型与数据也有相应使用条件。见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

协作方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。
