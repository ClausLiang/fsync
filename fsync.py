#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fsync.py —— 「本地子目录 ↔ 飞书同名文件夹」同步工具（纯 Python，直连飞书 API，不依赖 lark-cli）

自己接管登录与凭据（无需 lark-cli），主要特点：
  - 独立 OAuth v2 登录：本地回调服务器 + 浏览器点「同意」，拿 user_access_token / refresh_token，
    存在 ~/.fsync/，过期用 refresh_token 自动续，无感。
  - app_id / app_secret 由 `setup` 一次性录入，也存 ~/.fsync/（仓库外，永不进 git）。
  - 同步：直接调 Drive v1 接口（list / create_folder / upload_all / download / delete）。
  - 纯名字驱动：你只敲文件夹名字；基目录统一放在「我的空间」下的「文档同步」里。
  - smart 增量（按修改时间，保护较新一方）+ --force 无条件覆盖；递归子目录；只下载 type=file。

脚本本身不含任何密钥，可安全提交 GitHub。

用法：
  python3 fsync.py setup                 # 首次：录入 app_id/app_secret 并登录
  python3 fsync.py login                 # 重新登录（换账号 / 清了缓存时）
  python3 fsync.py push <目录名> [--force] [--dry-run]
  python3 fsync.py pull <目录名> [--force] [--dry-run]
  python3 fsync.py ls                    # 列出飞书「文档同步」下的文件夹
  python3 fsync.py logout                # 删除本地登录缓存
  python3 fsync.py --help

布局（脚本与目标目录同级）：
  工具目录/
  ├── fsync.py
  ├── 我的工作总结/
  └── 读书笔记/
"""

import json
import os
import sys
import time
import secrets
import ssl
import webbrowser
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------- 配置 ----------------
HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".fsync"
CRED_FILE = CONFIG_DIR / "credentials.json"     # {app_id, app_secret}
TOKEN_FILE = CONFIG_DIR / "token.json"          # {access_token, refresh_token, expires_at, refresh_expires_at}

BASE_FOLDER = "文档同步"          # 所有同步文件夹的基目录；留空 "" 则直接用根目录
SCOPES = "drive:drive offline_access"  # offline_access 才会返回 refresh_token
SKEW = 2                          # 修改时间比较的容差（秒）
MAX_UPLOAD = 20 * 1024 * 1024     # upload_all 上限 20MB，超过的本工具暂跳过并提示

PORT = int(os.environ.get("FSYNC_PORT", "17777"))
REDIRECT_URI = os.environ.get("FSYNC_REDIRECT", f"http://localhost:{PORT}/callback")

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
API = "https://open.feishu.cn/open-apis"

# 视为「令牌失效，需要刷新/重登」的飞书错误码
AUTH_CODES = {99991663, 99991664, 99991661, 99991671, 99991677, 20005}

# ---------------- 终端小工具 ----------------
def _c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m" if sys.stdout.isatty() else s

def die(msg):
    print(_c("31", f"✗ {msg}"), file=sys.stderr)
    sys.exit(1)

def ok(msg):
    print(f"{_c('32', '✓')} {msg}")

def warn(msg):
    print(f"{_c('33', '!')} {msg}")

def info(msg):
    print(msg)


# ---------------- 凭据 / 令牌存储 ----------------
def _ensure_dir():
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)

def _save_json(path, obj):
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)

def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def load_credentials():
    cred = _load_json(CRED_FILE)
    if not cred or not cred.get("app_id") or not cred.get("app_secret"):
        die("还没配置应用凭据。先运行：python3 fsync.py setup")
    return cred

_token_cache = None

def load_token():
    global _token_cache
    if _token_cache is None:
        _token_cache = _load_json(TOKEN_FILE) or {}
    return _token_cache

def save_token(tok):
    global _token_cache
    _token_cache = tok
    _save_json(TOKEN_FILE, tok)


# ---------------- 底层 HTTP ----------------
class FeishuError(Exception):
    def __init__(self, code, msg, status=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.status = status

def _ssl_context():
    """构造 HTTPS 校验上下文。

    公司网络常有 TLS 拦截代理，会换上自签名根证书导致默认校验失败。出口：
      - FSYNC_CA_BUNDLE / SSL_CERT_FILE / REQUESTS_CA_BUNDLE：指向公司根证书 .pem，照常校验（推荐）。
      - FSYNC_INSECURE=1：彻底关闭校验（仅在信任当前网络时临时用）。
    """
    if os.environ.get("FSYNC_INSECURE") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    cafile = (os.environ.get("FSYNC_CA_BUNDLE")
              or os.environ.get("SSL_CERT_FILE")
              or os.environ.get("REQUESTS_CA_BUNDLE"))
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()

def _request(method, url, headers=None, data=None):
    """发一个请求，返回 (status, body_bytes)。HTTP 错误也读出 body 一起返回，不抛。"""
    req = Request(url, data=data, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=60, context=_ssl_context()) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()
    except URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            die("HTTPS 证书校验失败：当前网络疑似有 TLS 拦截代理（公司防火墙/安全软件）。\n"
                "  方案一（推荐）：导出公司根证书，再指给 fsync：\n"
                "    export FSYNC_CA_BUNDLE=/路径/corp-ca.pem\n"
                "  方案二（临时、不校验）：export FSYNC_INSECURE=1\n"
                f"  原始错误：{reason}")
        die(f"网络请求失败：{reason}。检查一下网络连接是否正常。")

def _parse_envelope(status, body):
    """解析 {code,msg,data} 信封；code!=0 抛 FeishuError。返回 data。"""
    try:
        j = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FeishuError(-1, f"无法解析返回内容（HTTP {status}）：{body[:300]!r}", status)
    code = j.get("code", -1)
    if code != 0:
        raise FeishuError(code, j.get("msg", "未知错误"), status)
    return j.get("data", {})


# ---------------- OAuth 登录 ----------------
_SUCCESS_HTML = (
    "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
    "<title>授权完成</title></head><body style='font-family:sans-serif;text-align:center;margin-top:80px'>"
    "<h2>✅ 授权成功</h2><p>可以关闭此页面，回到终端继续。</p></body></html>"
).encode("utf-8")

def _oauth_browser_flow(app_id):
    """开浏览器跑一遍授权码流程，返回 authorization code。"""
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "prompt": "consent",
    }
    auth_url = AUTHORIZE_URL + "?" + urlencode(params)
    callback_path = urlparse(REDIRECT_URI).path or "/"
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            q = parse_qs(parsed.query)
            captured["code"] = (q.get("code") or [None])[0]
            captured["state"] = (q.get("state") or [None])[0]
            captured["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML)

        def log_message(self, *a):
            pass  # 静音

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        die(f"本地端口 {PORT} 被占用，无法接收回调（{e}）。\n"
            f"换个端口：设环境变量 FSYNC_PORT=别的端口，并把飞书应用后台的重定向 URL 同步改成 "
            f"http://localhost:<新端口>/callback。")

    info("正在打开浏览器完成飞书授权……请在页面上点【同意】。")
    info(f"若浏览器没自动打开，请手动访问：\n  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    while "code" not in captured and "error" not in captured:
        server.handle_request()
    server.server_close()

    if captured.get("error"):
        die("你在授权页点了拒绝（或授权失败）。重新运行命令再试一次即可。")
    if captured.get("state") != state:
        die("授权回调的 state 不匹配（可能遇到了安全问题），请重试。")
    if not captured.get("code"):
        die("没拿到授权码，请重试。")
    return captured["code"]

def _exchange_token(grant):
    """用授权码或 refresh_token 换取令牌。grant 是请求体 dict。"""
    cred = load_credentials()
    body = dict(grant, client_id=cred["app_id"], client_secret=cred["app_secret"])
    data = json.dumps(body).encode("utf-8")
    status, raw = _request(
        "POST", TOKEN_URL,
        headers={"Content-Type": "application/json; charset=utf-8"},
        data=data,
    )
    try:
        j = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        die(f"换取令牌时返回异常（HTTP {status}）：{raw[:300]!r}")
    if j.get("code", -1) != 0:
        return None, j  # 失败，交给调用方决定（刷新失败 → 重新登录）
    now = int(time.time())
    tok = {
        "access_token": j["access_token"],
        "refresh_token": j.get("refresh_token"),
        "expires_at": now + int(j.get("expires_in", 7200)),
        "refresh_expires_at": now + int(j.get("refresh_token_expires_in", 0)) if j.get("refresh_token_expires_in") else 0,
    }
    return tok, j

def do_login():
    """完整登录：浏览器授权 → 换 token → 保存。"""
    cred = load_credentials()
    code = _oauth_browser_flow(cred["app_id"])
    tok, j = _exchange_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    if not tok:
        die(_friendly_oauth(j))
    if not tok.get("refresh_token"):
        warn("没拿到 refresh_token（令牌将无法自动续期）。"
             "请确认飞书应用已开通 offline_access 权限并发布，然后重新登录。")
    save_token(tok)
    ok("飞书登录成功，凭据已保存到 ~/.fsync/。")

def _try_refresh():
    """用 refresh_token 续期；成功返回 True。"""
    tok = load_token()
    rt = tok.get("refresh_token")
    if not rt:
        return False
    new, j = _exchange_token({"grant_type": "refresh_token", "refresh_token": rt})
    if not new:
        return False
    if not new.get("refresh_token"):
        new["refresh_token"] = rt  # 有些返回不带新 refresh_token，沿用旧的
        new["refresh_expires_at"] = tok.get("refresh_expires_at", 0)
    save_token(new)
    return True

def ensure_token():
    """保证有可用 access_token：缺 → 登录；快过期 → 刷新，刷新失败 → 登录。"""
    tok = load_token()
    if not tok.get("access_token"):
        do_login()
        return
    if int(time.time()) >= tok.get("expires_at", 0) - 60:
        if not _try_refresh():
            info("登录已过期，需要重新授权一次……")
            do_login()


# ---------------- 带鉴权 & 自动续期的 API 调用 ----------------
def _auth_header():
    return {"Authorization": "Bearer " + load_token().get("access_token", "")}

def call_api(method, path, params=None, json_body=None, multipart=None, want_raw=False, _retried=False):
    """
    调用 /open-apis 下的接口，自动带 Bearer、自动在令牌失效时刷新/重登并重试一次。
    multipart=(fields_dict, file_field, filename, file_bytes) 时发 multipart 上传。
    want_raw=True 时（下载）成功直接返回原始 bytes。
    """
    url = API + path
    if params:
        url += "?" + urlencode(params)
    headers = _auth_header()
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
    elif multipart is not None:
        data, ctype = _build_multipart(*multipart)
        headers["Content-Type"] = ctype

    status, raw = _request(method, url, headers=headers, data=data)

    # 令牌失效 → 刷新/重登，重试一次
    if not _retried and _looks_like_auth_error(status, raw):
        if not _try_refresh():
            info("登录状态失效，重新授权一次……")
            do_login()
        return call_api(method, path, params, json_body, multipart, want_raw, _retried=True)

    if want_raw:
        if status == 200 and not raw[:1] == b"{":
            return raw
        # 200 但返回 JSON，多半是错误
        _parse_envelope(status, raw)
        return raw
    return _parse_envelope(status, raw)

def _looks_like_auth_error(status, raw):
    if status == 401:
        return True
    try:
        j = json.loads(raw.decode("utf-8"))
        return j.get("code") in AUTH_CODES
    except (ValueError, UnicodeDecodeError):
        return False

def _build_multipart(fields, file_field, filename, file_bytes):
    boundary = "----fsync" + uuid4().hex
    crlf = b"\r\n"
    buf = bytearray()
    for k, v in fields.items():
        buf += b"--" + boundary.encode() + crlf
        buf += f'Content-Disposition: form-data; name="{k}"'.encode("utf-8") + crlf + crlf
        buf += str(v).encode("utf-8") + crlf
    buf += b"--" + boundary.encode() + crlf
    buf += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode("utf-8") + crlf
    buf += b"Content-Type: application/octet-stream" + crlf + crlf
    buf += file_bytes + crlf
    buf += b"--" + boundary.encode() + b"--" + crlf
    return bytes(buf), "multipart/form-data; boundary=" + boundary


# ---------------- Drive 操作 ----------------
_root_token = None

def get_root_token():
    global _root_token
    if _root_token is None:
        data = call_api("GET", "/drive/explorer/v2/root_folder/meta")
        _root_token = data.get("token")
        if not _root_token:
            die("没取到「我的空间」根目录 token，可能是权限不足。")
    return _root_token

def list_folder(folder_token):
    """列出某文件夹下全部条目（自动翻页）。返回 [{token,name,type,modified_time,...}]。"""
    files = []
    page_token = None
    while True:
        params = {"folder_token": folder_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token
        data = call_api("GET", "/drive/v1/files", params=params)
        files.extend(data.get("files", []))
        if data.get("has_more") and data.get("next_page_token"):
            page_token = data["next_page_token"]
        else:
            return files

def find_child_folder(parent_token, name):
    for f in list_folder(parent_token):
        if f.get("type") == "folder" and f.get("name") == name:
            return f.get("token")
    return None

def create_folder(parent_token, name):
    data = call_api("POST", "/drive/v1/files/create_folder",
                    json_body={"name": name, "folder_token": parent_token})
    token = data.get("token")
    if not token:
        die(f"创建文件夹「{name}」后没拿到 token。")
    return token

def upload_file(folder_token, path: Path):
    raw = path.read_bytes()
    if len(raw) > MAX_UPLOAD:
        warn(f"跳过「{path.name}」：{len(raw)//1024//1024}MB 超过 upload_all 的 20MB 上限（大文件分片上传暂未实现）。")
        return None
    data = call_api("POST", "/drive/v1/files/upload_all", multipart=(
        {"file_name": path.name, "parent_type": "explorer",
         "parent_node": folder_token, "size": len(raw)},
        "file", path.name, raw,
    ))
    return data.get("file_token")

def delete_file(file_token):
    call_api("DELETE", f"/drive/v1/files/{file_token}", params={"type": "file"})

def download_file(file_token, dest: Path, mtime=None):
    raw = call_api("GET", f"/drive/v1/files/{file_token}/download", want_raw=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    if mtime:
        os.utime(dest, (mtime, mtime))  # 对齐云端修改时间，便于后续 smart 比较


# ---------------- 文件夹解析（基目录 → 子目录） ----------------
def resolve_base(create):
    if not BASE_FOLDER:
        return get_root_token()
    root = get_root_token()
    tok = find_child_folder(root, BASE_FOLDER)
    if tok:
        return tok
    if not create:
        return None
    tok = create_folder(root, BASE_FOLDER)
    ok(f"已在「我的空间」创建基目录「{BASE_FOLDER}」")
    return tok

def resolve_target(name, create):
    base = resolve_base(create)
    where = f"「{BASE_FOLDER}」" if BASE_FOLDER else "根目录"
    if base is None:        # 仅 dry-run/pull 在缺失时会到这
        return None
    tok = find_child_folder(base, name)
    if tok:
        return tok
    if not create:
        return None
    tok = create_folder(base, name)
    ok(f"已在{where}创建文件夹「{name}」")
    return tok


# ---------------- push / pull 递归镜像 ----------------
def _skip(name):
    return name.startswith(".") or name.startswith("~$")

def _mtime_int(p: Path):
    return int(p.stat().st_mtime)

def push_dir(local: Path, token, opts, execute):
    """本地目录 → 飞书文件夹。token 为 None 表示该云端文件夹尚不存在（dry-run）。"""
    remote = {}
    if token:
        for e in list_folder(token):
            remote[e["name"]] = e

    for entry in sorted(local.iterdir(), key=lambda p: p.name):
        if _skip(entry.name):
            continue
        if entry.is_dir():
            child = remote.get(entry.name)
            child_token = child["token"] if (child and child.get("type") == "folder") else None
            if child_token is None:
                if execute:
                    child_token = create_folder(token, entry.name)
                    ok(f"建子文件夹 {entry.name}/")
                else:
                    info(f"  + 建子文件夹 {entry.name}/")
            push_dir(entry, child_token, opts, execute)
            continue

        r = remote.get(entry.name)
        rel = entry.name
        if r is None:
            _do_upload(token, entry, None, rel, execute, label="上传(新)")
        elif r.get("type") != "file":
            warn(f"跳过「{rel}」：云端同名的是在线文档（{r.get('type')}），不覆盖。")
        else:
            newer = _mtime_int(entry) > int(r.get("modified_time", 0)) + SKEW
            if opts["force"] or newer:
                _do_upload(token, entry, r["token"], rel, execute,
                           label="覆盖" if opts["force"] else "上传(本地较新)")
            else:
                if not execute:
                    info(f"  = 跳过 {rel}（云端不旧）")

def _do_upload(token, path, existing_token, rel, execute, label):
    if not execute:
        info(f"  ↑ {label} {rel}")
        return
    if existing_token:
        delete_file(existing_token)  # 覆盖 = 先删旧再传（飞书上传不就地替换）
    if upload_file(token, path) is not None:
        ok(f"↑ {rel}")

def pull_dir(local: Path, token, opts, execute):
    """飞书文件夹 → 本地目录。只取 type=file；在线文档忽略。"""
    if execute:
        local.mkdir(parents=True, exist_ok=True)
    for e in list_folder(token):
        name = e.get("name")
        if e.get("type") == "folder":
            pull_dir(local / name, e["token"], opts, execute)
        elif e.get("type") == "file":
            dest = local / name
            rmt = int(e.get("modified_time", 0))
            if not dest.exists():
                _do_download(e["token"], dest, rmt, name, execute, "下载(新)")
            else:
                newer = rmt > _mtime_int(dest) + SKEW
                if opts["force"] or newer:
                    _do_download(e["token"], dest, rmt, name, execute,
                                 "覆盖" if opts["force"] else "下载(云端较新)")
                elif not execute:
                    info(f"  = 跳过 {name}（本地不旧）")
        # 其它类型（docx/sheet/bitable…在线文档）忽略

def _do_download(file_token, dest, mtime, name, execute, label):
    if not execute:
        info(f"  ↓ {label} {name}")
        return
    download_file(file_token, dest, mtime)
    ok(f"↓ {name}")


# ---------------- 子命令 ----------------
def cmd_sync(cmd, name, opts):
    if not name:
        die(f"用法：python3 fsync.py {cmd} <目录名>")
    local = (HERE / name).resolve()
    if cmd == "push" and not local.is_dir():
        die(f"本地目录不存在：{local}")

    ensure_token()
    base_label = f"「{BASE_FOLDER}」" if BASE_FOLDER else "根目录"
    dry = opts["dry_run"]

    if cmd == "push":
        token = resolve_target(name, create=not dry)
        if dry:
            info(f"[push] 「{name}」 (dry-run，仅预览)\n  目标：{base_label} 下的「{name}」"
                 + ("（尚不存在，将创建）" if token is None else "") + "\n")
            push_dir(local, token, opts, execute=False)
        else:
            info(f"[push] 「{name}」 ↔ 飞书 {token}\n")
            push_dir(local, token, opts, execute=True)
            ok("push 完成。")
    else:  # pull
        token = resolve_target(name, create=False)
        if token is None:
            die(f"云端{base_label}下没有「{name}」文件夹。先在有内容的机器上 push 一次。")
        if dry:
            info(f"[pull] 「{name}」 (dry-run，仅预览)\n")
            pull_dir(local, token, opts, execute=False)
        else:
            info(f"[pull] 飞书 {token} ↔ 「{name}」\n")
            pull_dir(local, token, opts, execute=True)
            ok("pull 完成。")

def cmd_ls():
    ensure_token()
    where = f"「{BASE_FOLDER}」" if BASE_FOLDER else "根目录"
    base = resolve_base(create=False)
    if base is None:
        info(f"飞书「我的空间」下还没有「{BASE_FOLDER}」基目录（push 后会自动创建）。")
        return
    folders = [f for f in list_folder(base) if f.get("type") == "folder"]
    if not folders:
        info(f"飞书{where}下没有文件夹。")
        return
    info(f"飞书{where}下的文件夹：")
    for f in folders:
        info(f"  {f['name']}  ({f['token']})")

def cmd_setup():
    info("配置飞书应用凭据（一次性）。\n"
         "本工具直连飞书 API，需要你先在飞书开放平台建一个【自建应用】并开通权限。\n"
         "请到 https://open.feishu.cn 准备好：\n"
         "  1) 权限管理：开通 drive:drive、offline_access，并「发布」应用；\n"
         "     ⚠️ 务必开通的是【用户身份】权限，不是【应用身份】——本工具以你本人身份登录，\n"
         "        文档存进你自己的「我的空间」；只开应用身份会授权失败、也进不了你的个人空间。\n"
         f"  2) 安全设置 → 重定向 URL，添加：{REDIRECT_URI}\n"
         "  3) 在「凭证与基础信息」里复制 App ID / App Secret 填到下面。\n")
    app_id = input("App ID: ").strip()
    app_secret = input("App Secret: ").strip()
    if not app_id or not app_secret:
        die("App ID / App Secret 不能为空。")
    _save_json(CRED_FILE, {"app_id": app_id, "app_secret": app_secret})
    ok("凭据已保存到 ~/.fsync/credentials.json")
    do_login()

def cmd_logout():
    n = 0
    for p in (TOKEN_FILE,):
        if p.exists():
            p.unlink()
            n += 1
    ok("已清除本地登录缓存。" if n else "本来就没有登录缓存。")
    info("（应用凭据 credentials.json 保留；要清掉就删 ~/.fsync/ 整个目录。）")


# ---------------- 友好错误翻译 ----------------
def _friendly_oauth(j):
    err = (j or {}).get("error", "")
    desc = (j or {}).get("error_description", "")
    if err == "invalid_client":
        return f"App ID / App Secret 不对（{desc}）。重新运行：python3 fsync.py setup 改一下。"
    if err in ("invalid_grant", "invalid_request"):
        return f"授权码无效或已过期（{desc}）。直接重跑命令再授权一次即可。"
    return f"登录失败：{err} {desc}".strip()

def _friendly_feishu(e: FeishuError):
    # 权限/scope 类
    if e.code in (99991672, 99991679, 1061045, 1062023):
        return ("飞书提示权限不足。多半是应用没开通云空间权限。\n"
                "请到 https://open.feishu.cn → 你的应用 → 权限管理，开通 drive:drive（云空间）相关权限并发布，\n"
                "然后重新运行：python3 fsync.py login")
    return None


def parse_args(argv):
    opts = {"cmd": None, "name": None, "force": False, "dry_run": False, "help": False}
    for a in argv:
        if a in ("--help", "-h"):
            opts["help"] = True
        elif a == "--force":
            opts["force"] = True
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a.startswith("-"):
            die(f"未知参数：{a}（试试 --help）")
        elif opts["cmd"] is None:
            opts["cmd"] = a
        elif opts["name"] is None:
            opts["name"] = a
    return opts

HELP = """fsync.py —— 本地子目录 ↔ 飞书同名文件夹（纯 Python，直连飞书 API）

用法：
  python3 fsync.py setup                              首次：录入 app_id/app_secret 并登录
  python3 fsync.py login                              重新登录
  python3 fsync.py push <目录名> [--force] [--dry-run]  本地 → 飞书（不存在自动建）
  python3 fsync.py pull <目录名> [--force] [--dry-run]  飞书 → 本地
  python3 fsync.py ls                                 列出飞书「{base}」下的文件夹
  python3 fsync.py logout                             清除本地登录缓存
  python3 fsync.py --help

说明：
  - <目录名> 是与脚本同级的子目录；飞书侧用同名文件夹。任意电脑同名 = 同一个云端文件夹。
  - 全程不需要 token；登录态存在 ~/.fsync/，过期自动续。
  - 默认 smart 增量（按修改时间，保护较新一方）；--force 无条件覆盖。
  - 递归子目录；上传所有文件；下载只取普通文件（在线文档不碰）。
""".format(base=BASE_FOLDER or "根目录")

def main():
    opts = parse_args(sys.argv[1:])
    if opts["help"] or not opts["cmd"]:
        print(HELP)
        return
    try:
        cmd = opts["cmd"]
        if cmd == "setup":
            cmd_setup()
        elif cmd == "login":
            do_login()
        elif cmd == "logout":
            cmd_logout()
        elif cmd == "ls":
            cmd_ls()
        elif cmd in ("push", "pull"):
            cmd_sync(cmd, opts["name"], opts)
        else:
            die(f"未知命令：{cmd}（试试 --help）")
    except FeishuError as e:
        friendly = _friendly_feishu(e)
        die(friendly or f"飞书接口报错：{e.msg}（code={e.code}）")
    except KeyboardInterrupt:
        print()
        die("已取消。")


if __name__ == "__main__":
    main()
