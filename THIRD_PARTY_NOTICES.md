# 来源、许可和数据边界

本快照服务于项目协作，没有为所有文件附加统一 MIT/Apache 或其他开源授权。原项目新增代码的对外许可尚未确定；公开发布前需要项目方决定。下载/使用某个组件也不等于取得重新分发全部相关数据与模型的权限。

| 部分 | 适用说明 |
|---|---|
| Meta iOS 示例与 SDK | [LICENSE](apps/ios/LICENSE)、[NOTICE](apps/ios/NOTICE) 及其文档；SDK 由 Swift Package Manager 获取，保留 Meta Developer Terms。实验性音频/Motion 仅用于开发验证，发布条件见 iOS 文档。 |
| 100DOH / hand-object detector | 保留 [上游 LICENSE](baselines/hand_object/vendor/hand_object_detector/LICENSE) 和 [来源记录](baselines/hand_object/provenance.json)。检测器权重不随源码包分发。 |
| Ego4D 训练 | 成员自行取得有效授权；本包不含原媒体、完整标注或特征。自训模型仍需结合适用的数据与上游模型条件使用。 |
| PyTorch、torchvision、NumPy、PyAV、FFmpeg 等 | 按各模块依赖安装，遵守各上游条款；本包不捆绑其运行环境。历史编码器保留 [torchvision LICENSE](training/historical/third_party/torchvision_LICENSE.txt)。 |
| Demo / bridge | 项目原型；包含的账号适配方式是实验实现，不保证任何服务当前支持它。不得分享账户登录态。 |

训练代码与模型具体来源见 [CODE_THIRD_PARTY_NOTICES.md](training/fho/CODE_THIRD_PARTY_NOTICES.md) 以及各模型配置。部分历史文件包含公开上游作者姓名或许可联系方式，这是法律归属信息，并非项目使用者的个人账户。

合成 demo 图片和视频仅用于软件功能验证，不是实拍结果或模型性能证据。
