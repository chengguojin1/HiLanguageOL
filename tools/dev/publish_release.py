# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传安装包。

需要环境变量 GH_TOKEN（由 git credential fill 注入，不落盘）。
用法：python tools/dev/publish_release.py <tag> <name> <asset-path>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

OWNER = "chengguojin1"
REPO = "HiLanguageOL"
PROXY = {"https": "http://127.0.0.1:33210", "http": "http://127.0.0.1:33210"}

BODY = """## 中英俄实时翻译软件 1.0.8

### 修复
- **蓝牙耳机麦克风采不到声音**：麦克风端点改为优先使用 WASAPI，避免落到 MME 的 44.1kHz 立体声端点（蓝牙 HFP 麦克风实际是 16kHz 单声道）。
- 翻译接口 400 错误现在会给出具体原因（欠费 / 内容审核 / 参数），不再只显示 "Bad Request"。
- qwen3.x 系列默认思考模式导致翻译 7~23 秒甚至空回复，现已自动关闭思考。

### 界面
- 软件更名为「**中英俄实时翻译软件**」，支持中 / 英 / 俄互译。
- 深色玻璃 / 极光玻璃两套皮肤。
- 字幕模式无背景、左对齐、文字颜色自动跟随底色（亮底黑字、暗底白字）。
- `⋯` 更多菜单与托盘菜单改为玻璃材质。

### 安装
下载 `HiLanguageOL-Setup-1.0.8.exe` 双击安装即可；升级安装不会清除 `%APPDATA%\\HiLanguageOL` 里的配置与录音。
"""


def main() -> int:
    if len(sys.argv) < 4:
        print("用法: publish_release.py <tag> <name> <asset>")
        return 2
    tag, name, asset = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("缺少 GH_TOKEN")
        return 2
    if not asset.exists():
        print("找不到文件:", asset)
        return 2

    s = requests.Session()
    s.proxies = PROXY
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    r = s.get(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{tag}",
              headers=h, timeout=60)
    if r.status_code == 404:
        r = s.post(f"https://api.github.com/repos/{OWNER}/{REPO}/releases", headers=h,
                   json={"tag_name": tag, "name": name, "body": BODY,
                         "draft": False, "prerelease": False}, timeout=120)
        if r.status_code >= 300:
            print("创建 release 失败:", r.status_code, r.text[:300])
            return 1
        print("已创建 release")
    elif r.status_code == 200:
        print("release 已存在，复用")
    else:
        print("查询 release 失败:", r.status_code, r.text[:200])
        return 1

    rel = r.json()
    rid = rel["id"]
    print("release id =", rid, rel.get("html_url"))

    for a in rel.get("assets", []):
        if a.get("name") == asset.name:
            print("资产已存在，先删除:", a["id"])
            s.delete(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{a['id']}",
                     headers=h, timeout=60)

    data = asset.read_bytes()
    print(f"上传 {asset.name} ({len(data)/1024/1024:.1f} MB) ...")
    up = s.post(f"https://uploads.github.com/repos/{OWNER}/{REPO}/releases/{rid}/assets",
                headers={**h, "Content-Type": "application/octet-stream"},
                params={"name": asset.name}, data=data, timeout=1800)
    print("upload:", up.status_code)
    if up.status_code >= 300:
        print(up.text[:400])
        return 1
    print("完成:", up.json().get("browser_download_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
