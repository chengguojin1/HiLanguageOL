"""日志小工具：带大小上限的追加写入。

诊断日志（识别结果、翻译调用、音频错误、结束流程）在长课堂里会持续增长，
实测一节 2 小时的课能写到几十 MB。这里统一做大小轮转：
超过上限就把当前文件改名为 <name>.1（只保留一份历史），再重新开始写。
"""

from __future__ import annotations

from pathlib import Path

MAX_BYTES = 512 * 1024  # 单文件上限 512KB，约等于数万行


def append_log(cfg, name: str, text: str, max_bytes: int = MAX_BYTES) -> None:
    """向 cfg 解析出的日志文件追加一行/一段文本；失败静默（日志不能影响主流程）。"""
    try:
        path = Path(cfg.resolve_path(name))
        _rotate_if_needed(path, max_bytes)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:  # noqa: BLE001
        pass


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        backup = path.with_name(path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
            path.rename(backup)
        except OSError:
            path.write_text("", encoding="utf-8")  # 改名失败就直接清空
    except Exception:  # noqa: BLE001
        pass
