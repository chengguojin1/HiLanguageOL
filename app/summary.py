"""M7 课堂要点提炼：课后对整课转写一次性生成（对标大厂会后纪要）。

输出 Markdown，含 5 板块 + 关键词；转写过长按 summary_block_chars 分块后**并行**请求合并。
模型选择：优先 session.summary_model_name（可设更快的 qwen-turbo），未配置则用 translate.model_name。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime

import requests

from .session import Session

SUMMARY_SYSTEM = (
    "你是课堂笔记助手。提取这部分双语转写中的可核实事实，只输出 Markdown 列表。"
    "每条保留时间点（如 00:05:30），覆盖主题、概念/结论、作业、疑问、术语。"
    "不要写标题、开场白或完整纪要；信息不足就省略，绝不编造。"
)

FINAL_SUMMARY_SYSTEM = (
    "你是课堂笔记助手。根据分块事实生成一份去重后的完整课堂纪要，只输出 Markdown。"
    "严格使用且只使用这些二级标题：课程主题、章节要点、关键结论、作业与任务、待查疑问、关键词。"
    "章节要点每条附时间点；信息不足写「本课未涉及」；绝不编造。"
)

MAX_WORKERS = 3  # 并行请求块数（兼顾速度与 API 限流）


def _block_text(session: Session) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    length = 0
    limit = session.cfg.get_int("session", "summary_block_chars")
    for seg in sorted(session.segments, key=lambda s: s.start_ms):
        h, rem = divmod(seg.start_ms, 3600_000)
        m, s = divmod(rem, 60_000)
        ts = f"{h:02d}:{m:02d}:{s:02d}"
        label = "老师" if seg.speaker == "teacher" else "我"
        line = f"[{ts}] [{label}] {seg.ru_text}"
        if seg.zh_text:
            line += f"（{seg.zh_text}）"
        current.append(line)
        length += len(line)
        if length >= limit:
            blocks.append("\n".join(current))
            current, length = [], 0
    if current:
        blocks.append("\n".join(current))
    return blocks


def _pick_model(cfg) -> str:
    """纪要模型：优先 session.summary_model_name，未配置则用翻译模型。"""
    try:
        m = cfg.get("session", "summary_model_name")
        if m and m.strip():
            return m.strip()
    except Exception:
        pass
    return cfg.get("translate", "model_name")


def _summarize_block(cfg, block: str, i: int, n: int, model: str) -> tuple[int, str]:
    """单块纪要请求（含一次失败重试），返回 (块索引, 文本)。"""
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": (
            f"课程名：{cfg.get('session', 'file_prefix')}\n"
            f"以下为转写第 {i}/{n} 部分（[HH:MM:SS] [说话人] 文本）：\n{block}"
        )},
    ]
    engine = cfg.get("translate", "engine")
    base = cfg.translate_base_url().rstrip("/")
    last_exc: Exception | None = None
    for attempt in range(1):  # 不重试，快速失败
        try:
            if engine == "openai":
                headers = {"Content-Type": "application/json"}
                api_key = cfg.translate_api_key()
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                resp = requests.post(
                    f"{base}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "temperature": cfg.get_float("translate", "temperature"),
                        "max_tokens": 1024,
                    },
                    headers=headers,
                    timeout=15,
                )
                if resp.status_code == 404:
                    raise RuntimeError(f"纪要模型不存在或接口路径错误（404）：{model}")
                resp.raise_for_status()
                try:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError(f"纪要响应格式异常: {resp.text[:200]}") from exc
            else:
                resp = requests.post(
                    f"{base}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {"num_predict": 1024,
                                    "temperature": cfg.get_float("translate", "temperature")},
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                text = (resp.json().get("message") or {}).get("content", "").strip()
            if text:
                return i, text
            raise RuntimeError("纪要返回为空")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                import time
                time.sleep(1.5)
                continue
    raise RuntimeError(f"第 {i} 块纪要失败: {last_exc}")


def _synthesize_summary(cfg, evidence: str, model: str) -> str:
    """第二阶段只请求一次：把分块事实合并为没有重复标题的最终纪要。"""
    messages = [
        {"role": "system", "content": FINAL_SUMMARY_SYSTEM},
        {"role": "user", "content": f"以下是按时间排序的课堂事实：\n{evidence}"},
    ]
    base = cfg.translate_base_url().rstrip("/")
    headers = {"Content-Type": "application/json"}
    api_key = cfg.translate_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if cfg.get("translate", "engine") == "openai":
        resp = requests.post(f"{base}/v1/chat/completions", json={
            "model": model, "messages": messages, "stream": False,
            "temperature": cfg.get_float("translate", "temperature"), "max_tokens": 1600,
        }, headers=headers, timeout=25)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    resp = requests.post(f"{base}/api/chat", json={
        "model": model, "messages": messages, "stream": False,
        "options": {"num_predict": 1600, "temperature": cfg.get_float("translate", "temperature")},
    }, timeout=25)
    resp.raise_for_status()
    return (resp.json().get("message") or {}).get("content", "").strip()


def generate_summary(cfg, session: Session) -> str:
    """返回 Markdown 字符串；分块并行请求，单块失败不阻塞其余块。"""
    if not session.segments:
        raise RuntimeError("本课无有效转写，无法生成要点")
    blocks = _block_text(session)
    model = _pick_model(cfg)
    parts = [
        f"# 课堂要点 - {session.name} - {session.start_time.strftime('%Y-%m-%d')}",
        f"> 纪要模型：{model}　|　转写分块：{len(blocks)}　|　并行生成",
        "",
    ]
    results: dict[int, str] = {}
    errors: list[int] = []
    workers = min(MAX_WORKERS, max(1, len(blocks)))
    overall_deadline = 90  # 整体超时 90s，防止某块卡住导致无限等待
    import time as _time
    t0 = _time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_summarize_block, cfg, b, i + 1, len(blocks), model): i
            for i, b in enumerate(blocks)
        }
        pending = set(futures.keys())
        while pending:
            remaining = max(1.0, overall_deadline - (_time.monotonic() - t0))
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                # 整体超时：取消剩余 future，标记失败
                for fut in pending:
                    fut.cancel()
                    i = futures[fut]
                    errors.append(i + 1)
                    results[i] = ""
                break
            for fut in done:
                i = futures[fut]
                try:
                    _, text = fut.result(timeout=2)
                    results[i] = text
                except Exception:  # noqa: BLE001
                    errors.append(i + 1)
                    results[i] = ""
    evidence = "\n\n".join(results[i] for i in range(len(blocks)) if results.get(i))
    if evidence:
        try:
            final_summary = _synthesize_summary(cfg, evidence, model)
            if final_summary:
                parts.append(final_summary)
                parts.append("")
        except Exception as exc:  # 保留已完成的分块事实，不能让纪要完全丢失
            parts.append("## 章节要点\n")
            parts.append(evidence)
            parts.append(f"\n> 汇总失败，以下保留分块事实：{exc}\n")
    if errors:
        parts.append(f"> 注：第 {', '.join(str(e) for e in errors)} 部分生成失败，其余正常。")
        parts.append("")
    if len(parts) <= 3:
        raise RuntimeError("纪要生成结果为空")
    return "\n".join(parts)


def generate_summary_async(cfg, session: Session, done_cb=None) -> threading.Thread:
    """T6 纪要 worker：异步生成并落盘。"""

    def _work() -> None:
        try:
            markdown = generate_summary(cfg, session)
            out = session.dir / "summary.md"
            out.write_text(markdown, encoding="utf-8")
            if done_cb:
                done_cb(True, str(out))
        except Exception as exc:  # noqa: BLE001
            if done_cb:
                done_cb(False, str(exc))

    t = threading.Thread(name="SummaryWorker", target=_work, daemon=True)
    t.start()
    return t
