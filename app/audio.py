"""M2 音频采集：WASAPI 回环（老师）+ 麦克风（我）双通道。

技术选型：pyaudiowpatch（PyAudio 的 WASAPI 分支，支持 loopback）。
关键工程约束（踩坑结论）：
  1. portaudio 全局状态非线程安全：多个线程各自创建 PyAudio() 实例会
     触发 0xC0000005 原生崩溃 → 全进程共享单个实例（_get_pa），
     枚举用锁保护。
  2. 回环流必须使用设备默认采样率（WASAPI 混音格式）。
  3. 无音频播放时回环 read() 会阻塞 → 轮询 read_available 而非阻塞读，
     保证 stop() 可及时返回。
  4. pyaudiowpatch 设备索引跨枚举不稳定 → 一律按名称匹配，不缓存索引。

通道策略（自动、灵活）：
  - 配置的设备名不存在时自动回退到「默认输出对应的回环 / 默认麦克风」，
    并把回退原因写进状态栏，不再静默失败；
  - 某个通道的设备打不开（被拔掉/被占用/没有该设备）不影响另一个通道，
    后台每隔 device_retry_seconds 秒重试，插上麦克风后自动恢复；
  - 每通道滚动检测「有没有人说话」（差分 RMS，对直流和 20Hz 之类低频
    噪声不敏感），老师静音 / 我静音 / 双通道有声音都能实时看出来。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .logs import append_log

TARGET_ASR_RATE = 16000
CHUNK_MS = 100  # 采集块粒度（毫秒）

# ---- 语音活动检测阈值（归一化差分 RMS，见 _diff_level）----
# 实测标定：俄语 TTS/课堂人声 1 秒窗口 ≥0.02；ToDesk 虚拟麦 20Hz 噪声 ≈0.0013~0.0055；
# 全零静音 = 0。因此 0.015/0.006 能干净区分"有人在说话"和"设备在冒噪声"。
SPEECH_LEVEL = 0.015
SILENCE_LEVEL = 0.006
ACTIVITY_WINDOW_S = 4.0

# ---- 共享 PyAudio 实例（全进程唯一，锁保护） ----
_pa_instance = None
_pa_lock = threading.RLock()  # 可重入：default_loopback_name 内会再调 _get_pa

CHANNEL_LABEL = {"teacher": "老师", "me": "我"}


def _get_pa():
    """进程内共享的 pyaudiowpatch 实例（首次创建后复用）。"""
    global _pa_instance
    with _pa_lock:
        if _pa_instance is None:
            import pyaudiowpatch as pyaudio

            _pa_instance = pyaudio.PyAudio()
        return _pa_instance


def _pa_format():
    return 1  # paFloat32


@dataclass
class AudioChunk:
    channel: str  # "teacher" | "me"
    samples: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # float32 16k mono
    raw_samples: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # float32 native mono
    sample_rate: int = 0  # raw 采样率
    pts_ms: int = 0  # 采集时间戳（毫秒）
    captured_at_ms: int = 0  # 单调时钟时间轴，用于双设备录音对齐
    message: str = ""  # 状态/错误信息（正常为空）


def _resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性重采样（语音识别对精度不敏感，成本低）。"""
    if src_rate == dst_rate or x.size == 0:
        return x
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, np.float32)
    src_idx = np.linspace(0, x.size - 1, n_out)
    return np.interp(src_idx, np.arange(x.size), x).astype(np.float32)


def _mono(data: bytes | np.ndarray, channels: int) -> np.ndarray:
    frame = np.frombuffer(data, dtype=np.float32) if isinstance(data, (bytes, bytearray)) \
        else np.asarray(data, dtype=np.float32)
    if channels > 1:
        frame = frame.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return frame.reshape(-1)


def _diff_level(x: np.ndarray, sr: int) -> float:
    """归一化差分 RMS：近似"语音频段能量"，对直流/低频漂移不敏感。

    一阶差分相当于高通，幅度与采样率成正比，故按 16k 归一化，
    这样不同设备采样率（44.1k/48k/16k）的阈值可以统一。
    """
    if x.size < 2 or sr <= 0:
        return 0.0
    d = np.diff(np.asarray(x, dtype=np.float64))
    return float(np.sqrt(np.mean(d * d)) * sr / TARGET_ASR_RATE)


class SpeechActivity:
    """滚动判断"最近有没有人说话"。"""

    def __init__(self, window_s: float = ACTIVITY_WINDOW_S) -> None:
        self._window_s = window_s
        self._buf = np.zeros(0, np.float32)
        self._sr = TARGET_ASR_RATE

    def feed(self, samples: np.ndarray, sr: int) -> None:
        if samples.size == 0:
            return
        self._sr = sr or self._sr
        self._buf = samples.astype(np.float32, copy=True) if self._buf.size == 0 \
            else np.concatenate([self._buf, samples])
        keep = max(1, int(self._sr * self._window_s))
        if self._buf.size > keep:
            self._buf = self._buf[-keep:]

    def levels(self) -> tuple[float, float]:
        """(最近 1 秒电平, 整个窗口电平)"""
        if self._buf.size < 2:
            return 0.0, 0.0
        sr = self._sr or TARGET_ASR_RATE
        recent = self._buf[-sr:] if self._buf.size > sr else self._buf
        return _diff_level(recent, sr), _diff_level(self._buf, sr)

    def state(self) -> str:
        now_lv, win_lv = self.levels()
        if now_lv >= SPEECH_LEVEL:
            return "signal"
        if win_lv >= SILENCE_LEVEL:
            return "weak"
        return "silent"

    @property
    def window_level(self) -> float:
        return self.levels()[1]


@dataclass
class ChannelHealth:
    """单个通道的运行健康状态（供 UI 显示与自动判断）。"""

    channel: str
    device: str = ""
    opened: bool = False
    note: str = ""       # 自动切换/回退说明
    error: str = ""      # 最近一次失败原因（空=正常）
    retries: int = 0
    state: str = "unknown"   # signal | weak | silent | unknown
    level: float = 0.0
    last_signal_ts: float = 0.0

    @property
    def label(self) -> str:
        return CHANNEL_LABEL.get(self.channel, self.channel)

    @property
    def state_text(self) -> str:
        if not self.opened:
            return "未就绪"
        return {"signal": "有声音", "weak": "声音弱", "silent": "静音"}.get(self.state, "未知")

    def summary(self) -> str:
        if not self.opened:
            return f"{self.label} {self.error or '未就绪'}"
        return f"{self.label} {self.state_text}"

    def copy(self) -> "ChannelHealth":
        return ChannelHealth(**self.__dict__)


def _wasapi_index(kind: str) -> int:
    import pyaudiowpatch as pyaudio

    pa = _get_pa()
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    return int(wasapi[kind])


def _default_input_name() -> str:
    """WASAPI 默认输入设备名（尽力解析，失败空串）。"""
    try:
        pa = _get_pa()
        return str(pa.get_device_info_by_index(_wasapi_index("defaultInputDevice"))["name"])
    except Exception:
        return ""


def _default_output_name() -> str:
    try:
        pa = _get_pa()
        return str(pa.get_device_info_by_index(_wasapi_index("defaultOutputDevice"))["name"])
    except Exception:
        return ""


def _is_loopback(info: dict) -> bool:
    name = str(info.get("name", "")).lower()
    return bool(info.get("isLoopbackDevice")) or "loopback" in name or "[loopback]" in name


def _is_aggregate(info: dict) -> bool:
    name = str(info.get("name", ""))
    return "声音映射器" in name or "主声音" in name


def _devices(pa) -> list[dict]:
    out = []
    for i in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(i)
        except Exception:
            continue
        out.append({**info, "index": i})
    return out


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _loopback_candidates() -> list[dict]:
    with _pa_lock:
        pa = _get_pa()
        devs = [d for d in _devices(pa)
                if _is_loopback(d) and d.get("maxInputChannels", 0) > 0]
    return devs


def _mic_candidates() -> list[dict]:
    """麦克风候选：优先 WASAPI。

    同一支麦克风会同时出现在 MME / DirectSound / WASAPI 三个主机 API 下，
    名字完全相同。以前的实现按名字取第一个，往往拿到 MME 的 44.1kHz 立体声
    端点——蓝牙耳机（HFP 麦克风只有 16kHz 单声道）走这条路经常采不到声音，
    或者被系统静音。WASAPI 是 Windows 原生路径，也和后端回环采集一致。
    """
    with _pa_lock:
        pa = _get_pa()
        devs = [d for d in _devices(pa)
                if not _is_loopback(d)
                and d.get("maxInputChannels", 0) > 0
                and not _is_aggregate(d)]
        wasapi = []
        for d in devs:
            try:
                api = pa.get_host_api_info_by_index(int(d.get("hostApi", -1)))
                if "wasapi" in str(api.get("name", "")).lower():
                    wasapi.append(d)
            except Exception:  # noqa: BLE001
                continue
    return wasapi or devs


def list_loopback_devices() -> list[str]:
    try:
        return _dedupe([d["name"] for d in _loopback_candidates()])
    except Exception:
        return []


def list_mic_devices() -> list[str]:
    try:
        return _dedupe([d["name"] for d in _mic_candidates()])
    except Exception:
        return []


def default_mic_name() -> str:
    return _default_input_name()


def default_loopback_name() -> str:
    """默认输出设备对应的回环设备名（老师声音实际来源）。

    匹配必须严格：WASAPI 回环名就是 "<输出设备名> [Loopback]"。
    旧实现里有一句宽松匹配（out_name.split(" (")[0] in nm），
    会把任意"扬声器 …"都算命中，导致多声卡机器上永远选中设备表里
    排在前面的那个回环（例如 ToDesk），默认设备切换后跟不过去。
    """
    try:
        out_name = _default_output_name()
        if not out_name:
            return ""
        with _pa_lock:
            pa = _get_pa()
            loops = [d["name"] for d in _devices(pa) if _is_loopback(d)]
        target = out_name + " [Loopback]"
        if target in loops:  # 1) 精确匹配
            return target
        base = out_name.split(" (")[0]
        exact_base = [nm for nm in loops if nm.split(" (")[0] == base]
        if len(exact_base) == 1:  # 2) 去掉括号说明后唯一匹配才采用
            return exact_base[0]
        return ""
    except Exception:
        return ""


def _match(candidates: list[dict], wanted: str) -> dict | None:
    if not wanted:
        return None
    for d in candidates:
        if d["name"] == wanted:
            return d
    low = wanted.lower()
    for d in candidates:
        if low in str(d["name"]).lower():
            return d
    return None


def resolve_capture_devices(cfg, channels=("teacher", "me")) -> tuple[dict, dict, dict]:
    """同步解析各通道要用的设备（不打开流）。

    返回 (devices, notes, errors)：
      devices[ch] = 设备 dict 或 None；notes[ch] = 自动回退说明；errors[ch] = 不可用原因。
    """
    devices: dict[str, dict | None] = {}
    notes: dict[str, str] = {}
    errors: dict[str, str] = {}
    for ch in channels:
        try:
            if ch == "teacher":
                cands = _loopback_candidates()
                wanted = (cfg.get("audio", "loopback_device") or "").strip()
                prefer = default_loopback_name()
                kind = "回环（老师声音）"
            else:
                cands = _mic_candidates()
                wanted = (cfg.get("audio", "mic_device") or "").strip()
                prefer = default_mic_name()
                kind = "麦克风（我的声音）"
            if not cands:
                devices[ch] = None
                errors[ch] = f"未发现任何{kind}设备"
                continue
            if wanted and wanted.lower() != "auto":
                hit = _match(cands, wanted)
                if hit is not None:
                    devices[ch] = hit
                    continue
                note = f"配置的{kind}设备不存在（{wanted}）"
            else:
                note = ""
            fallback = _match(cands, prefer) if prefer else None
            if fallback is None:
                fallback = cands[0]
            devices[ch] = fallback
            notes[ch] = (note + f"，已自动改用 {fallback['name']}").lstrip("，")
        except Exception as exc:  # noqa: BLE001
            devices[ch] = None
            errors[ch] = f"设备枚举失败: {exc}"
    return devices, notes, errors


def capture_channels(cfg) -> tuple[str, ...]:
    """按 [audio] capture_mode 决定启用哪些通道。auto/both=双通道（谁有声音翻谁）。"""
    mode = (cfg.get("audio", "capture_mode") or "auto").strip().lower()
    if mode in ("teacher", "teacher_only", "loopback"):
        return ("teacher",)
    if mode in ("me", "mic", "microphone"):
        return ("me",)
    return ("teacher", "me")


class AudioCapture:
    """多通道采集器：start() 后每个通道一个线程持续向 out_q 推送 AudioChunk。

    各采集线程共享同一个 PyAudio 实例（portaudio 非线程安全）。
    设备缺失/打开失败只影响该通道，并在后台自动重试，不会拖垮其它通道。
    """

    def __init__(self, cfg, out_q: "queue.Queue[AudioChunk]", rec_q: "queue.Queue[AudioChunk]",
                 channels=None):
        self._cfg = cfg
        self._out_q = out_q
        self._rec_q = rec_q
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._pause_evt.set()  # 默认不暂停
        self._threads: list[threading.Thread] = []
        self.dropped_audio = 0
        self.dropped_recording = 0
        self._drop_lock = threading.Lock()
        self._channels = tuple(channels) if channels else capture_channels(cfg)
        self._health: dict[str, ChannelHealth] = {
            ch: ChannelHealth(channel=ch) for ch in self._channels}
        self._health_lock = threading.Lock()
        self._last_msg: dict[str, str] = {}  # 去重：同一条状态消息不重复刷屏
        self._activity: dict[str, SpeechActivity] = {
            ch: SpeechActivity() for ch in self._channels}

    # ---- 生命周期 ----
    @property
    def channels(self) -> tuple[str, ...]:
        return self._channels

    def start(self) -> None:
        devices, notes, errors = resolve_capture_devices(self._cfg, self._channels)
        for ch in self._channels:
            dev = devices.get(ch)
            with self._health_lock:
                h = self._health[ch]
                h.device = (dev or {}).get("name", "")
                h.note = notes.get(ch, "")
                h.error = errors.get(ch, "")
            if dev is None:
                self._push_message(ch, f"{CHANNEL_LABEL.get(ch, ch)}通道不可用：{h.error}")
            else:
                self._push_message(
                    ch, f"{CHANNEL_LABEL.get(ch, ch)}通道：{h.device}"
                    + (f"（{h.note}）" if h.note else ""))
            t = threading.Thread(target=self._capture_loop, args=(ch,),
                                 name=f"Capture-{ch}", daemon=True)
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._pause_evt.set()  # 解除暂停，让线程能退出
        for t in self._threads:
            t.join(timeout=1.5)

    def pause(self) -> None:
        self._pause_evt.clear()

    def resume(self) -> None:
        self._pause_evt.set()

    def snapshot(self) -> dict[str, ChannelHealth]:
        with self._health_lock:
            return {ch: h.copy() for ch, h in self._health.items()}

    def status_text(self) -> str:
        snap = self.snapshot()
        parts = [snap[ch].summary() for ch in self._channels if ch in snap]
        text = "　|　".join(parts)
        dropped = self.dropped_audio + self.dropped_recording
        if dropped:
            text += f"　|　丢弃音频块 {dropped}"
        return text

    # ---- 内部 ----
    def _push_message(self, channel: str, message: str) -> None:
        with self._health_lock:
            if self._last_msg.get(channel) == message:
                return  # 同一条消息连续重复（例如设备一直不可用）不重复推送
            self._last_msg[channel] = message
        self._offer_latest(self._out_q, AudioChunk(channel=channel, message=message), "audio")

    def _set_health(self, channel: str, **kw) -> None:
        with self._health_lock:
            h = self._health.get(channel)
            if h is None:
                return
            for k, v in kw.items():
                setattr(h, k, v)

    def _resolve_channel(self, channel: str) -> tuple[dict | None, str, str]:
        devices, notes, errors = resolve_capture_devices(self._cfg, (channel,))
        return devices.get(channel), notes.get(channel, ""), errors.get(channel, "")

    def _is_auto(self, channel: str) -> bool:
        key = "loopback_device" if channel == "teacher" else "mic_device"
        val = (self._cfg.get("audio", key) or "").strip().lower()
        return val in ("", "auto")

    def _open_stream(self, device: dict):
        with _pa_lock:  # 枚举+打开共享实例，串行化避免 portaudio 全局状态冲突
            rate = int(device.get("defaultSampleRate", 48000))
            ch_choices = [max(1, int(device.get("maxInputChannels", 2))), 2, 1]
            ch_choices = list(dict.fromkeys(ch_choices))  # 去重保序
            last_err: Exception | None = None
            for ch in ch_choices:
                try:
                    stream = _get_pa().open(
                        format=_pa_format(),
                        channels=ch,
                        rate=rate,
                        input=True,
                        input_device_index=device["index"],
                        frames_per_buffer=max(1, int(rate * CHUNK_MS / 1000)),
                    )
                    return stream, rate, ch
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
            raise RuntimeError(f"打开失败: {last_err}")

    def _capture_loop(self, channel: str) -> None:
        label = CHANNEL_LABEL.get(channel, channel)
        retry_s = max(2, self._cfg.get_int("audio", "device_retry_seconds"))
        while not self._stop_evt.is_set():
            device, note, err = self._resolve_channel(channel)
            if device is None:
                self._set_health(channel, opened=False, error=err, note=note, state="unknown")
                self._push_message(channel, f"{label}通道不可用：{err}")
                if self._stop_evt.wait(retry_s):
                    return
                continue
            stream = None
            try:
                stream, rate, opened_ch = self._open_stream(device)
            except Exception as exc:  # noqa: BLE001
                snap = self.snapshot().get(channel)
                retries = (snap.retries if snap else 0) + 1
                self._set_health(channel, opened=False, error=f"打开失败: {exc}",
                                 note=note, retries=retries, state="unknown")
                self._push_message(channel, f"{label}通道打开失败（第 {retries} 次，{retry_s}s 后重试）：{exc}")
                self._log_error(channel, exc)
                if self._stop_evt.wait(retry_s):
                    return
                continue
            self._set_health(channel, opened=True, error="", note=note,
                             device=device["name"], state="silent")
            self._push_message(channel, f"{label}通道已就绪：{device['name']}"
                                        f"（{rate}Hz/{opened_ch}ch）")
            reason = "stopped"
            try:
                reason = self._stream_loop(stream, channel, rate, opened_ch, device["name"],
                                           retry_stall_s=float(retry_s))
            except Exception as exc:  # noqa: BLE001
                self._log_error(channel, exc)
                self._push_message(channel, f"{label}通道读取中断：{exc}")
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
            if self._stop_evt.is_set():
                return
            if reason == "switch":
                # 系统默认设备变了（auto 模式）：立刻重新解析并打开新设备，不等待
                self._set_health(channel, opened=False, state="unknown")
                continue
            if reason == "stall":
                # 数据流卡住：稍等后重开（设备开始播放声音时就能重新接上）
                self._set_health(channel, opened=False, state="unknown")
                if self._stop_evt.wait(1.0):
                    return
                continue
            self._push_message(channel, f"{label}通道数据流已结束，{retry_s}s 后重启")
            if self._stop_evt.wait(retry_s):
                return

    def _stream_loop(self, stream, channel: str, rate: int, channels: int,
                     device_name: str = "", retry_stall_s: float = 10.0) -> str:
        """返回结束原因："stopped"（停止/异常）/"switch"（默认设备已切换）/"stall"（数据流卡住）。

        注意：WASAPI 回环在"该输出设备当前没有任何声音"时可能完全不再交付数据
        （实测 ToDesk 虚拟声卡就是这样），所以除了跟随默认设备，还需要一个
        看门狗：长时间读不到数据就重开设备，等设备一开始播放声音就能接上。
        """
        frames = max(1, int(rate * CHUNK_MS / 1000))
        base_ms = 0
        follow = self._is_auto(channel)          # auto 模式才跟随默认设备
        next_check = time.monotonic() + 5.0      # 每 5 秒检查一次默认设备是否变化
        last_data = time.monotonic()             # 看门狗：多久没读到数据
        stall_timeout = max(5.0, retry_stall_s)
        stream_started_ms = int(time.monotonic() * 1000)
        activity = self._activity.get(channel) or SpeechActivity()
        last_signal = 0.0
        while not self._stop_evt.is_set():
            # 暂停：继续读但丢弃（保证声卡缓冲不堆积，恢复后不喷旧声音）
            paused = not self._pause_evt.is_set()
            now_m = time.monotonic()
            # auto 模式：跟随系统默认输入/输出设备的变化（中途换设备/插耳机）
            if follow and device_name and now_m >= next_check:
                next_check = now_m + 5.0
                new_dev, _note, _err = self._resolve_channel(channel)
                new_name = (new_dev or {}).get("name", "")
                if new_name and new_name != device_name:
                    self._push_message(
                        channel, f"检测到默认设备已切换为 {new_name}，正在自动跟随")
                    return "switch"
            if now_m - last_data > stall_timeout:
                self._push_message(channel, f"{CHANNEL_LABEL.get(channel, channel)}通道"
                                            f"{int(now_m - last_data)}秒没有数据，重新打开设备")
                return "stall"
            # 轮询可读量：回环在无播放时可能没有数据，不能阻塞 read
            try:
                avail = stream.get_read_available()
            except Exception:
                avail = frames
            if avail < frames:
                self._stop_evt.wait(0.02 if paused else 0.05)
                continue
            last_data = time.monotonic()
            try:
                data = stream.read(frames, exception_on_overflow=False)
            except Exception:
                continue
            raw = _mono(data, channels)
            if raw.size == 0:
                continue
            pts = base_ms + int(len(raw) * 1000 / rate)
            base_ms = pts
            if paused:
                continue
            # 语音活动检测（只影响状态显示与提示，不影响是否送识别）
            activity.feed(raw, rate)
            state = activity.state()
            if state == "signal":
                last_signal = time.monotonic()
            self._set_health(channel, state=state, level=round(activity.window_level, 5),
                             last_signal_ts=last_signal)
            chunk = AudioChunk(
                channel=channel,
                samples=_resample(raw, rate, TARGET_ASR_RATE),
                raw_samples=raw,
                sample_rate=rate,
                pts_ms=pts,
                captured_at_ms=stream_started_ms + pts,
            )
            self._offer_latest(self._out_q, chunk, "audio")
            self._offer_latest(self._rec_q, chunk, "recording")
        return "stopped"

    def _log_error(self, channel: str, exc: Exception) -> None:
        import traceback
        append_log(self._cfg, "audio_error.log",
                   f"[{time.strftime('%H:%M:%S')}] {channel} 通道异常: {exc}\n"
                   f"{traceback.format_exc()}\n")

    def _offer_latest(self, target: queue.Queue, chunk: AudioChunk, kind: str) -> None:
        """队列满时淘汰最旧音频，绝不让声卡读取线程无限阻塞。"""
        try:
            target.put_nowait(chunk)
            return
        except queue.Full:
            pass
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(chunk)
        except queue.Full:
            pass
        with self._drop_lock:
            if kind == "audio":
                self.dropped_audio += 1
            else:
                self.dropped_recording += 1


