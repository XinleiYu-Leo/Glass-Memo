# RoboMemo 本地 bridge / Local analysis bridge

可选的本机服务，为 iPhone 和找物品 Demo 提供受控的图像分析接口。代码包括账号连接、图片/视频抽帧描述、物品观察，以及 teacher 标注诊断路由。**启动离线 Demo 不需要运行本服务。**

## 运行（默认仅本机）

需要 Node.js **22 或更新版本**，没有 npm 依赖。在仓库根目录运行：

```sh
cd services/bridge
node server.mjs
```

访问 <http://127.0.0.1:8765/>。媒体 API 默认是 <http://127.0.0.1:8766/>。首次启动创建自己的空 `.private-state/connection.json`，不会沿用原作者账号，也不会主动提交图片。状态为“未连接”是正常的。

此源码保留的是**实验性账号 OAuth/sign-in 适配器**，它请求的 issuer/resource、scope、模型列表和响应协议可在 `core.mjs`、`server.mjs`、`media-api.mjs` 中检查。它不是通用 API-key 客户端，也不提供任意供应商下拉切换。当前账号是否能用该流程、有哪些模型、额度/计费和服务条款，需要协作者自行核实；本次仅验证空状态启动与模拟请求，**没有替协作者验证真实登录或云端推理，也不承诺 ChatGPT 套餐包含免费 API**。

需要真实推理时，先用自己的受支持账号完成连接并选择图像模型，再从 iPhone 或 Demo 主动提交已获授权的画面。登录、刷新模型/令牌会连接外部服务；分析只在请求时发生。更换供应商需要实现并验证 [Demo 合约](../../apps/demo/BRIDGE_CONTRACT.txt)，不能只填一个 API key。

## 配置与私有状态

复制 `env.example` 为 `.env`，修改必要配置，然后运行：

```sh
node --env-file=.env server.mjs
```

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `ROBOMEMO_DASHBOARD_PORT` | `8765` | 仅本机账号面板 |
| `ROBOMEMO_CAPTION_PORT` | `8766` | 媒体 API |
| `ROBOMEMO_STATE_DIR` | 本模块 `.private-state/` | 自己的连接、令牌和配对信息 |
| `ROBOMEMO_ALLOW_LAN` | 关闭 | 只有值为 `1` 才允许手机经局域网连接媒体 API |

`.env` 不会自动加载；使用上述参数或设置进程环境变量。账号信息只在本机私有目录持久化，代码设置目录/文件权限；Windows 下还需使用正常受保护的用户目录。状态目录、环境文件、日志由 `.gitignore` 排除，**不要上传它们或手工压缩整个运行目录**。若迁出私有状态，使用自己选定的目录；本仓库不带任何可共享账号凭据。

## 连接离线 Demo

在 `apps/demo/.env` 中设置：

```dotenv
ROBOMEMO_ENABLE_AUTO=1
ROBOMEMO_BRIDGE_URL=http://127.0.0.1:8766
ROBOMEMO_BRIDGE_CONNECTION_FILE=../../services/bridge/.private-state/connection.json
```

从 `apps/demo` 运行 `node --env-file=.env server.mjs`。仅配对码会被客户端读取用于本机认证；原生抽帧工具需先在 macOS 编译，见 [Demo README](../../apps/demo/README.md)。连接完成不等于自动上传，仍需在页面主动发起分析。个人素材和受限数据集不随源码分享。

## iPhone 局域网测试

若需要手机连接，在 `.env` 明确设置 `ROBOMEMO_ALLOW_LAN=1`。只有媒体 API 改为监听所有接口，账号面板仍限 `127.0.0.1`。在 App 的连接设置中填**此电脑在可信 Wi-Fi 上可达的地址**和本次生成的配对码。自动发现使用 macOS 的本地主机名；Windows/Linux 或名称解析失败时，请自己填写局域网 IP 与端口，不要使用手机自己的 `127.0.0.1`。防火墙也需允许该端口。

局域网链路是开发用 HTTP + 配对码；不要暴露到公网。电脑到 provider 的请求使用 HTTPS。该说明描述当前原型，不代表完成了生产部署、跨设备配对或完整安全审计。

## 模块和测试

| 文件 | 用途 |
| --- | --- |
| `server.mjs` | 本机面板、OAuth、私有状态、媒体 API 生命周期 |
| `core.mjs` | 认证与输入工具、响应解析、错误控制 |
| `media-api.mjs` | 受认证的图片/视频抽帧、物品和 teacher API |
| `object-memory.mjs` | 物品观察提示词/schema |
| `teacher-*.mjs` | teacher 动作/存在性/输入诊断提示与校验 |
| `index.html`, `dashboard.js`, `style.css` | 连接面板 |

```sh
node --test
```

本次在 macOS / Node 24.22.1 运行，102 项测试全部通过。测试使用临时状态、模拟响应和内存生成的测试签名密钥，不登录、不上传真实图片。覆盖配对认证、来源检查、大小/schema、流解析、取消、teacher 约束及空状态启动。`server.mjs` 是可执行入口，import 它会启动服务；单测使用模块 API，启动测试使用独立进程。

## English quick reference

Optional local service; the offline demo works without it. Node >=22, no npm dependencies. Start with `node server.mjs` and open <http://127.0.0.1:8765/>. Media API listens on localhost:8766. A fresh launch creates empty private state and performs no image inference. Explicitly configure your **own** supported account or implement a compatible adapter. This snapshot does not guarantee provider eligibility, model availability or included/free API usage.

Private state, tokens, pairing codes and real media are excluded from this repository. LAN exposure is opt-in (`ROBOMEMO_ALLOW_LAN=1`); the dashboard remains loopback-only. Tests (`node --test`) use synthetic/mocked input. The student selector is not part of this bridge.

## Source provenance

Based on the available local bridge implementation, with sanitized defaults and documentation. Source comparison during packaging found no newer live inference logic to merge. No account state, original credentials, developer launchers, raw model outputs or personal media were copied. Repository-level attribution and sharing terms apply.
