# fsync —— 本地子目录 ↔ 飞书同名文件夹（纯名字驱动）

通用同步工具，底层用 lark-cli 原生的 `drive +push` / `drive +pull`（目录↔文件夹的文件级镜像）。

**你只需要敲文件夹名字，全程不碰 token。** 工具内部按名字在你的飞书账号里找到（或创建）同名文件夹，token 自动解析。因为解析的是同一个飞书账号，所以**任意电脑上同名 = 同一个云端文件夹**——换电脑什么都不用带，名字一样就接得上。

脚本不含任何密钥，可安全提交到 GitHub。

## 布局（脚本与目标目录同级）

```
工具目录/
├── fsync.js
├── 我的工作总结/      ← 目标目录之一
│   ├── a.md
│   └── b.md
└── 读书笔记/          ← 另一个
```

## 一次性 setup（每台机器一次）

```bash
npx @larksuite/cli@latest install
```

新版 `install` 会自动建应用、写好配置、并完成默认登录，**一条就够**。

> 不用再跑 `lark-cli config init`（会多建一个重复应用）或 `lark-cli auth login`。
> 万一某次 push/pull 提示缺权限，本工具会自动弹浏览器让你点「同意」补授权，无需手动命令。

## 使用

```bash
node fsync.js push 我的工作总结 --dry-run   # 先空跑看步骤
node fsync.js push 我的工作总结             # 本地 → 飞书（文件夹不存在会自动创建）
node fsync.js pull 我的工作总结             # 飞书 → 本地
node fsync.js push 我的工作总结 --force     # 强制覆盖（--if-exists=overwrite）
node fsync.js ls                           # 列出飞书根目录下的文件夹（排查用）
```

换台电脑：装好 lark-cli 并登录同一个飞书账号，把脚本和目标目录拷过去，`node fsync.js pull 我的工作总结` 即可。

## 云端结构

所有同步文件夹统一放在「我的空间」下的一个**基目录**里（默认名「文档同步」，见脚本顶部 `BASE_FOLDER`，留空 `''` 则直接用根目录），保持根目录清爽：

```
我的空间/
└── 文档同步/            ← 基目录，工具自动建一次
    ├── 我的工作总结/
    └── 读书笔记/
```

## 工作原理

`push 我的工作总结` 时：

1. `drive files list` 列根目录 → 找/建基目录「文档同步」，拿它的 token；
2. 列基目录 → 找名为「我的工作总结」且 `type=folder` 的项；找不到则 `create_folder` 在基目录下建一个；
3. `drive +push --folder-token <token> --local-dir .` 把本地目录镜像上去。

`pull` 同理，但找不到时报错（不会凭空建本地内容）。

## 覆盖策略

底层 `--if-exists`（脚本顶部常量可调）：push/pull 默认都用 `smart`（按修改时间增量，并保护较新的一方，避免冲掉未同步的改动）。`--force` = 当次改用 `overwrite` 无条件覆盖。

## 给非 IT 用户分发时

- **运行时授权**：工具会自动处理。遇到缺权限/未登录，它不会甩英文 JSON，而是提示「需要飞书授权，即将打开浏览器，请点同意」并自动发起授权，用户点一下「同意」即可继续。
- **一次性后台配置（需要懂行的人做一次）**：创建飞书自建应用、在「权限管理」里开通云空间相关权限、`lark-cli config init` 填 app_id/secret。**建议由你统一建一个应用、配齐权限**，再把 app_id/secret 给同事，他们只需 `config init` + 点浏览器同意，不用碰开放平台。

## 注意

- **文件级镜像**：同步目录里**所有文件**（不只 `.md`，也含图片等附件；docx 等在线文档不碰）。
- **重名文件夹**：若飞书根目录有多个同名文件夹，取最近修改的那个并告警，建议去飞书清理。
- **Windows**：脚本已处理（`shell` 调用 + 临时文件传中文参数），无需额外设置。
- 删除不自动同步：本地删了云端不会删（更安全）。
- 若 `create_folder` 报 “unknown flag”，跑 `lark-cli drive files create_folder --help` 看请求体 flag 名，改脚本顶部 `CREATE_DATA_FLAG`。
