# -*- coding: utf-8 -*-
"""版本号工具：installer.iss 是唯一版本源，其余文件跟着同步。

版本号原本散落在三处，手工改必漏：
  - installer.iss     -> #define MyAppVersion "1.0.0"   （安装包名、升级识别）
  - installer.iss     -> VersionInfoVersion=1.0.0.0     （安装包自身版本资源）
  - version_info.txt  -> FileVersion / ProductVersion   （exe 文件属性）

用法：
    --show        显示当前版本，例如 "当前版本: 1.0.2"
    --next        只打印下一个补丁版本（1.0.2 -> 1.0.3），给 build.bat 当默认值
    --auto        直接把补丁号 +1 并写入（无人值守打包）
    1.1.0         写入指定版本（三处一起改）
退出码：0 成功；2 版本号不合法或读不到当前版本。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer.iss"
VFI = ROOT / "version_info.txt"

VER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_version() -> str:
    """从 installer.iss 读当前版本；读不到返回 '?'。"""
    try:
        text = ISS.read_text(encoding="utf-8-sig")
    except OSError:
        return "?"
    m = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', text)
    return m.group(1) if m else "?"


def next_version(v: str) -> str:
    """补丁号 +1：1.0.2 -> 1.0.3。"""
    major, minor, patch = (int(x) for x in v.split("."))
    return f"{major}.{minor}.{patch + 1}"


def write_version(v: str) -> None:
    """把版本写进 installer.iss 与 version_info.txt。"""
    parts = [int(x) for x in v.split(".")]
    while len(parts) < 4:
        parts.append(0)
    quad = ", ".join(str(x) for x in parts[:4])
    dotted = ".".join(str(x) for x in parts[:4])

    iss = ISS.read_text(encoding="utf-8-sig")
    iss = re.sub(r'(#define\s+MyAppVersion\s+")[^"]+(")', rf"\g<1>{v}\g<2>", iss)
    iss = re.sub(r"(?m)^(\s*VersionInfoVersion\s*=)[\d.]+",
                 rf"\g<1>{dotted}", iss)
    ISS.write_text(iss, encoding="utf-8-sig", newline="\r\n")

    vfi = VFI.read_text(encoding="utf-8")
    vfi = re.sub(r"filevers=\([^)]*\)", f"filevers=({quad})", vfi)
    vfi = re.sub(r"prodvers=\([^)]*\)", f"prodvers=({quad})", vfi)
    vfi = re.sub(r"StringStruct\('FileVersion', '[^']*'\)",
                 f"StringStruct('FileVersion', '{dotted}')", vfi)
    vfi = re.sub(r"StringStruct\('ProductVersion', '[^']*'\)",
                 f"StringStruct('ProductVersion', '{dotted}')", vfi)
    VFI.write_text(vfi, encoding="utf-8")


def _report(old: str, new: str) -> None:
    print(f"版本号已更新: {old} -> {new}")
    print(f"  - {ISS.name}: MyAppVersion = {new}, VersionInfoVersion = {new}.0")
    print(f"  - {VFI.name}: FileVersion / ProductVersion = {new}.0")
    print(f"  下次打包默认: {next_version(new)}（直接回车即可）")


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0].strip() if args else "--show"

    if cmd in ("--show", "-s"):
        print(f"当前版本: {read_version()}")
        return 0

    cur = read_version()

    if cmd in ("--next", "-n"):
        if not VER_RE.match(cur):
            print("", end="")
            print("[错误] 读不到当前版本号，请检查 installer.iss", file=sys.stderr)
            return 2
        print(next_version(cur))
        return 0

    if cmd in ("--auto", "--bump"):
        if not VER_RE.match(cur):
            print("[错误] 读不到当前版本号，请检查 installer.iss", file=sys.stderr)
            return 2
        new = next_version(cur)
        write_version(new)
        _report(cur, new)
        return 0

    new = cmd.lstrip("v")
    if not VER_RE.match(new):
        print(f"[错误] 版本号格式应为 X.Y.Z（例如 1.1.0），收到: {cmd!r}")
        return 2
    write_version(new)
    _report(cur, new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
