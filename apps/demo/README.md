# RoboMemo 找物品 Demo / Object-memory demo

这是一个可在本机运行的找物品原型：搜索物品名称，显示最近一次清楚看到它的画面、时间线和对应视频。仓库自带的图片、视频、位置描述全部是 **虚构的几何图形样例（synthetic fixtures）**，用于演示界面和验证代码，不能作为模型效果或真实实验结果。

## 先运行离线样例

安装 Node.js **22 或更新版本**。没有 npm 依赖，不需要 `npm install`、账号或模型权重。在仓库根目录运行：

```sh
cd apps/demo
node server.mjs
```

浏览器打开 <http://127.0.0.1:8787/>，输入“钥匙”/`keys` 或“钱包”/`wallet`。点击证据图回放虚构视频。按 `Ctrl+C` 停止。Windows PowerShell、macOS 和 Linux 都可运行这个离线 Node 界面；本次打包在 macOS / Node 24.22.1 验证。

离线查询是别名匹配与证据时间排序，不调用大模型。自动分析默认关闭。钥匙示例展示“较早放在 A → 后来拿在手上 → 去向不明”；系统不把较早位置当作当前位置，也不猜画面外的新落点。

端口被占用时可复制 `env.example` 为 `.env`，修改 `ROBOMEMO_DEMO_PORT`，再运行：

```sh
node --env-file=.env server.mjs
```

`.env` 不是自动加载的；上面的参数或当前进程环境变量才会生效。

## 可选：自己配置自动分析

自动管线的代码已经包含在此目录：导入短视频 → 粗抽帧 → 本地 bridge 请求视觉模型 → 按需要细抽帧 → 校验证据 → 写入独立自动索引。**训练出来的小模型尚未接入这条管线**，目前不是“小模型筛选后再交给大模型”的端到端实现。

1. 先按 [bridge README](../../services/bridge/README.md) 启动并配置协作者自己的连接。仓库没有原用户账号状态。
2. 在 **macOS + Xcode/Swift** 上编译抽帧和封装工具：

   ```sh
   bash tools/build-auto-extract.sh
   bash tools/build-auto-remux.sh
   ```

3. 复制 `env.example` 为 `.env`，把 `ROBOMEMO_ENABLE_AUTO` 设为 `1`，并配置自己的 `ROBOMEMO_BRIDGE_CONNECTION_FILE` 或运行时配对码；再用 `node --env-file=.env server.mjs` 启动。
4. 在页面“自动分析录像”中明确点击开始。导入只接受 MOV/MP4，最长 5 分钟、最大 1 GiB；现有封装器要求 H.264，不提供通用 HEVC 转码。仅分析你有权发送给该服务的输入。Ego4D 等受限数据须另行遵守其授权，不要因代码可运行就把数据上传。
5. 自动结果显示在“自动识别”索引；它们是需要复核的候选线索，不能直接称为人工确认的放置事件。

Windows/Linux 的离线界面可用，但本仓库的原视频抽帧器使用 Apple AVFoundation，不能原样在这些系统编译。跨平台抽帧器尚未提供，需要实现相同接口并测试。原有分段诊断 CSV 并非自动分析的直接输入。

`ROBOMEMO_BRIDGE_URL` 只能指向 `http://127.0.0.1:<port>`。bridge 合约见 [BRIDGE_CONTRACT.txt](BRIDGE_CONTRACT.txt)。当前对象 schema 限定为钥匙、钱包、耳机盒；自带离线索引仅包含前两项。新增物品需同时改对象目录、提示词和 schema。API key 本身不能代替这个 bridge 合约，当前代码没有通用 API-key provider 插件。

## 文件与本地数据

| 路径 | 用途 |
| --- | --- |
| `core.mjs` | 索引校验、别名查找、最近清楚证据和不确定性 |
| `server.mjs` | 本机 HTTP、有限文件路由、范围回放、受控导入 |
| `auto-pipeline.mjs` | 抽帧、bridge 调用、缓存、取消、次数限制、原子发布 |
| `refinement-windows.mjs` | 细查窗口选择和去重 |
| `web/` | 中文界面 |
| `tools/AutoExtract.swift`, `tools/AutoRemux.swift` | 原生抽帧与封装源码 |
| `tools/build_index.py` | 从自有复核材料构建索引的辅助脚本 |
| `tools/make_synthetic_fixture.py` | 重新生成虚构样例；可选，需要 Python、Pillow 和 PyAV |
| `data/index.json`, `data/synthetic/` | 唯一随仓库提交的样例数据 |

新录像、自动帧、模型响应和缓存可能包含隐私，保存在 `.auto-work/`、`data/auto/`、`data/auto-index.json` 等目录，由 `.gitignore` 排除。不要将整个运行目录直接压缩分享。Git 忽略规则不会自动检查手工 ZIP，也不会阻止 `git add -f`。

服务只监听本机，检查 Host/Origin，媒体只由索引白名单提供；没有任意文件浏览接口。上传文件名、大小和目录均校验，导入副本在完成/取消后清理；缓存与结果不会因此一并删除。

## 检查代码

```sh
node --test tests/*.test.mjs
```

本次 55 项测试全部通过，两个 Swift 工具编译通过，并成功对虚构视频做抽帧（11 帧）与无重编码封装检查。预编译产物不随仓库提交。

测试只使用虚构数据、临时文件和模拟 bridge。包括离线启动、搜索、视频分段回放、拒绝跨站请求/无效上传/越界文件、缓存和任务取消，不发起云端推理。Windows PowerShell 也可使用 `node --test`，它自动发现 `tests/` 下的测试。

## English quick reference

From the repository root, run `cd apps/demo`, then `node server.mjs` and open <http://127.0.0.1:8787/>. Requires Node >=22, no npm dependencies. Everything bundled is **fictional synthetic material**, not personal footage, model predictions or research evidence. Search `keys` or `wallet`; evidence retrieval and playback stay offline by default.

Optional automatic analysis requires your own explicitly configured bridge and the macOS native extraction tools. See [bridge setup](../../services/bridge/README.md) and `env.example`; `.env` must be loaded with `node --env-file=.env server.mjs`. The learned student selector is not integrated. Account eligibility, quota and billing are not supplied by this repository.

## Source provenance

This preserves the available demo implementation with the previously prepared sanitized fixture/configuration adaptations. Source comparison during packaging found no newer live demo logic to merge. No original Git history, household data, prebuilt tools or account files are included. Repository-level attribution and sharing terms apply; bundling this module does not assign licenses to third-party software or datasets.
