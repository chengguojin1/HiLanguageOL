"""M6 会话与文件：录音落盘、SRT/VTT/TXT 导出、会话列表。

录音采用 soundfile 双通道独立 WAV（16bit PCM），每 flush_interval 秒 flush 一次，
崩溃时已落盘分段可恢复。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import AudioChunk
from .asr_ws import TranscriptSegment


def _fmt_ms(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_ = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms_:03d}"


def _fmt_vtt(ms: int) -> str:
    return _fmt_ms(ms).replace(",", ".")


def _speaker_label(speaker: str) -> str:
    return "老师" if speaker == "teacher" else "我"


def build_srt(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(sorted(segments, key=lambda s: s.start_ms), 1):
        body = f"[{_speaker_label(seg.speaker)}] {seg.ru_text}"
        if seg.zh_text:
            body += f"\n[{_speaker_label(seg.speaker)}] {seg.zh_text}"
        lines.append(f"{i}\n{_fmt_ms(seg.start_ms)} --> {_fmt_ms(seg.end_ms)}\n{body}\n")
    return "\n".join(lines)


def build_vtt(segments: list[TranscriptSegment]) -> str:
    lines = ["WEBVTT", ""]
    for seg in sorted(segments, key=lambda s: s.start_ms):
        body = f"[{_speaker_label(seg.speaker)}] {seg.ru_text}"
        if seg.zh_text:
            body += f"\n[{_speaker_label(seg.speaker)}] {seg.zh_text}"
        lines.append(f"{_fmt_vtt(seg.start_ms)} --> {_fmt_vtt(seg.end_ms)}\n{body}\n")
    return "\n".join(lines)


def build_txt(segments: list[TranscriptSegment], with_translation: bool = True) -> str:
    lines: list[str] = []
    for seg in sorted(segments, key=lambda s: s.start_ms):
        h, rem = divmod(seg.start_ms, 3600_000)
        m, rem2 = divmod(rem, 60_000)
        s, _ = divmod(rem2, 1000)
        ts = f"{h:02d}:{m:02d}:{s:02d}"
        label = _speaker_label(seg.speaker)
        lines.append(f"[{ts}] [{label}] {seg.ru_text}")
        if with_translation and seg.zh_text:
            lines.append(f"[{ts}] [{label}] {seg.zh_text}")
    return "\n".join(lines)


@dataclass
class Session:
    cfg: object = field(repr=False)
    name: str = ""
    id: str = field(default="")
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None
    segments: list[TranscriptSegment] = field(default_factory=list)
    dir: Path | None = None
    _writers: dict = field(default_factory=dict, repr=False)
    _writer_rates: dict[str, int] = field(default_factory=dict, repr=False)
    _writer_starts_ms: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.start_time.strftime("%Y%m%d_%H%M%S")
        if not self.name:
            self.name = self.id
        root = self.cfg.resolve_path(self.cfg.get("session", "output_dir"))
        prefix = self.cfg.get("session", "file_prefix")
        self.dir = root / f"{prefix}_{self.name}_{self.id}"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 录音（两路临时文件，结束时合并为单文件立体声）----
    def open_recording(self, channel: str, sample_rate: int, captured_at_ms: int) -> None:
        if channel in self._writers:
            return
        path = self.dir / f"rec_tmp_{channel}.wav"
        self._writers[channel] = sf.SoundFile(
            str(path), mode="w", samplerate=sample_rate,
            channels=1, subtype="PCM_16",
        )
        self._writer_rates[channel] = sample_rate
        self._writer_starts_ms[channel] = captured_at_ms

    def write_audio(self, channel: str, samples: np.ndarray) -> None:
        writer = self._writers.get(channel)
        if writer is not None and samples.size:
            writer.write(samples)

    def flush(self) -> None:
        for writer in self._writers.values():
            try:
                writer.flush()
            except Exception:
                pass

    # ---- 转写 ----
    def add_segment(self, seg: TranscriptSegment) -> None:
        self.segments.append(seg)
        # 翻译确定的文本实时追加写入文件（防崩溃丢数据）
        if seg.status == "translated" and seg.ru_text and seg.zh_text:
            try:
                txt_path = self.dir / "transcript_live.txt"
                minutes = seg.start_ms // 60000
                seconds = (seg.start_ms % 60000) // 1000
                speaker = "老师" if seg.speaker == "teacher" else "我"
                line = f"[{minutes:02d}:{seconds:02d}] [{speaker}] {seg.ru_text}\n{seg.zh_text}\n\n"
                with open(txt_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    def get_context(self, n: int) -> list[tuple[str, str]]:
        """取最近 n 句已翻译的俄中对照（作为翻译上下文）。"""
        pairs = []
        for seg in self.segments:
            if seg.ru_text and seg.zh_text:
                pairs.append((seg.ru_text, seg.zh_text))
        return pairs[-n:]

    # ---- 结束与导出 ----
    def finalize(self) -> dict:
        self.end_time = datetime.now()
        for writer in self._writers.values():
            writer.close()
        self._writers = {}
        srt = self.dir / "transcript.srt"
        vtt = self.dir / "transcript.vtt"
        txt = self.dir / "transcript.txt"
        srt.write_text(build_srt(self.segments), encoding="utf-8")
        vtt.write_text(build_vtt(self.segments), encoding="utf-8")
        txt.write_text(build_txt(self.segments), encoding="utf-8")
        rec_path = self.dir / "rec.wav"
        self._merge_recordings(rec_path)
        return {
            "rec": str(rec_path),
            "srt": str(srt), "vtt": str(vtt), "txt": str(txt),
            "summary": str(self.dir / "summary.md"),
        }

    def _merge_recordings(self, out_path: "pathlib.Path") -> None:
        """两路临时录音 -> 单声道混音单文件（像会议软件一样一路声音），删除临时文件。"""
        paths = {ch: self.dir / f"rec_tmp_{ch}.wav" for ch in ("teacher", "me")}
        existing = {ch: p for ch, p in paths.items()
                    if p.exists() and p.stat().st_size > 44}
        if not existing:
            return
        # 统一到最高原生采样率，并按采集单调时钟补齐起始偏移。
        # 避免蓝牙/HFP 与回环设备采样率不同造成播放变速或两路错位。
        sr = max((self._writer_rates.get(ch, 0) for ch in existing), default=48000)
        handles = {ch: sf.SoundFile(str(p)) for ch, p in existing.items()}
        earliest = min(self._writer_starts_ms.get(ch, 0) for ch in existing)
        remaining_silence = {
            ch: max(0, round((self._writer_starts_ms.get(ch, earliest) - earliest) * sr / 1000))
            for ch in existing
        }

        def read_at_rate(channel: str, frames: int) -> np.ndarray:
            silence = remaining_silence[channel]
            if silence:
                n = min(frames, silence)
                remaining_silence[channel] -= n
                return np.zeros(n, dtype=np.int16)
            source = handles[channel]
            raw = source.read(frames if source.samplerate == sr else max(1, round(frames * source.samplerate / sr)), dtype="float32")
            if raw.size == 0:
                return np.array([], dtype=np.int16)
            if source.samplerate != sr:
                n = max(1, round(len(raw) * sr / source.samplerate))
                raw = np.interp(np.linspace(0, len(raw) - 1, n), np.arange(len(raw)), raw)
            return np.clip(raw * 32767, -32768, 32767).astype(np.int16)
        try:
            with sf.SoundFile(str(out_path), mode="w", samplerate=sr,
                              channels=1, subtype="PCM_16") as out:
                while True:
                    left = read_at_rate("teacher", 65536) if "teacher" in handles else np.array([], dtype="int16")
                    right = read_at_rate("me", 65536) if "me" in handles else np.array([], dtype="int16")
                    n = max(len(left), len(right))
                    if n == 0:
                        break
                    mix = np.zeros(n, dtype=np.int32)
                    if len(left):
                        mix[: len(left)] += left.astype(np.int32)
                    if len(right):
                        mix[: len(right)] += right.astype(np.int32)
                    np.clip(mix, -32768, 32767, out=mix)
                    out.write(mix.astype(np.int16))
        finally:
            for h in handles.values():
                h.close()
        for p in paths.values():
            try:
                p.unlink()
            except OSError:
                pass


class Recorder(threading.Thread):
    """T5 录音 writer：消费 rec_q 旁路音频写盘。"""

    def __init__(self, cfg, rec_q: "queue.Queue[AudioChunk]", session: Session):
        super().__init__(name="Recorder", daemon=True)
        self._cfg = cfg
        self._rec_q = rec_q
        self._session = session
        self._stop_evt = threading.Event()
        self._last_flush = time.monotonic()

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(timeout=1)

    def run(self) -> None:
        flush_interval = self._cfg.get_int("session", "flush_interval")
        while not self._stop_evt.is_set():
            try:
                chunk = self._rec_q.get(timeout=0.5)
            except queue.Empty:
                chunk = None
            if chunk is not None and chunk.raw_samples.size:
                self._session.open_recording(chunk.channel, chunk.sample_rate, chunk.captured_at_ms)
                self._session.write_audio(chunk.channel, chunk.raw_samples)
            now = time.monotonic()
            if now - self._last_flush >= flush_interval:
                self._session.flush()
                self._last_flush = now
