#!/usr/bin/env node
/**
 * fsync —— 「本地子目录 ↔ 飞书同名文件夹」同步工具（纯名字驱动，不碰 token）
 *
 * 底层用 lark-cli：
 *   drive files list            → 列目录，找/建基目录「文档同步」，再在其中按名字找到目标文件夹，拿它的 token
 *   drive files create_folder   → 没有就创建（基目录或目标文件夹）
 *   drive +push / +pull         → 本地子目录 ↔ 该文件夹 的文件级镜像
 *
 * 特点：
 *   - 你只敲文件夹名字；token 全程由工具内部解析，你不用记、不用填。
 *   - 名字解析的是「你登录的飞书账号」，所以任意电脑上同名 = 同一个云端文件夹。
 *   - 无配置文件、无密钥，脚本可安全提交 GitHub。
 *
 * 布局（脚本与目标目录同级）：
 *   工具目录/
 *   ├── fsync.js
 *   ├── 我的工作总结/
 *   └── 读书笔记/
 *
 * 用法：
 *   node fsync.js push <目录名> [--force] [--dry-run]
 *   node fsync.js pull <目录名> [--force] [--dry-run]
 *   node fsync.js ls                      # 列出飞书「文档同步」下的文件夹（排查用）
 *   node fsync.js --help
 *
 * 首次 setup（每台机器一次）：
 *   npx @larksuite/cli@latest install    # 自动建应用 + 配置 + 登录，一条就够
 *   （缺权限时本工具会自动开浏览器引导授权，无需手动 config init / auth login）
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const HERE = __dirname;
const IS_WIN = process.platform === 'win32';
const LARK_BIN = process.env.LARK_CLI_BIN || 'lark-cli';

const PUSH_IF_EXISTS = 'smart'; // 本地较新才覆盖远端，远端较新则跳过
const PULL_IF_EXISTS = 'smart'; // 本地较新则跳过（保护未上传改动），远端较新才覆盖

// 所有同步文件夹都放在「我的空间」下这个基目录里，保持根目录清爽。留空 '' 则直接用根目录。
const BASE_FOLDER = '文档同步';

// create_folder 用 --data 传请求体 JSON（已对 `lark-cli drive files create_folder --help` 核实，支持 @file）。
const CREATE_DATA_FLAG = '--data';

// 本工具会用到的全部飞书权限。缺授权时一次性把这些都授权掉，避免一个个补。
const KNOWN_SCOPES = ['drive:drive', 'space:document:retrieve', 'space:folder:create'];
let didReauth = false; // 一个进程内只自动重新授权一次，防止死循环

// ---------- 小工具 ----------
function die(msg) { console.error(`\x1b[31m✗ ${msg}\x1b[0m`); process.exit(1); }
function ok(msg) { console.log(`\x1b[32m✓\x1b[0m ${msg}`); }
function warn(msg) { console.log(`\x1b[33m!\x1b[0m ${msg}`); }

function parseArgs(argv) {
  const out = { cmd: null, name: null, force: false, dryRun: false, help: false };
  for (const a of argv) {
    if (a === '--force') out.force = true;
    else if (a === '--dry-run') out.dryRun = true;
    else if (a === '--help' || a === '-h') out.help = true;
    else if (a.startsWith('-')) die(`未知参数: ${a}（试试 --help）`);
    else if (!out.cmd) out.cmd = a;
    else if (!out.name) out.name = a;
  }
  return out;
}

// 尝试把 lark-cli 的错误输出解析成结构化对象
function parseErr(raw) {
  try { const j = JSON.parse(raw); return j.error || j; } catch { return null; }
}

// 把开发者向的错误翻译成大白话；返回 null 表示这类错未专门翻译
function friendlyError(errObj) {
  if (!errObj) return null;
  const t = errObj.type;
  if (t === 'authentication') {
    return '你还没登录飞书（或登录已过期）。请运行：\n  lark-cli auth login --recommend\n登录后在浏览器点「同意」，再重试。';
  }
  if (t === 'authorization') {
    const miss = (errObj.missing_scopes || []).join('、') || '若干权限';
    return `飞书授权还差一些权限（${miss}）。\n  多半是这个应用在开放平台还没开通对应权限。请让应用管理员到\n  https://open.feishu.cn → 你的应用 → 权限管理，添加云空间相关权限后发布，\n  再运行：lark-cli auth login --recommend`;
  }
  return null;
}

// 检测“缺授权”并自动重新授权（开浏览器让用户点同意），成功发起则返回 true 以便重试一次
function tryAutoReauth(errObj) {
  if (didReauth || !errObj) return false;
  if (errObj.type !== 'authorization' && errObj.type !== 'authentication') return false;
  didReauth = true;
  const scopes = Array.from(new Set([...KNOWN_SCOPES, ...(errObj.missing_scopes || [])]));
  console.log('\x1b[33m需要飞书授权\x1b[0m：即将打开浏览器，请在页面上点【同意】完成授权，然后会自动继续……\n');
  const scopeArgs = scopes.flatMap((s) => ['--scope', s]);
  spawnSync(LARK_BIN, ['auth', 'login', ...scopeArgs], { stdio: 'inherit', shell: IS_WIN });
  return true;
}

// 运行 lark-cli 并捕获 stdout（给 list / create_folder 用）；自动处理授权类错误
function larkCapture(args, cwd) {
  const r = spawnSync(LARK_BIN, args, { encoding: 'utf8', shell: IS_WIN, cwd, maxBuffer: 64 * 1024 * 1024 });
  if (r.error && r.error.code === 'ENOENT') {
    die(`找不到命令 "${LARK_BIN}"。先装 lark-cli：\n  npx @larksuite/cli@latest install\n或设置环境变量 LARK_CLI_BIN`);
  }
  if (r.status === 0) return r.stdout || '';

  const raw = ((r.stdout || '') + (r.stderr || '')).trim();
  const errObj = parseErr(raw);
  if (tryAutoReauth(errObj)) return larkCapture(args, cwd); // 重新授权后重试一次
  const friendly = friendlyError(errObj);
  die(friendly || `操作失败：\n${raw}`);
}

// 运行 lark-cli 并把输出直接透传（给 +push / +pull 看进度）
function larkInherit(args, cwd) {
  const r = spawnSync(LARK_BIN, args, { stdio: 'inherit', shell: IS_WIN, cwd });
  if (r.error && r.error.code === 'ENOENT') {
    die(`找不到命令 "${LARK_BIN}"。先装 lark-cli：\n  npx @larksuite/cli@latest install\n或设置环境变量 LARK_CLI_BIN`);
  }
  if (typeof r.status === 'number' && r.status !== 0) {
    warn('同步未成功。若上面是「授权 / 权限 / unauthorized」相关的报错，运行 `node fsync.js ls` 会自动引导你重新授权后再试。');
    process.exitCode = r.status;
  }
}

function parseJson(s, what) {
  try { return JSON.parse(s); }
  catch { die(`解析 ${what} 的 JSON 输出失败。原始输出：\n${s.slice(0, 600)}`); }
}

// 列出某文件夹（parentToken 为空 = 我的空间根目录）下的 files 数组
function listFolder(parentToken) {
  const args = ['drive', 'files', 'list', '--json'];
  let tmp = null;
  if (parentToken) {
    tmp = path.join(HERE, '._fsync_list.json');
    fs.writeFileSync(tmp, JSON.stringify({ folder_token: parentToken }));
    args.push('--params', '@._fsync_list.json', '--page-all');
  }
  try {
    const out = larkCapture(args, HERE);
    const j = parseJson(out, 'drive files list');
    return j.files || (j.data && j.data.files) || [];
  } finally { if (tmp) fs.rmSync(tmp, { force: true }); }
}

// 在 parentToken（空=根目录）下创建名为 name 的文件夹，返回新 token
function createFolderIn(parentToken, name) {
  const tmp = path.join(HERE, '._fsync_create.json');
  fs.writeFileSync(tmp, JSON.stringify({ folder_token: parentToken, name }));
  try {
    const out = larkCapture(['drive', 'files', 'create_folder', CREATE_DATA_FLAG, '@._fsync_create.json'], HERE);
    const j = parseJson(out, 'drive files create_folder');
    const token = j.token || (j.data && j.data.token);
    if (!token) die(`创建文件夹后没解析到 token，原始输出：\n${out.slice(0, 600)}`);
    return token;
  } finally { fs.rmSync(tmp, { force: true }); }
}

// 在 parentToken（空=根目录）下按名字找文件夹；create=true 时找不到就建
function resolveIn(parentToken, name, where, { create }) {
  const matches = listFolder(parentToken).filter((f) => f.type === 'folder' && f.name === name);
  if (matches.length === 1) return matches[0].token;
  if (matches.length > 1) {
    warn(`${where}里有 ${matches.length} 个都叫「${name}」的文件夹，用最近修改的那个。建议去飞书删掉多余的。`);
    matches.sort((a, b) => Number(b.modified_time || 0) - Number(a.modified_time || 0));
    return matches[0].token;
  }
  if (!create) die(`${where}里没有名为「${name}」的文件夹。先在有内容的那台机器 push 一次（会自动创建）。`);
  const token = createFolderIn(parentToken, name);
  ok(`已在${where}创建文件夹「${name}」`);
  return token;
}

// 解析基目录 token（BASE_FOLDER 为空则返回 ''，表示直接用根目录）
function resolveBase({ create }) {
  if (!BASE_FOLDER) return '';
  return resolveIn('', BASE_FOLDER, '飞书根目录', { create });
}

// 解析「同步目录名」→ 飞书文件夹 token（位于基目录下）；create=true 时找不到就建
function resolveFolder(name, { create }) {
  const baseToken = resolveBase({ create });
  const where = BASE_FOLDER ? `飞书「${BASE_FOLDER}」文件夹` : '飞书根目录';
  return resolveIn(baseToken, name, where, { create });
}

function localDirOf(name) {
  const abs = path.resolve(HERE, name);
  return { abs, folderName: path.basename(abs) };
}

// ---------- 子命令 ----------
function cmdSync(cmd, opts) {
  if (!opts.name) die(`用法：node fsync.js ${cmd} <目录名>`);
  const { abs, folderName } = localDirOf(opts.name);

  if (cmd === 'push' && (!fs.existsSync(abs) || !fs.statSync(abs).isDirectory())) {
    die(`本地目录不存在：${abs}`);
  }

  const ifExists = opts.force ? 'overwrite' : (cmd === 'pull' ? PULL_IF_EXISTS : PUSH_IF_EXISTS);
  const mirror = cmd === 'pull' ? '+pull' : '+push';

  if (opts.dryRun) {
    const base = BASE_FOLDER ? `「${BASE_FOLDER}」` : '根目录';
    console.log(`[${cmd}] 「${folderName}」  (dry-run，以下为将执行的步骤，不实际执行)\n`);
    console.log(`  1) 在飞书根目录找/建基目录 ${base}，再在其中找文件夹「${folderName}」`);
    if (cmd === 'push') console.log(`     找不到则 create_folder 在 ${base} 下创建「${folderName}」`);
    console.log(`  2) 同步：${LARK_BIN} drive ${mirror} --folder-token <解析到的token> --local-dir . --if-exists ${ifExists}   (cwd=${abs})`);
    return;
  }

  const token = resolveFolder(folderName, { create: cmd === 'push' });
  if (cmd === 'pull') fs.mkdirSync(abs, { recursive: true });

  const args = ['drive', mirror, '--folder-token', token, '--local-dir', '.', '--if-exists', ifExists];
  console.log(`[${cmd}] 「${folderName}」 ↔ 飞书文件夹 ${token}\n`);
  larkInherit(args, abs);
}

function cmdLs() {
  let baseToken = '';
  const where = BASE_FOLDER ? `「${BASE_FOLDER}」` : '根目录';
  if (BASE_FOLDER) {
    const m = listFolder('').filter((f) => f.type === 'folder' && f.name === BASE_FOLDER);
    if (m.length === 0) return console.log(`飞书根目录下还没有「${BASE_FOLDER}」基目录（push 后会自动创建）。`);
    baseToken = m[0].token;
  }
  const folders = listFolder(baseToken).filter((f) => f.type === 'folder');
  if (folders.length === 0) return console.log(`飞书${where}下没有文件夹。`);
  console.log(`飞书${where}下的文件夹：`);
  for (const f of folders) console.log(`  ${f.name}  (${f.token})`);
}

function printHelp() {
  console.log(`fsync —— 本地子目录 ↔ 飞书同名文件夹（纯名字驱动，不碰 token）

用法：
  node fsync.js push <目录名> [--force] [--dry-run]   本地子目录 → 飞书同名文件夹（不存在自动建）
  node fsync.js pull <目录名> [--force] [--dry-run]   飞书同名文件夹 → 本地子目录
  node fsync.js ls                                    列出飞书「文档同步」下的文件夹
  node fsync.js --help

说明：
  - <目录名> 是与脚本同级的子目录；飞书侧用同名文件夹。任意电脑同名 = 同一个云端文件夹。
  - 全程不需要 token；工具按名字自动解析/创建。
  - --force 用 overwrite 无条件覆盖（默认 smart：按新旧增量并保护较新一方）。

首次 setup：npx @larksuite/cli@latest install   （一条就够；缺权限时工具会自动引导授权）`);
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help || !opts.cmd) return printHelp();
  switch (opts.cmd) {
    case 'ls': return cmdLs();
    case 'push':
    case 'pull': return cmdSync(opts.cmd, opts);
    default: die(`未知命令: ${opts.cmd}（试试 --help）`);
  }
}

main();
