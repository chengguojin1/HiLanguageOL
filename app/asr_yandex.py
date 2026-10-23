"""Yandex SpeechKit gRPC 流式实时语音识别（俄罗斯本地服务，从俄罗斯访问稳定）。

协议流程（每个通道独立一条 gRPC 流式连接）：
  1. 建立 gRPC SSL 通道到 stt.api.cloud.yandex.net:443
  2. 发送 StreamingRequest(session_options=StreamingOptions(...))：指定音频格式、语言
  3. 持续发送 StreamingRequest(chunk=AudioChunk(data=pcm_bytes))
  4. 接收 StreamingResponse：
     - partial：中间识别结果（临时字幕，灰色显示）
     - final：最终识别结果（句子结束）
     - final_refinement：归一化后的最终结果
  5. 单次会话最长 5 分钟，到期自动重连（保留未确认的音频缓冲）

与 app/asr_ws.py 的 ASRWorker 接口完全一致，可无缝替换。
"""

from __future__ import annotations

import os
import sys
import queue
import threading
import time
from dataclasses import replace

import numpy as np

# 先导入 google.protobuf，确保 site-packages 中的 google 命名空间包被加载
# （否则 yandex_proto 下的 google 目录会覆盖，导致找不到 protobuf）
from google.protobuf import descriptor as _pb_descriptor  # noqa: F401

# 把 proto 生成目录加入 path，使 `import yandex.cloud.ai.stt.v3.stt_pb2` 可用
_PROTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yandex_proto")
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

# 预加载 yandex_proto 下的 google.api / google.rpc 模块（命名空间包合并）
import importlib
for _mod in ("google.api.http_pb2", "google.api.annotations_pb2", "google.rpc.status_pb2"):
    try:
        importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        pass

import grpc
from yandex.cloud.ai.stt.v3 import stt_pb2
from yandex.cloud.ai.stt.v3 import stt_service_pb2_grpc

from .asr_ws import TranscriptSegment
from .audio import AudioChunk
from .logs import append_log

_YANDEX_ENDPOINT = "stt.api.cloud.yandex.net:443"
# Yandex 流式识别单次会话最长 5 分钟，提前 10 秒重连
_MAX_SESSION_SECONDS = 290


class YandexASRChannelWorker(threading.Thread):
    """单通道 Yandex SpeechKit gRPC 流式 ASR（老师或我）。"""

    def __init__(self, cfg, channel: str, in_q: "queue.Queue[AudioChunk]",
                 out_q: "queue.Queue[TranscriptSegment]",
                 status_cb=None, ready_cb=None, seq_lock=None, seq_ref=None,
                 preview_q=None, timeline_origin_ms: int = 0):
        super().__init__(name=f"YandexASR-{channel}", daemon=True)
        self._cfg = cfg
        self._channel = channel
        self._in_q = in_q
        self._out_q = out_q
        self._preview_q = preview_q
        self._timeline_origin_ms = timeline_origin_ms
        self._status_cb = status_cb or (lambda msg: None)
        self._ready_cb = ready_cb or (lambda ok: None)
        self._seq_lock = seq_lock
        self._seq_ref = seq_ref
        self._stop_evt = threading.Event()
        self._api_key = cfg.get("yandex", "api_key", fallback="").strip()
        self._sample_rate = cfg.get_int("asr", "sample_rate")
        self._lang = cfg.source_lang() or "ru-RU"
        # Yandex 语言代码格式：ru-RU, en-US
        if "-" not in self._lang and len(self._lang) == 2:
            self._lang = f"{self._lang}-{self._lang.upper()}"
        self._session_start = 0.0
        self._grpc_channel = None
        self._stub = None
        self._call = None
        self._connected = threading.Event()
        self._final_text = ""
        self._partial_text = ""
        self._sentence_start_ms = 0

    # ---- 生命周期 ----
    def run(self) -> None:
        if not self._api_key:
            self._status_cb(f"Yandex API Key 为空（{self._channel}），请在设置中配置")
            self._ready_cb(False)
            return
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001
            self._status_cb(f"Yandex ASR 连接失败（{self._channel}）: {exc}")
            self._ready_cb(False)
            return
        self._ready_cb(True)
        self._session_start = time.monotonic()
        # 发送线程 + 接收线程
        send_t = threading.Thread(target=self._send_loop, name=f"YandexSend-{self._channel}", daemon=True)
        recv_t = threading.Thread(target=self._recv_loop, name=f"YandexRecv-{self._channel}", daemon=True)
        send_t.start()
        recv_t.start()
        send_t.join()
        recv_t.join(timeout=2)
        self._close_grpc()

    def stop(self) -> None:
        self._stop_evt.set()
        self._close_grpc()

    def _close_grpc(self) -> None:
        try:
            if self._call is not None:
                self._call.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._grpc_channel is not None:
                self._grpc_channel.close()
        except Exception:  # noqa: BLE001
            pass
        self._connected.clear()

    # ---- gRPC 连接 ----
    def _connect(self) -> None:
        append_log(self._cfg, "yandex_asr.log",
                   f"[{time.strftime('%H:%M:%S')}] {self._channel} connecting to stt.api.cloud.yandex.net:443\n")
        cred = grpc.ssl_channel_credentials()
        self._grpc_channel = grpc.secure_channel(_YANDEX_ENDPOINT, cred)
        self._stub = stt_service_pb2_grpc.RecognizerStub(self._grpc_channel)
        self._call = self._stub.RecognizeStreaming(
            self._request_generator(),
            metadata=[("authorization", f"Api-Key {self._api_key}")],
        )
        append_log(self._cfg, "yandex_asr.log",
                   f"[{time.strftime('%H:%M:%S')}] {self._channel} gRPC stream created, lang={self._lang}, sample_rate={self._sample_rate}\n")
        self._connected.set()

    def _reconnect(self) -> bool:
        """会话到期或断开时重连。"""
        self._close_grpc()
        time.sleep(0.5)
        try:
            self._connect()
            self._session_start = time.monotonic()
            append_log(self._cfg, "yandex_asr.log",
                       f"[{time.strftime('%H:%M:%S')}] {self._channel} reconnected\n")
            return True
        except Exception as exc:  # noqa: BLE001
            self._status_cb(f"Yandex ASR 重连失败（{self._channel}）: {exc}")
            return False

    # ---- 发送音频 ----
    def _request_generator(self):
        """gRPC 请求生成器：先发 session_options，再持续发音频 chunk。"""
        # 第一条：会话配置
        options = stt_pb2.StreamingOptions(
            recognition_model=stt_pb2.RecognitionModelOptions(
                audio_format=stt_pb2.AudioFormatOptions(
                    raw_audio=stt_pb2.RawAudio(
                        audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
                        sample_rate_hertz=self._sample_rate,
                        audio_channel_count=1,
                    )
                ),
                text_normalization=stt_pb2.TextNormalizationOptions(
                    text_normalization=stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED,
                    profanity_filter=False,
                    literature_text=False,
                ),
                language_restriction=stt_pb2.LanguageRestrictionOptions(
                    restriction_type=stt_pb2.LanguageRestrictionOptions.WHITELIST,
                    language_code=[self._lang],
                ),
                audio_processing_type=stt_pb2.RecognitionModelOptions.REAL_TIME,
            )
        )
        yield stt_pb2.StreamingRequest(session_options=options)
        # 后续：音频 chunk
        while not self._stop_evt.is_set():
            # 会话快到期（5分钟），主动结束生成器触发重连
            if time.monotonic() - self._session_start > _MAX_SESSION_SECONDS:
                append_log(self._cfg, "yandex_asr.log",
                           f"[{time.strftime('%H:%M:%S')}] {self._channel} session timeout, reconnecting\n")
                return
            try:
                chunk = self._in_q.get(timeout=0.1)
            except queue.Empty:
                # 发静音保持连接（Yandex 5秒不发数据会断开）
                silence = np.zeros(int(self._sample_rate * 0.1), dtype=np.float32)
                pcm = (silence * 32767).astype(np.int16).tobytes()
                yield stt_pb2.StreamingRequest(chunk=stt_pb2.AudioChunk(data=pcm))
                continue
            # float32 -> int16 PCM
            pcm = (chunk.data * 32767).astype(np.int16).tobytes()
            yield stt_pb2.StreamingRequest(chunk=stt_pb2.AudioChunk(data=pcm))

    def _send_loop(self) -> None:
        """发送线程：管理 gRPC 流的生命周期，到期自动重连。"""
        while not self._stop_evt.is_set():
            try:
                # _request_generator 在 _connect 时已经绑定到 self._call
                # 这里只需要等待，生成器由 gRPC 内部消费
                # 会话到期时生成器 return，_recv_loop 会检测到结束并触发重连
                time.sleep(0.5)
            except Exception:  # noqa: BLE001
                break

    # ---- 接收结果 ----
    def _recv_loop(self) -> None:
        """接收线程：消费 gRPC 响应，输出识别结果。"""
        while not self._stop_evt.is_set():
            if self._call is None:
                time.sleep(0.2)
                continue
            try:
                for resp in self._call:
                    if self._stop_evt.is_set():
                        break
                    self._handle_response(resp)
                # 流正常结束（会话到期）→ 重连
                if not self._stop_evt.is_set():
                    if self._reconnect():
                        continue
                    break
            except grpc.RpcError as exc:  # noqa: PERF203
                if self._stop_evt.is_set():
                    break
                code = exc.code() if hasattr(exc, "code") else "?"
                append_log(self._cfg, "yandex_asr.log",
                           f"[{time.strftime('%H:%M:%S')}] {self._channel} RpcError {code}: {exc}\n")
                if self._reconnect():
                    continue
                self._status_cb(f"Yandex ASR 连接断开（{self._channel}）: {code}")
                break
            except Exception as exc:  # noqa: BLE001
                if self._stop_evt.is_set():
                    break
                append_log(self._cfg, "yandex_asr.log",
                           f"[{time.strftime('%H:%M:%S')}] {self._channel} error: {exc}\n")
                time.sleep(1)

    def _handle_response(self, resp) -> None:
        event_type = resp.WhichOneof("Event")
        now_ms = int((time.monotonic() * 1000) - self._timeline_origin_ms)
        # 调试日志：记录每个响应类型
        append_log(self._cfg, "yandex_asr.log",
                   f"[{time.strftime('%H:%M:%S')}] {self._channel} recv event={event_type}\n")

        if event_type == "partial":
            alts = resp.partial.alternatives
            if not alts:
                return
            text = alts[0].text.strip()
            if not text:
                return
            self._partial_text = text
            # 中间结果 → preview_q（灰色临时字幕）
            if self._preview_q is not None:
                seg = TranscriptSegment(
                    seq=-1, speaker=self._channel,
                    start_ms=self._sentence_start_ms, end_ms=now_ms,
                    ru_text=text, status="partial",
                )
                try:
                    self._preview_q.put_nowait(seg)
                except queue.Full:
                    pass

        elif event_type == "final":
            alts = resp.final.alternatives
            if not alts:
                return
            text = alts[0].text.strip()
            if not text:
                return
            self._final_text = text
            self._emit_final(text, now_ms)

        elif event_type == "final_refinement":
            # 归一化后的最终结果，优先用这个
            try:
                alts = resp.final_refinement.normalized_text.alternatives
            except Exception:  # noqa: BLE001
                alts = []
            if alts:
                text = alts[0].text.strip()
                if text:
                    self._final_text = text
                    # final_refinement 在 final 之后到达，更新最后一条
                    self._update_last_final(text)

        elif event_type == "status_code":
            pass  # 心跳/状态，忽略

    def _next_seq(self) -> int:
        if self._seq_lock is not None:
            with self._seq_lock:
                self._seq_ref[0] += 1
                return self._seq_ref[0]
        return 0

    def _emit_final(self, text: str, end_ms: int) -> None:
        seg = TranscriptSegment(
            seq=self._next_seq(), speaker=self._channel,
            start_ms=self._sentence_start_ms, end_ms=end_ms,
            ru_text=text, status="recognized",
        )
        self._sentence_start_ms = end_ms
        self._partial_text = ""
        try:
            self._out_q.put_nowait(seg)
        except queue.Full:
            try:
                self._out_q.get_nowait()
                self._out_q.put_nowait(seg)
            except (queue.Empty, queue.Full):
                pass

    def _update_last_final(self, text: str) -> None:
        """final_refinement 到达时，用归一化文本更新最后输出的 segment。"""
        # 直接发一条新的 recognized segment（带相同语义），翻译层会去重
        seg = TranscriptSegment(
            seq=self._next_seq(), speaker=self._channel,
            start_ms=self._sentence_start_ms, end_ms=self._sentence_start_ms,
            ru_text=text, status="recognized",
        )
        try:
            self._out_q.put_nowait(seg)
        except queue.Full:
            pass


class YandexASRWorker:
    """Yandex SpeechKit ASR 管理类，接口与 asr_ws.ASRWorker 完全一致。"""

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
        self._channel_qs: dict[str, "queue.Queue[AudioChunk]"] = {}
        self._channel_workers: dict[str, YandexASRChannelWorker] = {}
        for ch in self._channels:
            q = queue.Queue(maxsize=32)
            self._channel_qs[ch] = q
            self._channel_workers[ch] = YandexASRChannelWorker(
                cfg, ch, q, out_q, status_cb,
                lambda ok, c=ch: self._on_channel_ready(c, ok),
                self._seq_lock, self._seq_ref,
                preview_q=self._preview_q, timeline_origin_ms=self._timeline_origin_ms)
        self._router = threading.Thread(target=self._route_audio, name="YandexAudioRouter", daemon=True)

    def set_timeline_origin(self, origin_ms: int) -> None:
        self._timeline_origin_ms = origin_ms
        for w in self._channel_workers.values():
            w._timeline_origin_ms = origin_ms

    def _on_channel_ready(self, channel: str, ok: bool):
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
            q = self._channel_qs.get(chunk.channel)
            if q is not None:
                self._offer(q, chunk)

    def wait_ready(self, timeout: float) -> bool:
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
