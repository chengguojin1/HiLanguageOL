# -*- coding: utf-8 -*-
"""翻译质量盲测：同一套 App 提示词，多个模型翻同一批俄语课句子。

运行：.venv\\Scripts\\python.exe tools\\dev\\bench_translate.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests

from app.config import Config
from app.translator import build_translate_messages

SENTS = [
    "Итак, производная функции — это скорость изменения функции в данной точке, "
    "а её геометрический смысл — угловой коэффициент касательной.",
    "Теорема утверждает, что если функция дифференцируема в точке, то она непрерывна "
    "в этой точке, но обратное, вообще говоря, неверно.",
    "Ну, то есть, смотрите, вот эта штука определена не совсем корректно, "
    "потому что мы не оговорили область определения.",
    "Мы будем использовать метод Ньютона, и на каждом шаге нам нужно вычислять "
    "значение функции и её производной.",
    "На следующей паре мы разберём пределы и непрерывность, а эту тему закрепим "
    "на семинаре в четверг.",
]

# (model, 需要关思考, 是否 qwen-mt 专用接口)
MODELS = [
    ("qwen-flash", False, False),
    ("qwen-plus", False, False),
    ("qwen3.6-plus", True, False),
    ("qwen3.6-flash", True, False),
    ("qwen3.8-flash", True, False),
    ("qwen-mt-plus", False, True),
]


def translate(cfg, model: str, text: str, no_think: bool, is_mt: bool):
    url = cfg.translate_base_url().rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.translate_api_key()}",
               "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": build_translate_messages(
            [], text, cfg.source_lang(), cfg.target_lang(),
            cfg.get("translate", "topic"), cfg.get("translate", "glossary"),
            auto_correct=False),
        "max_tokens": 256,
        "temperature": 0.2,
    }
    if no_think:
        body["enable_thinking"] = False
    if is_mt:
        body["translation_options"] = {"source_lang": "Russian", "target_lang": "Chinese"}
    t0 = time.time()
    r = requests.post(url, headers=headers, json=body, timeout=60)
    dt = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return None, dt, f"HTTP {r.status_code} {r.text[:120]}"
    return (r.json()["choices"][0]["message"].get("content") or "").strip(), dt, ""


def main() -> int:
    cfg = Config()
    for model, no_think, is_mt in MODELS:
        print("=" * 78)
        print(f"模型: {model}   (thinking={'off' if no_think else '默认'})")
        total = 0
        for i, s in enumerate(SENTS, 1):
            out, dt, err = translate(cfg, model, s, no_think, is_mt)
            total += dt
            print(f"  [{i}] {dt:>5}ms  {out if out else err}")
        print(f"  平均 {total // len(SENTS)}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
