# -*- coding: utf-8 -*-
"""用 App 真实的翻译提示词测模型：质量、延迟、并发是否触发 429。

不打印 API Key。运行：.venv\\Scripts\\python.exe tools\\dev\\probe_translate.py
"""
from __future__ import annotations

import queue
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import Config
from app.translator import Translator

RU = "Итак, производная функции — это скорость изменения функции в данной точке."
RU2 = ("Теорема утверждает, что если функция дифференцируема в точке, "
       "то она непрерывна в этой точке, но обратное неверно.")

MODELS = ["qwen-flash", "qwen-plus", "qwen-max", "qwen-mt-plus", "qwen-long"]


def main() -> int:
    cfg = Config()
    tr = Translator(cfg, queue.Queue(), queue.Queue(), lambda n: [])
    print("model =", cfg.get("translate", "model_name"),
          "| fallback =", cfg.get("translate", "fallback_model_name"))
    print("-" * 78)
    for m in MODELS:
        t0 = time.time()
        try:
            out = tr._call_openai(m, RU)
            dt = int((time.time() - t0) * 1000)
            print(f"OK   {m:<13} {dt:>5}ms  {out!r}")
        except Exception as exc:  # noqa: BLE001
            dt = int((time.time() - t0) * 1000)
            print(f"FAIL {m:<13} {dt:>5}ms  {type(exc).__name__}: {exc}")

    print("-" * 78)
    # 并发 3 条：看会不会触发 429（翻译并发默认 2）
    for m in ("qwen-flash", "qwen-plus"):
        def one(i, model=m):
            try:
                t = time.time()
                tr._call_openai(model, RU2)
                return m, int((time.time() - t) * 1000), "ok"
            except Exception as exc:  # noqa: BLE001
                return m, 0, f"{type(exc).__name__}: {exc}"

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as ex:
            res = list(ex.map(one, range(3)))
        print(f"并发x3 {m:<12} 总耗时 {int((time.time()-t0)*1000)}ms  ->",
              [r[2] for r in res], [r[1] for r in res])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
