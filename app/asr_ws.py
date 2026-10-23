"""M3 识别链路：阿里云百炼 WebSocket 流式实时语音识别。

协议流程（每个通道独立一条 WebSocket 连接）：
  1. 建立 wss 连接（握手 Header 带 Authorization: Bearer {api_key}）
  2. 发送 run-task（JSON 文本帧）：指定模型、format、sample_rate、language_hints
  3. 等待服务端 task-started 事件
  4. 持续发送音频二进制帧（PCM 16-bit 小端，每 frame_ms 一帧）
  5. 接收 transcription 事件：is_final=true 时输出成句识别结果
  6. 音频结束后发送 finish-task，等待 task-finished
  7. 关闭连接

双通道：老师（loopback）和我（mic）各一条 WebSocket 连接，共享 out_q。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass

import numpy as np

from .audio import AudioChunk
from .logs import append_log

try:
    import websocket
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

# 长句渐进翻译的切分边界
STRONG_BOUNDARY = "。！？!?;；.…"   # 句末/分句：可以放心先把这段交出去翻
WEAK_BOUNDARY = "，,、:： "          # 没有句末标点时，退而求其次（含空格，避免切断单词）


@dataclass
class TranscriptSegment:
    seq: int
    speaker: str          # "teacher" | "me"
    start_ms: int
    end_ms: int
    ru_text: str
    zh_text: str = ""
    status: str = "recognized"
    confidence: float = 0.0
    corrected_ru: str = ""


class ASRChannelWorker(threading.Thread):
    """单通道 WebSocket 流式 ASR（老师或我）。"""

    def __init__(self, cfg, channel: str, in_q: "queue.Queue[AudioChunk]",
                 out_q: "queue.Queue[TranscriptSegment]",
                 status_cb=None, ready_cb=None, seq_lock=None, seq_ref=None,
                 preview_q=None, timeline_origin_ms: int = 0):
        super().__init__(name=f"ASR-{channel}", daemon=True)
        self._cfg = cfg
        self._channel = channel
        self._in_q = in_q
        self._out_q = out_q
        self._preview_q = preview_q
        self._timeline_origin_ms = timeline_origin_ms
        self._status_cb = status_cb or (lambda msg: None)
        self._ready_cb = ready_cb or (lambda ok: None)
        self._seq_lock = seq_lock
        self._seq_ref = seq_ref  # list[int] 共享序号
        self._stop_evt = threading.Event()
        self._ws = None
        self._ws_connected = threading.Event()
        self._task_started = threading.Event()
        self._task_finished = threading.Event()
        self._buffer = np.zeros(0, np.float32)
        # 长句渐进式翻译所需状态：当前句 id / 当前句已识别文本 / 已切出去翻译的字符数
        self._sentence_id = None
        self._sentence_text = ""
        self._committed = 0
        self._chunk_stream = cfg.get_bool("asr", "chunk_stream")
        self._chunk_max_chars = max(40, cfg.get_int("asr", "chunk_max_chars"))
        self._chunk_min_chars = max(10, cfg.get_int("asr", "chunk_min_chars"))

    # ---- 连接 ----
    def _connect(self) -> bool:
        if not _WS_AVAILABLE:
            self._status_cb("缺少 websocket-client 库，请运行 pip install websocket-client")
            self._ready_cb(False)
            return False
        # 识别服务独立的 Key（未单独配置时自动回退到翻译服务的 Key）
        api_key = self._cfg.asr_api_key()
        if not api_key:
            self._status_cb("语音识别需要 API Key（设置 →「AI 模型」→ 识别服务 API Key）")
            self._ready_cb(False)
            return False
        ws_url = self._cfg.asr_base_url()
        if not ws_url:
            self._status_cb("语音识别服务地址为空（设置 →「AI 模型」→ 识别服务地址）")
            self._ready_cb(False)
            return False
        model = self._cfg.get("asr", "model")
        sample_rate = self._cfg.get_int("asr", "sample_rate")
        fmt = self._cfg.get("asr", "format")
        language = self._cfg.source_lang()
        max_silence = self._cfg.get_int("asr", "max_sentence_silence")

        def on_open(ws):
            self._ws_connected.set()
            run_task = {
                "header": {
                    "action": "run-task",
                    "task_id": str(uuid.uuid4()),
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": model,
                    "input": {},
                    "parameters": {
                        "format": fmt,
                        "sample_rate": sample_rate,
                        "language_hints": [language],
                        "max_sentence_silence": max_silence,
                    },
                },
            }
            ws.send(json.dumps(run_task))

        def on_message(ws, message):
            self._handle_message(message)

        def on_error(ws, error):
            self._status_cb(f"ASR WebSocket 错误（{self._channel}）: {error}")

        def on_close(ws, code, msg):
            self._ws_connected.clear()
            self._task_started.clear()
            if code and code != 1000:
                try:
                    with open(self._cfg.resolve_path("asr_close.log"), "a", encoding="utf-8") as _f:
                        _f.write(f"[{time.strftime('%H:%M:%S')}] {self._channel} closed: code={code}, msg={msg}\n")
                except Exception:
                    pass

        try:
            self._ws = websocket.WebSocketApp(
                ws_url,
                header={"Authorization": f"Bearer {api_key}"},
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            # 心跳：长课堂（数小时）经 NAT/代理时防止被静默断开
            heartbeat = self._cfg.get_int("asr", "heartbeat_sec")
            wst = threading.Thread(
                target=self._ws.run_forever,
                kwargs={"ping_interval": heartbeat if heartbeat > 0 else 0},
                daemon=True)
            wst.start()
            # 等待 task-started（超时可配，默认 10 秒）
            if not self._task_started.wait(timeout=self._cfg.get_int("asr", "timeout_sec")):
                self._status_cb(f"ASR 连接超时（{self._channel}）")
                self._ready_cb(False)
                return False
            self._ready_cb(True)
            return True
        except Exception as exc:
            self._status_cb(f"ASR 连接失败（{self._channel}）: {exc}")
            self._ready_cb(False)
            return False

    def _handle_message(self, message):
        try:
            data = json.loads(message)
            event = data.get("header", {}).get("event", "")
            if event == "task-started":
                self._task_started.set()
            elif event == "result-generated":
                output = data.get("payload", {}).get("output", {})
                sentence = output.get("sentence", {})
                text = (output.get("text", "") or "").strip()
                is_final = bool(sentence.get("sentence_end", False))
                sid = sentence.get("sentence_id")
                # 诊断日志（带大小轮转，防止长课堂写爆磁盘）
                append_log(self._cfg, "asr_result.log",
                           f"[{time.strftime('%H:%M:%S')}] {self._channel} final={is_final} text={text[:80]}\n")
                if sid != self._sentence_id:   # 新的一句：重置进度
                    self._sentence_id = sid
                    self._committed = 0
                if not text:
                    if is_final:
                        self._committed = 0
                    return
                if is_final:
                    # 收尾：把还没翻出去的尾巴交出去（前面已经边识别边翻了）
                    tail = text[self._committed:] if self._committed < len(text) else ""
                    if tail.strip():
                        self._emit_segment(tail.strip(), 0, 0)
                    elif self._committed == 0:
                        self._emit_segment(text, 0, 0)
                    self._committed = 0
                else:
                    self._emit_progressive(text)
            elif event == "task-finished":
                self._task_finished.set()
            elif event == "error":
                code = data.get("payload", {}).get("code", "")
                msg = data.get("payload", {}).get("message", "")
                if code != "ASR_RESPONSE_HAVE_NO_WORDS":
                    self._status_cb(f"ASR 服务端错误（{self._channel}）: {code} {msg}")
        except Exception:
            pass

    def _emit_progressive(self, text: str) -> None:
        """长句边识别边出译文。

        只要已识别文本攒到标点边界（。！？；等）并且够长，就立刻把这一段交给翻译，
        不用等整句说完 —— 长句的译文能提前好几秒出现，UI 上按字幕行依次追加。
        剩余还没稳定的部分继续以灰色"识别中…"显示。
        """
        if self._committed > len(text):   # 识别结果被回改（变短）：重新开始累计
            self._committed = 0
        pending = text[self._committed:]
        if not self._chunk_stream:
            if pending.strip():
                self._emit_preview(pending)
            return
        cut = 0
        # 1) 强标点（句末/分句）：必须含俄语/英语的 '.'，否则俄语句子永远等不到边界
        for i in range(len(pending) - 1, -1, -1):
            if pending[i] in STRONG_BOUNDARY:
                if i + 1 >= self._chunk_min_chars:
                    cut = i + 1
                break
        # 2) 迟迟没有句末标点：攒够 chunk_max_chars 就在最近的弱标点/空格处切
        if cut == 0 and len(pending) >= self._chunk_max_chars:
            head = pending[: self._chunk_max_chars]
            for i in range(len(head) - 1, self._chunk_min_chars - 1, -1):
                if head[i] in WEAK_BOUNDARY:
                    cut = i + 1
                    break
            if cut == 0:
                cut = self._chunk_max_chars
        if cut > 0:
            chunk = pending[:cut].strip()
            if chunk:
                self._emit_segment(chunk, 0, 0)
            self._committed += cut
        tail = text[self._committed:]
        if tail.strip():
            self._emit_preview(tail)

    def _emit_preview(self, text: str):
        """临时字幕：中间识别结果，状态为 preview，UI 用灰色显示。"""
        if self._preview_q is None:
            return
        now_ms = int(time.monotonic() * 1000) - self._timeline_origin_ms
        try:
            self._preview_q.put_nowait(TranscriptSegment(
                seq=-1, speaker=self._channel,
                start_ms=now_ms, end_ms=now_ms,
                ru_text=text, status="preview", confidence=0.5,
            ))
        except queue.Full:
            pass

    def _emit_segment(self, text: str, begin_ms: int, end_ms: int):
        if self._seq_lock and self._seq_ref is not None:
            with self._seq_lock:
                self._seq_ref[0] += 1
                seq = self._seq_ref[0]
        else:
            seq = 0
        # 用本地统一单调时钟做会话时间轴，云端时间仅作通道内参考
        now_ms = int(time.monotonic() * 1000) - self._timeline_origin_ms
        self._out_q.put(TranscriptSegment(
            seq=seq, speaker=self._channel,
            start_ms=now_ms, end_ms=now_ms,
            ru_text=text, status="recognized", confidence=0.9,
        ))

    # ---- 主循环（含自动重连）----
    def run(self) -> None:
        try:
            if not self._connect():
                return
            self._status_cb(f"语音识别就绪（{self._channel}，WebSocket 流式）")
            frame_ms = self._cfg.get_int("asr", "frame_ms")
            sample_rate = self._cfg.get_int("asr", "sample_rate")
            frame_samples = int(sample_rate * frame_ms / 1000)
            last_send = time.time()
            reconnect_delay = 1  # 重连初始延迟（指数退避）
            reconnect_count = 0
            while not self._stop_evt.is_set():
                # 自动重连：检测连接断开
                if not self._ws_connected.is_set() and not self._stop_evt.is_set():
                    reconnect_count += 1
                    self._status_cb(f"ASR 连接断开，{reconnect_delay}秒后第{reconnect_count}次重连（{self._channel}）…")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30)  # 指数退避，最大30秒
                    try:
                        if self._ws:
                            try:
                                self._ws.close()
                            except Exception:
                                pass
                        self._ws_connected.clear()
                        self._task_started.clear()
                        self._task_finished.clear()
                        if self._connect():
                            self._status_cb(f"ASR 重连成功（{self._channel}）")
                            reconnect_delay = 1
                            reconnect_count = 0
                    except Exception as exc:
                        self._status_cb(f"ASR 重连失败（{self._channel}）: {exc}")
                    continue
                # 正常发送音频
                try:
                    chunk = self._in_q.get(timeout=0.1)
                except queue.Empty:
                    chunk = None
                # 采集/播放侧的状态与错误信息（设备不存在、打开失败、文件解不了…）
                # 必须显式转成界面提示，否则用户只会看到"运行中"却没有任何字幕。
                if chunk is not None and getattr(chunk, "message", ""):
                    self._status_cb(chunk.message)
                if chunk is not None and chunk.samples.size > 0 and chunk.channel == self._channel:
                    # 诊断：记录音频数据格式（前10次）
                    if not hasattr(self, "_diag_count"):
                        self._diag_count = 0
                    if self._diag_count < 10:
                        self._diag_count += 1
                        try:
                            with open(self._cfg.resolve_path("asr_audio_diag.log"), "a", encoding="utf-8") as _f:
                                _f.write(f"[{time.strftime('%H:%M:%S')}] {self._channel} chunk: samples={chunk.samples.shape}, dtype={chunk.samples.dtype}, min={chunk.samples.min():.4f}, max={chunk.samples.max():.4f}, sr={chunk.sample_rate}\n")
                        except Exception:
                            pass
                    self._buffer = np.concatenate([self._buffer, chunk.samples])
                    # 上限保护：断线/暂停期间不要把旧音频无限堆积（重连后灌一堆过时语音）
                    max_keep = sample_rate * max(1, self._cfg.get_int("asr", "reconnect_buffer_seconds"))
                    if self._buffer.size > max_keep:
                        self._buffer = self._buffer[-max_keep:]
                # 攒够一帧就发送
                now = time.time()
                if len(self._buffer) >= frame_samples or (now - last_send > frame_ms / 1000.0 and len(self._buffer) > 0):
                    send_samples = min(len(self._buffer), frame_samples)
                    audio = self._buffer[:send_samples]
                    self._buffer = self._buffer[send_samples:]
                    audio_int16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
                    pcm_bytes = audio_int16.tobytes()
                    # 诊断：记录发送的PCM数据（前5次）
                    if not hasattr(self, "_pcm_diag_count"):
                        self._pcm_diag_count = 0
                    if self._pcm_diag_count < 5:
                        self._pcm_diag_count += 1
                        try:
                            with open(self._cfg.resolve_path("asr_audio_diag.log"), "a", encoding="utf-8") as _f:
                                _f.write(f"[{time.strftime('%H:%M:%S')}] {self._channel} send PCM: samples={send_samples}, bytes={len(pcm_bytes)}, dtype={audio_int16.dtype}\n")
                        except Exception:
                            pass
                    try:
                        if self._ws and self._ws_connected.is_set():
                            self._ws.send(pcm_bytes, opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception:
                        # 发送失败标记连接断开，触发重连
                        self._ws_connected.clear()
                    last_send = now
            # 停止：发送剩余缓冲 + finish-task
            if len(self._buffer) > 0 and self._ws and self._ws_connected.is_set():
                audio_int16 = np.clip(self._buffer * 32767, -32768, 32767).astype(np.int16)
                try:
                    self._ws.send(audio_int16.tobytes(), opcode=websocket.ABNF.OPCODE_BINARY)
                except Exception:
                    pass
            self._send_finish()
        except Exception as exc:
            import traceback
            self._status_cb(f"ASR 线程异常（{self._channel}）: {exc}")
            try:
                with open(self._cfg.resolve_path("asr_error.log"), "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] ASR-{self._channel} 异常: {exc}\n")
                    f.write(traceback.format_exc() + "\n")
            except Exception:
                pass

    def _send_finish(self):
        if self._ws and self._ws_connected.is_set():
            try:
                finish = {
                    "header": {"action": "finish-task", "task_id": str(uuid.uuid4()), "streaming": "duplex"},
                    "payload": {"input": {}},
                }
                self._ws.send(json.dumps(finish))
                self._task_finished.wait(timeout=5)
            except Exception:
                pass
            try:
                self._ws.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(timeout=8)


class ASRWorker:
    """WebSocket ASR 管理类，支持单通道或双通道（接口与旧版本地 ASRWorker 一致）。

    channels 参数指定启用哪些通道，默认双通道（teacher 系统声音 + me 麦克风），
    由 [audio] capture_mode 决定；哪个通道有声音就翻哪个，互不阻塞。
    """

    def __init__(self, cfg, in_q: "queue.Queue[AudioChunk]",
                 out_q: "queue.Queue[TranscriptSegment]", preview_q=None,
                 status_cb=None, model_ready_cb=None,
                 channels=("teacher", "me")):
        self._cfg = cfg
        self._in_q = in_q
        self._out_q = out_q
        self._preview_q = preview_q
        self._status_cb = status_cb or (lambda msg: None)
        self._model_ready_cb = model_ready_cb or (lambda ok: None)
        self._channels = tuple(channels)
        self._seq_lock = threading.Lock()
        self._seq_ref = [0]
        self._ready_lock = threading.Lock()
        self._ready_results: dict[str, bool] = {}
        self._ready_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._timeline_origin_ms = 0
        # 按启用的通道创建队列和 worker（避免创建空通道的 WebSocket 连接）
        self._channel_qs: dict[str, "queue.Queue[AudioChunk]"] = {}
        self._channel_workers: dict[str, ASRChannelWorker] = {}
        for ch in self._channels:
            q = queue.Queue(maxsize=32)
            self._channel_qs[ch] = q
            self._channel_workers[ch] = ASRChannelWorker(
                cfg, ch, q, out_q, status_cb,
                lambda ok, c=ch: self._on_channel_ready(c, ok),
                self._seq_lock, self._seq_ref,
                preview_q=self._preview_q, timeline_origin_ms=self._timeline_origin_ms)
        self._router = threading.Thread(target=self._route_audio, name="ASRAudioRouter", daemon=True)

    def set_timeline_origin(self, origin_ms: int) -> None:
        self._timeline_origin_ms = origin_ms
        for w in self._channel_workers.values():
            w._timeline_origin_ms = origin_ms

    def _on_channel_ready(self, channel: str, ok: bool):
        # 回调按 worker 顺序到达。任一通道可用即可工作（单通道也能翻译），
        # 只有全部失败才算整体未就绪。
        with self._ready_lock:
            self._ready_results[channel] = ok
            if len(self._ready_results) == len(self._channels):
                any_ok = any(self._ready_results.values())
                if not any_ok:
                    self._status_cb("所有语音识别通道都未连接成功")
                elif not all(self._ready_results.values()):
                    bad = [c for c, v in self._ready_results.items() if not v]
                    self._status_cb(f"部分语音识别通道未连接（{'、'.join(bad)}），其余通道继续工作")
                self._model_ready_cb(any_ok)
                self._ready_evt.set()

    @staticmethod
    def _offer(target: queue.Queue, chunk: AudioChunk) -> None:
        try:
            target.put_nowait(chunk)
        except queue.Full:
            try:
                target.get_nowait()
                target.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass

    def _route_audio(self) -> None:
        while not self._stop_evt.is_set():
            try:
                chunk = self._in_q.get(timeout=0.1)
            except queue.Empty:
                continue
            # 只分流到启用的通道，未启用的通道数据直接忽略
            q = self._channel_qs.get(chunk.channel)
            if q is not None:
                self._offer(q, chunk)

    def wait_ready(self, timeout: float) -> bool:
        """等待识别通道就绪。

        只要**任一**通道连上就立刻返回 True —— 双通道里另一个没声音/没启用/
        连不上，都不允许拖住整条链路。只有全部通道都失败（或超时）才返回 False。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._ready_lock:
                results = dict(self._ready_results)
                done = len(results) == len(self._channels)
            if any(results.values()):
                return True
            if done or time.monotonic() >= deadline:
                return False
            self._ready_evt.wait(0.1)

    def start(self) -> None:
        self._router.start()
        for w in self._channel_workers.values():
            w.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._router.join(timeout=1)
        for w in self._channel_workers.values():
            w.stop()
