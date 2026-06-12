# fsync —— 本地子目录 ↔ 飞书同名文件夹（纯名字驱动）

[![GitHub](https://img.shields.io/badge/GitHub-ClausLiang%2Ffsync-181717?logo=github)](https://github.com/ClausLiang/fsync)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

通用同步工具。**你只需要敲文件夹名字，全程不碰 token。** 工具内部按名字在你的飞书账号里找到（或创建）同名文件夹，token 自动解析。因为解析的是同一个飞书账号，所以**任意电脑上同名 = 同一个云端文件夹**——换电脑什么都不用带，名字一样就接得上。

脚本不含任何密钥，可安全提交到 GitHub。

提供两个等价实现，行为一致（基目录「文档同步」、纯名字、smart 增量、`--force` 覆盖、递归子目录、在线文档不碰），按喜好选一个：

| | `fsync.js`（Node 版） | `fsync.py`（Python 版） |
|---|---|---|
| 底层 | 调 lark-cli 的 `drive +push/+pull` | 直连飞书 Drive API，**不依赖 lark-cli** |
| 依赖 | Node.js + lark-cli | 仅 Python 3（零第三方库） |
| 登录/凭据 | 由 lark-cli 管 | 自己管：独立 OAuth，存 `~/.fsync/`，过期自动续 |

## 布局（脚本与目标目录同级）

**整个工具就是一个脚本文件**（`fsync.js` 或 `fsync.py`），单文件、零安装：用的时候**把这一个文件复制到本地、放在你的文档目录旁边即可**，不用建项目、不用 `npm install` / `pip install`、不产生别的文件。

```
工具目录/
├── fsync.py          ← 拷过来的这一个脚本文件
├── 我的工作总结/      ← 目标目录之一
│   ├── a.md
│   └── b.md
└── 读书笔记/          ← 另一个
```

> 注：脚本本身拷一个文件就行，但每台机器仍需具备**运行环境**（Python 版要 Python 3 / Node 版要 Node+lark-cli）并**首次登录**一次（见各版本说明）。登录凭据存在脚本之外，不随脚本走。

---

## A. Node 版（`fsync.js`）

### 一次性 setup（每台机器一次）

```bash
npx @larksuite/cli@latest install
```

新版 `install` 会自动建应用、写好配置、并完成默认登录，**一条就够**，无需再跑 `config init` 或 `auth login`。

> 万一某次 push/pull 提示缺权限，本工具会自动弹浏览器让你点「同意」补授权，无需手动命令。

### 使用

```bash
node fsync.js push 我的工作总结 --dry-run   # 先空跑看步骤
node fsync.js push 我的工作总结             # 本地 → 飞书（文件夹不存在会自动创建）
node fsync.js pull 我的工作总结             # 飞书 → 本地
node fsync.js push 我的工作总结 --force     # 强制覆盖（--if-exists=overwrite）
node fsync.js ls                           # 列出飞书「文档同步」下的文件夹（排查用）
```

换台电脑：装好 lark-cli 并登录同一个飞书账号，把脚本和目标目录拷过去，`node fsync.js pull 我的工作总结` 即可。

---

## B. Python 版（`fsync.py`，直连 API，不依赖 lark-cli）

### 安装 Python（唯一依赖，零第三方库）

脚本只用 Python 3 标准库，**装好 Python 3.6+ 即可，不用 `pip install` 任何东西**。先验证：

```bash
python3 --version
```

能打印出 `Python 3.x.x` 就说明已具备，直接跳到下一步。否则按系统装一个：

- **Windows**：到 [python.org/downloads](https://www.python.org/downloads/) 下载安装，**勾选「Add Python to PATH」**；装好后命令用 `python`（或 `py`）代替下文的 `python3`。
- **macOS**：`brew install python3`（或同样去 python.org 下载）。系统自带的一般也够。
- **Linux**：`sudo apt install python3`（Debian/Ubuntu）或 `sudo yum install python3`（CentOS/RHEL）。

### 一次性后台准备（懂行的人做一次）

> Python 版不依赖 lark-cli，需要你**自己在飞书开放平台建一个自建应用**并开通权限（Node 版的 `install` 会自动建应用，省了这步）。

到 [open.feishu.cn](https://open.feishu.cn) → 开发者后台 → 创建/选择一个**自建应用**，然后：

1. **权限管理**：开通 `drive:drive`（云空间读写）和 `offline_access`（令牌自动续期），并**发布**应用。
   - ⚠️ **关键：要开的是「用户身份」权限，不是「应用身份」权限。** 本工具走的是「你本人登录授权」（user_access_token），文档是以**你的身份**存进**你的「我的空间」**；如果只开了「应用身份」（tenant token），用的就不是你的身份、也进不了你的个人空间，会授权失败。权限管理页每个权限通常能分别勾「应用身份 / 用户身份」，**务必勾上用户身份**。
2. **安全设置 → 重定向 URL**：添加 `http://localhost:17777/callback`（端口可用环境变量 `FSYNC_PORT` 改，重定向 URL 要同步改）；
3. **凭证与基础信息**：记下 App ID / App Secret，下一步要填。

### 每台机器一次

```bash
python3 fsync.py setup     # 录入 App ID/Secret → 自动开浏览器登录，点「同意」即可
```

凭据与登录态都存在 `~/.fsync/`（仓库外，永不进 git）；令牌过期会用 refresh_token 自动续，无感。

### 使用

```bash
python3 fsync.py push 我的工作总结 --dry-run   # 先空跑看每个文件的动作
python3 fsync.py push 我的工作总结             # 本地 → 飞书（不存在自动建）
python3 fsync.py pull 我的工作总结             # 飞书 → 本地
python3 fsync.py push 我的工作总结 --force     # 无条件覆盖
python3 fsync.py ls                           # 列出「文档同步」下的文件夹
python3 fsync.py login                         # 重新登录（换账号）
python3 fsync.py logout                        # 清除本地登录缓存
```

换台电脑：拷脚本和目标目录过去，`python3 fsync.py setup` 登录同一账号，然后 `python3 fsync.py pull 我的工作总结`。

> 注意：Python 版 `upload_all` 单文件上限 20MB，超过的会跳过并提示（大文件分片上传暂未实现）。

## 云端结构

所有同步文件夹统一放在「我的空间」下的一个**基目录**里（默认名「文档同步」，见脚本顶部 `BASE_FOLDER`，留空 `''` 则直接用根目录），保持根目录清爽：

```
我的空间/
└── 文档同步/            ← 基目录，工具自动建一次
    ├── 我的工作总结/
    └── 读书笔记/
```

## 工作原理

两版逻辑相同，下面以 Node 版命令示意（Python 版调对应的飞书 API，效果一样）。`push 我的工作总结` 时：

1. `drive files list` 列根目录 → 找/建基目录「文档同步」，拿它的 token；
2. 列基目录 → 找名为「我的工作总结」且 `type=folder` 的项；找不到则 `create_folder` 在基目录下建一个；
3. `drive +push --folder-token <token> --local-dir .` 把本地目录镜像上去。

`pull` 同理，但找不到时报错（不会凭空建本地内容）。

## 覆盖策略

两版默认都用 **smart**：按修改时间增量同步，并保护较新的一方，避免冲掉未同步的改动。加 `--force` 则当次**无条件覆盖**。（Node 版底层对应 lark-cli 的 `--if-exists`，可在 `fsync.js` 顶部常量微调。）

## 给非 IT 用户分发时

- **运行时授权**：工具会自动处理。遇到缺权限/未登录，它不会甩英文 JSON，而是提示「需要飞书授权，即将打开浏览器，请点同意」并自动发起授权，用户点一下「同意」即可继续。
- **一次性后台配置（需要懂行的人做一次）**：统一建一个飞书自建应用、配齐权限，再把 app_id/secret 发给同事，同事不用碰开放平台——Node 版让同事 `lark-cli config init` 填入即可，Python 版让同事 `python3 fsync.py setup` 填入即可，之后都是点浏览器「同意」。

## 排查

### HTTPS 证书校验失败（`CERTIFICATE_VERIFY_FAILED` / `self-signed certificate in certificate chain`）

不是网络不通，而是当前网络有 **TLS 拦截代理**（公司防火墙、安全软件、杀软的 HTTPS 检查）在中间换上了自签名根证书，Python 默认不信任它，于是校验失败。Python 版（`fsync.py`）提供两个出口：

- **方案一（推荐，仍校验）**：拿到公司根证书 `.pem`，用环境变量指给 fsync——

  ```bash
  export FSYNC_CA_BUNDLE=/路径/corp-ca.pem
  python3 fsync.py ls
  ```

  根证书通常在系统里：Linux 一般是 `/etc/ssl/certs/ca-certificates.crt`（已含公司证书时直接指它即可）；macOS 可从「钥匙串访问」把企业根证书导出为 `.pem`。也兼容标准的 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`。

- **方案二（临时，跳过校验）**：信任当前网络、只想先跑通时——

  ```bash
  export FSYNC_INSECURE=1
  python3 fsync.py ls
  ```

  这会关闭证书校验，失去中间人防护，仅建议临时排查用。

## 注意

- **文件级镜像**：同步目录里**所有文件**（不只 `.md`，也含图片等附件；docx 等在线文档不碰）。
- **重名文件夹**：若同一层级下有多个同名文件夹，取最近修改的那个并告警，建议去飞书清理。
- **Windows**：两版都可用，无需额外设置；命令里把 `python3` 换成 `python`（Python 版）、或正常用 `node`（Node 版）即可，中文路径/参数脚本已处理好。
- 删除不自动同步：本地删了云端不会删（更安全）。
