# -*- coding: utf-8 -*-
"""探测当前账号下哪些 DashScope 模型可用（每条只发很小的一次请求）。

不打印 API Key，只打印模型名 / HTTP 状态 / 简短返回。
运行：.venv\\Scripts\\python.exe tools\\dev\\probe_models.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests

from app.config import Config

MODELS = [
    "qwen-max",
    "qwen-turbo",
    "qwen-plus",
    "qwen-flash",
    "qwen-mt-turbo",
    "qwen-mt-plus",
    "qwen3-max",
    "qwen-long",
]


def main() -> int:
    cfg = Config()
    base = cfg.translate_base_url().rstrip("/")
    key = cfg.translate_api_key()
    print(f"base={base}  key={'有' if key else '无'} ({len(key)} 字符)")
    if not key:
        return 1
    url = f"{base}/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    for model in MODELS:
        body = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "把 hello 翻译成中文，只回答译文"}],
        }
        t0 = time.time()
        try:
            r = requests.post(url, headers=headers, json=body, timeout=25)
            dt = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                try:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                except Exception:  # noqa: BLE001
                    text = r.text[:80]
                print(f"OK   {model:<14} {dt:>5}ms  -> {text!r}")
            else:
                try:
                    err = str(r.json().get("error", r.text))[:160]
                except Exception:  # noqa: BLE001
                    err = r.text[:160]
                print(f"FAIL {model:<14} HTTP {r.status_code}  {err}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR  {model:<14} {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
