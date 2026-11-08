# 发给同组：上传方法

## 现有共享仓库

本次交付已选用 [XinleiYu-Leo/Glass-Memo](https://github.com/XinleiYu-Leo/Glass-Memo)。它是公开仓库，以下命令可直接取得源码：

```sh
git clone https://github.com/XinleiYu-Leo/Glass-Memo.git
cd Glass-Memo
```

然后按根目录 README 运行。协作时新建分支、提交改动并发 Pull Request；不要在已有仓库中再次执行下面的 `git init` 或 `git remote add`。模型仍通过单独附件交接，按 `models/README.md` 安装到忽略目录。

## 创建独立副本时

以下保留最初源码包的通用上传说明，适用于另建仓库。

建议先创建 **Private** GitHub 仓库并邀请同组成员。上传的是解压后的 `robomemo` 目录内容；不要把所有原工作文件夹、录像或模型 ZIP 一起拖进源码仓库。

## 用 GitHub Desktop

1. 解压源码包，得到 `robomemo` 文件夹。
2. 在 GitHub Desktop 选择添加本地仓库，指向该文件夹；若提示还不是 Git 仓库，就在该目录创建。
3. 确认变更只有这里的代码/说明/虚构样例，提交一次初始快照。
4. Publish repository 时保持私有，再从 GitHub 仓库设置邀请成员。

也可以使用下面的命令行方式；无需同时操作两套。

## 用命令行

先在 GitHub 创建空的私有仓库，不要额外勾选 README、license 或 gitignore。本包已有这些配置/说明。进入解压后的 `robomemo` 目录，先检查：

```sh
python3 tools/repository_audit.py
git init -b main
git add .
git diff --cached --stat
git status --short
git commit -m "Add sanitized RoboMemo collaboration snapshot"
```

Windows 若用原生 Python，可将 `python3` 换成 `python`。确认 Git 提交身份是你希望同组看到的名称/邮箱；需要时用 GitHub 提供的 noreply 地址。随后把下列示例 URL 换成自己的真实仓库：

```sh
git remote add origin https://github.com/YOUR-ACCOUNT/robomemo.git
git push -u origin main
```

如果 Git 要求登录，使用你自己的 GitHub 登录流程。包内没有任何账号凭据。

官方流程参考：[GitHub 导入本地代码](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)。

## 模型怎样给同组

`RoboMemo_local_models.zip` 是独立附件。先确认适用的数据和模型条款允许向该成员提供，再通过访问受控的团队存储共享；不要放进 `robomemo` 目录再整体上传。成员解压得到 `local-models`，按 [模型 README](../models/README.md) 校验并安装到被 Git 忽略的 `models/local/`。访问控制本身不替代使用或分发授权。

源码本身不需要 Git LFS。GitHub 普通 Git 文件上限为 100 MiB，网页上传每文件上限 25 MiB；需要公开较大模型时再单独考虑 LFS/Release/模型托管及对应许可。[GitHub 文件上传限制](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)

## 以后更新

只提交代码、配置模板、文档和可分享的汇总结果。`.gitignore` 无法清除已提交的秘密，也不能自动判断一张新图片是否私人；每次提交前看 `git diff --cached`。不要把录制目录移入 synthetic 样例目录。
