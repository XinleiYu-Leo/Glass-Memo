# Historical reviewed-v2 work

本目录保留早期的四帧分类器、人工/teacher 复核标签训练工具、标注盘点，以及后来十帧训练对照的准备代码。它们是研究沿革，不是当前 FHO 主模型。当前入口是 [../fho](../fho/README.md)。

已保存的 `historical_reviewed_v2.pt` 包含冻结的 MobileNetV3-Small 编码器和 4×576→128→1 分类头。经过单独权重包安装后，从仓库根目录运行：

```bash
python models/install_local.py --source ../local-models
python training/historical/infer_historical_v2.py --self-test
python training/historical/infer_historical_v2.py --images frame1.png frame2.png frame3.png frame4.png
```

预期四帧对应决策时刻之前 −1.5、−1.0、−0.5、0 秒。可用 `--timestamps t1 t2 t3 t4 --decision-time T` 校验时刻。依赖 Python 3.12、兼容的 torch/torchvision、NumPy、Pillow；`requirements.txt` 还含旧对照工具用的 PyAV。此离线推理不调用大模型 API；`--self-test` 只用合成图，不能证明真实准确率。

`train_reviewed_historical_v2.py` 使用已复核的标签文件训练，不会代替人给数据下结论。`historical_tenframe_comparison/package/tools` 包含选源、抽帧、标签模板和对照训练，附合成数据测试。原始人工答案、teacher 输入输出和真实图像不在本包中。检查旧脚本的 `--help` 后再使用，不要把历史协议或数据哈希用于当前实验。

测试（从本目录运行）：

```bash
python -m unittest discover -s annotation_inventory -v
cd historical_tenframe_comparison
python -m unittest discover -s source_tests -v
python -m unittest discover -s trainer_tests -v
```

`third_party/torchvision_LICENSE.txt` 保留预训练编码器的来源许可；这并不为数据集或全部项目代码自动赋予相同许可证。
