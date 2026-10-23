# -*- coding: utf-8 -*-
"""复现翻译 400：用 App 的真实提示词 + 真实上下文，打印响应体。

运行：.venv\\Scripts\\python.exe tools\\dev\\probe_400.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests

from app.config import Config
from app.translator import build_translate_messages

TEXTS = [
    "Итак, производная функции — это скорость изменения функции в данной точке.",
    "Теорема утверждает, что если функция дифференцируема в точке, то она непрерывна в этой точке.",
    "Да.",
    "А если функция не дифференцируема?",
]


def call(cfg, model: str, text: str, context: list[tuple[str, str]]) -> None:
    msgs = build_translate_messages(
        context, text, cfg.source_lang(), cfg.target_lang(),
        cfg.get("translate", "topic"), cfg.get("translate", "glossary"),
        auto_correct=cfg.get_bool("translate", "auto_correct"))
    body = {
        "model": model,
        "messages": msgs,
        "stream": False,
        "temperature": cfg.get_float("translate", "temperature"),
        "max_tokens": cfg.get_int("translate", "max_tokens"),
    }
    url = f"{cfg.translate_base_url().rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg.translate_api_key()}"}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    tag = "OK  " if r.status_code == 200 else "FAIL"
    print(f"{tag} {model:<11} ctx={len(context):<2} len={len(text):<4} HTTP {r.status_code}")
    if r.status_code != 200:
        print("     body:", r.text[:400])
    else:
        print("     ->", r.json()["choices"][0]["message"]["content"][:60])


def main() -> int:
    cfg = Config()
    model = cfg.get("translate", "model_name")
    print("model =", model, "| auto_correct =", cfg.get_bool("translate", "auto_correct"),
          "| temp =", cfg.get_float("translate", "temperature"),
          "| max_tokens =", cfg.get_int("translate", "max_tokens"))
    ctx0: list[tuple[str, str]] = []
    ctx5 = [(f"Что такое производная {i}?", f"什么是导数 {i}？") for i in range(5)]

    print("\n[A] 无上下文")
    for t in TEXTS:
        call(cfg, model, t, ctx0)

    print("\n[B] 带 5 条上文（App 正常运行时的样子）")
    for t in TEXTS:
        call(cfg, model, t, ctx5)

    print("\n[C] 超长句（模拟长篇讲课）")
    long_text = " ".join(TEXTS * 4)
    call(cfg, model, long_text, ctx5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
