# -*- coding: utf-8 -*-
"""打包后安检：确认发布产物里没有夹带 API Key 等敏感信息。

为什么需要：用户的 config.ini（含 API Key）绝对不能进发布包。
本脚本扫描 dist/ 与 installer/ 里的所有文件，命中即失败（退出码 1）。

用法（打包完成后执行，或直接用 build.bat 一键走完）：
    .venv\\Scripts\\python.exe tools\\check_package.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 控制台可能是 GBK(936)：把输出设成"遇到无法编码的字符就替换"，避免 print 直接抛异常
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "dist" / "HiLanguageOL", ROOT / "installer"]
SCAN_EXTS = {".exe", ".dll", ".pyd", ".ini", ".json", ".txt", ".py", ".pyc", ".log", ""}

# 常见密钥形态（DashScope/OpenAI 风格的 sk-，以及明显的占位后仍被填值的情况）
SECRET_PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9._\-]{20,}"),
    re.compile(rb"Bearer\s+[A-Za-z0-9._\-]{20,}"),
]
# 允许出现的示例/占位（避免误报）
WHITELIST = [b"sk-UPGRADE-TEST-MARKER", b"sk-test", b"sk-xxx", b"sk-ASR-MARKER"]


def redact(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return s[:10] + "***" + s[-4:] if len(s) > 16 else "***"


def scan_file(path: Path) -> list[str]:
    hits: list[str] = []
    try:
        data = path.read_bytes()
    except OSError:
        return hits
    for pat in SECRET_PATTERNS:
        for m in pat.finditer(data):
            raw = m.group(0)
            if any(w in raw for w in WHITELIST):
                continue
            hits.append(f"{path.relative_to(ROOT)}  疑似密钥: {redact(raw)}")
    return hits


def check_bundled_config() -> list[str]:
    """打包进去的配置模板里，api_key 必须是空的。"""
    problems: list[str] = []
    for rel in ("dist/HiLanguageOL/_internal/config.example.ini",
                "dist/HiLanguageOL/config.example.ini"):
        p = ROOT / rel
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("api_key") and "=" in s:
                val = s.split("=", 1)[1].strip()
                if val:
                    problems.append(f"{rel}  模板里的 api_key 非空: {val[:8]}***")
    return problems


def main() -> int:
    problems: list[str] = []
    scanned = 0
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in SCAN_EXTS:
                continue
            scanned += 1
            problems += scan_file(f)
    problems += check_bundled_config()

    print(f"[安检] 已扫描 {scanned} 个文件（dist/ 与 installer/）")
    if problems:
        print("[安检][未通过] 发现问题，禁止发布：")
        for p in problems:
            print("   -", p)
        return 1
    print("[安检][通过] 未发现 API Key 等敏感信息；打包用的配置模板 api_key 为空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
