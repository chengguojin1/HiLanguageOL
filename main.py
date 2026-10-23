"""中英俄实时翻译软件 · 入口与控制器。

状态机：IDLE → STARTING → RUNNING ⇄ PAUSED → ENDING → IDLE，关键异常进 ERROR。
启动：python main.py（单窗口模式：只启动置顶字幕窗，所有功能在悬浮条+设置内）

线程约定（重要）：
  - 所有 Qt 控件操作只能在主线程做。后台线程一律通过信号（status_changed /
    ui_refresh / start_finished / end_finished / channel_status）回到主线程后再碰控件。
  - 启动/建连（ASR WebSocket、设备打开）全部放在后台线程，主线程立刻返回，
    避免"点了开始翻译之后窗口假死"。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from enum import Enum

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

try:
    from app.asr_ws import ASRWorker
    from app.asr_yandex import YandexASRWorker
    from app.audio import (
        CHANNEL_LABEL, AudioCapture, AudioChunk, capture_channels,
        resolve_capture_devices,
    )
    from app.config import Config
    from app.logs import append_log
    from app.session import Recorder, Session
    from app.summary import generate_summary_async
    from app.translator import Translator
    from app.ui import SubtitleWindow
    from app import theme
except Exception:  # noqa: BLE001
    # pythonw.exe 没有控制台，依赖缺失/导入错误会静默退出（用户以为"没反应"）。
    # 这里用 Win32 弹窗把原因显示出来，再正常抛出。
    import ctypes
    import traceback

    _detail = traceback.format_exc()
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "程序启动失败，通常是虚拟环境依赖缺失。\n\n"
            "请在项目目录执行：\n"
            "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n"
            f"错误详情：\n{_detail[-700:]}",
            "HiLanguageOL 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass
    raise


class State(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    ENDING = "ending"
    SUMMARIZING = "summarizing"   # 手动生成课堂纪要中
    ERROR = "error"


def _drain(q: "queue.Queue") -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class Controller(QObject):
    # 跨线程信号：后台线程 emit，主线程 slot 执行（替代 QTimer.singleShot）
    status_changed = pyqtSignal(str)
    summary_done = pyqtSignal(bool, str)
    end_finished = pyqtSignal(str)
    model_ready = pyqtSignal(bool)
    # 启动流程（后台线程 → 主线程）
    start_finished = pyqtSignal(bool, str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.state = State.IDLE
        self.paused = False
        self.last_status = "就绪"
        self._ending = False
        self._quit_after_end = False
        self._starting = False
        self._cancel_evt = threading.Event()
        self._start_thread: threading.Thread | None = None
        self._summarizing = False
        self._end_summary = False   # 本次「结束」是否附带生成纪要（由调用方决定）

        self.audio_q: "queue.Queue[AudioChunk]" = queue.Queue(maxsize=16)
        self.rec_q: "queue.Queue[AudioChunk]" = queue.Queue(maxsize=64)
        self.seg_q: "queue.Queue" = queue.Queue(maxsize=64)
        self.disp_q: "queue.Queue" = queue.Queue(maxsize=128)

        self.capture: AudioCapture | None = None
        self.asr: ASRWorker | None = None
        self.translator: Translator | None = None
        self.recorder: Recorder | None = None
        self.session: Session | None = None
        self._summary_thread: threading.Thread | None = None
        self._silent_since: dict[str, float] = {}
        self._silent_warned: dict[str, float] = {}
        self._silence_hint_s = max(3, cfg.get_int("audio", "silence_hint_seconds"))

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.subtitle = SubtitleWindow(cfg)
        self.subtitle.disp_q = self.disp_q  # type: ignore[attr-defined]
        self.subtitle.set_controller(self)
        # 跨线程信号连接到主线程 slot
        self.status_changed.connect(self._status)
        self.summary_done.connect(self._apply_summary_done)
        self.end_finished.connect(self._finish_with_status)
        self.model_ready.connect(self._apply_model_ready)
        self.start_finished.connect(self._apply_start_finished)
        # 通道健康状态轮询（主线程定时器，安全碰控件）
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._poll_channels)
        self._health_timer.start(1000)

    # ---- 状态提示 ----
    def _status(self, message: str) -> None:
        """只能从主线程调用；后台线程请用 self.status_changed.emit(...)。"""
        self.last_status = message
        self.subtitle.set_status(message)

    # ---- 通道健康状态 ----
    def _poll_channels(self) -> None:
        src = self.capture
        if src is None:
            return
        try:
            text = src.status_text()
            snap = src.snapshot()
        except Exception:  # noqa: BLE001
            return
        # 翻译链路的实时状态：待译积压 + 上一句耗时（一眼看出是不是网络/模型慢）
        if self.state == State.RUNNING and self.translator is not None:
            try:
                st = self.translator.stats()
                extra = []
                if st.get("pending"):
                    extra.append(f"待译 {st['pending']}")
                # 延迟只在最近 15 秒内翻过句子时显示，避免显示过期数据
                if st.get("last_ms") and (time.monotonic() - st.get("last_ts", 0)) < 15:
                    extra.append(f"{st['last_ms']}ms")
                if st.get("fail"):
                    extra.append(f"失败 {st['fail']}")
                if extra:
                    text = f"{text}　|　{' '.join(extra)}"
            except Exception:  # noqa: BLE001
                pass
        self.subtitle.set_channels(text)
        if self.state != State.RUNNING:
            self._silent_since.clear()
            return
        now = time.monotonic()
        for ch, health in snap.items():
            if not health.opened:
                continue
            if health.state == "signal":
                self._silent_since.pop(ch, None)
                self._silent_warned.pop(ch, None)
                continue
            since = self._silent_since.setdefault(ch, now)
            if now - since < self._silence_hint_s:
                continue
            last_warn = self._silent_warned.get(ch, 0.0)
            if last_warn and now - last_warn < 60:
                continue  # 同一段静音最多每分钟提醒一次，避免刷屏
            self._silent_warned[ch] = now
            if ch == "teacher":
                self.status_changed.emit(
                    f"已经 {int(now - since)} 秒没听到老师的声音：请检查回环设备是否选对、"
                    f"课堂声音是否被静音")
            else:
                self.status_changed.emit(
                    f"已经 {int(now - since)} 秒没听到你的声音（麦克风被静音或设备未选对）")

    # ---- 启动（后台线程，UI 不阻塞） ----
    def start(self) -> None:
        if self.state not in (State.IDLE, State.ERROR) or self._starting:
            return
        self._prepare_session()
        self._begin_starting("正在启动…（正在连接语音识别）")
        self._start_thread = threading.Thread(
            target=self._start_worker, name="StartWorker", daemon=True)
        self._start_thread.start()

    def _prepare_session(self) -> None:
        """主线程：新建会话、清空残留队列。"""
        self.session = Session(self.cfg, name=self.session_name())
        self.disp_q = queue.Queue(maxsize=128)
        self.subtitle.disp_q = self.disp_q  # type: ignore[attr-defined]
        for q in (self.audio_q, self.seg_q, self.rec_q, self.disp_q):
            _drain(q)

    def _begin_starting(self, message: str) -> None:
        self._starting = True
        self._cancel_evt.clear()
        self._silent_since.clear()
        self.state = State.STARTING
        self._status(message)
        self.subtitle.update_controls()

    def _start_worker(self) -> None:
        """后台线程：建 Translator/ASR/采集/录音。用信号回报结果。"""
        try:
            channels = capture_channels(self.cfg)
            devices, notes, errors = resolve_capture_devices(self.cfg, channels)
            usable = []
            for ch in channels:
                label = CHANNEL_LABEL.get(ch, ch)
                if devices.get(ch) is None:
                    self.status_changed.emit(f"{label}通道不可用：{errors.get(ch, '未发现设备')}")
                else:
                    usable.append(ch)
                    if notes.get(ch):
                        self.status_changed.emit(f"{label}通道：{notes[ch]}")
            if not usable:
                raise RuntimeError("没有任何可用音频设备（老师回环 / 麦克风都打不开）")
            self._build_pipeline(channels)
            if self._cancel_evt.is_set():
                self._abort_start()
                return
            if not self.asr.wait_ready(timeout=12):  # type: ignore[union-attr]
                raise RuntimeError("语音识别连接未就绪，请检查网络与 API Key")
            if self._cancel_evt.is_set():
                self._abort_start()
                return
            self.capture = AudioCapture(self.cfg, self.audio_q, self.rec_q,
                                        channels=tuple(channels))
            self.capture.start()
            self.recorder = Recorder(self.cfg, self.rec_q, self.session)  # type: ignore[arg-type]
            self.recorder.start()
            names = "、".join(CHANNEL_LABEL.get(c, c) for c in channels)
            self.start_finished.emit(True, f"翻译运行中（{names}通道，谁说翻谁）")
        except Exception as exc:  # noqa: BLE001
            self._teardown()
            self.start_finished.emit(False, f"启动失败: {exc}")

    def _build_pipeline(self, channels) -> None:
        """后台线程：创建并启动 Translator 与 ASRWorker。"""
        self.translator = Translator(
            self.cfg, self.seg_q, self.disp_q,
            get_context=self.session.get_context,  # type: ignore[union-attr]
            status_cb=self.status_changed.emit,
            on_segment=self.session.add_segment,   # type: ignore[union-attr]
        )
        self.translator.start()
        asr_engine = self.cfg.get("asr", "engine", fallback="aliyun").lower()
        asr_cls = YandexASRWorker if asr_engine == "yandex" else ASRWorker
        self.asr = asr_cls(
            self.cfg, self.audio_q, self.seg_q, preview_q=self.disp_q,
            status_cb=self.status_changed.emit,
            model_ready_cb=self.model_ready.emit,
            channels=tuple(channels),
        )
        self.asr.set_timeline_origin(int(time.monotonic() * 1000))
        self.asr.start()

    def _abort_start(self) -> None:
        self._teardown()
        self.start_finished.emit(False, "已取消启动")

    def _apply_start_finished(self, ok: bool, message: str) -> None:
        """主线程：处理启动结果。"""
        self._starting = False
        self.state = State.RUNNING if ok else State.IDLE
        self.paused = False
        self._status(message)
        self.subtitle.update_controls()
        if not ok and self._quit_after_end:
            self._finish_ending()

    def _apply_model_ready(self, ok: bool) -> None:
        """ASR 线程的模型加载结果，仅在 Qt 主线程处理。"""
        if ok and self.translator is not None:
            # ASR 连接就绪后预热翻译模型（防首句冷加载超时）
            threading.Thread(
                target=self.translator.warmup, name="TranslatorWarmup", daemon=True
            ).start()

    def toggle_pause(self) -> None:
        if self.state == State.RUNNING:
            self.state = State.PAUSED
            self.paused = True
            if self.capture is not None:
                self.capture.pause()   # 真正暂停采集（旧实现只改状态，声音照旧在跑）
            self._status("已暂停")
        elif self.state == State.PAUSED:
            self.state = State.RUNNING
            self.paused = False
            if self.capture is not None:
                self.capture.resume()
            self._status("已继续")
        self.subtitle.update_controls()

    def end(self, generate_summary: bool | None = None) -> None:
        """结束会话：停采集 → 落盘（录音/双语文本）→（可选）生成课堂纪要。

        generate_summary：
          None  = 按配置 [session] auto_summary 决定（默认 false，即不自动生成）
          False = 绝不生成（点「退出」走这条，保证退出不产生纪要）
          True  = 结束后立即生成
        注意：本方法可能被后台线程（采集/识别回调）调用，
        因此这里绝不允许直接操作 Qt 控件（只能 emit 信号）。
        """
        if self._starting:
            # 启动还没完成：先取消，启动线程收尾后由 _apply_start_finished 决定去向
            self._cancel_evt.set()
            self.status_changed.emit("正在取消启动…")
            return
        if self.state not in (State.RUNNING, State.PAUSED) or self._ending:
            return
        self._end_summary = (self.cfg.get_bool("session", "auto_summary")
                             if generate_summary is None else bool(generate_summary))
        self._ending = True
        self.state = State.ENDING
        self.status_changed.emit("正在结束（保存录音与双语文本）…")
        threading.Thread(target=self._end_worker, name="EndingWorker", daemon=True).start()

    def _end_worker(self) -> None:
        """后台线程：停止采集 → 落盘（录音/转写） →（按 _end_summary）生成纪要 → 信号回调 UI。"""
        log_lines = []
        def _log(msg):
            append_log(self.cfg, "end_flow.log", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        t0 = time.monotonic()
        try:
            _log("开始结束流程")
            self.status_changed.emit("正在停止音频采集…")
            if self.capture:
                self.capture.stop()
            _log(f"capture stopped ({time.monotonic()-t0:.1f}s)")
            self.status_changed.emit("正在停止语音识别…")
            if self.asr:
                self.asr.stop()
            _log(f"asr stopped ({time.monotonic()-t0:.1f}s)")
            self.status_changed.emit("正在停止翻译…")
            if self.translator:
                self.translator.stop()
            _log(f"translator stopped ({time.monotonic()-t0:.1f}s)")
            if self.recorder:
                self.recorder.stop()
            _log(f"recorder stopped ({time.monotonic()-t0:.1f}s)")
            self.status_changed.emit("正在保存录音与双语文本…")
            assert self.session is not None
            files = self.session.finalize()
            _log(f"finalize done ({time.monotonic()-t0:.1f}s), files={list(files.keys())}")
            missing = [k for k, v in files.items()
                       if k != "summary" and not self._file_ok(v)]
            if missing:
                _log(f"missing files: {missing}")
                self.end_finished.emit(f"部分文件保存异常: {missing}，其余已保存，可退出")
                return
            if self._end_summary:
                _log("starting summary generation")
                self.status_changed.emit("录音与双语文本已保存，正在生成纪要…")
                self._summary_thread = generate_summary_async(
                    self.cfg, self.session, done_cb=self._on_summary_done,
                )
            else:
                _log("summary skipped（手动模式：点「纪要」按钮才生成）")
                self.end_finished.emit(
                    "录音与双语文本已保存；可点「纪要」生成课堂要点，或直接退出")
            _log(f"end_worker finished normally ({time.monotonic()-t0:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            _log(f"END WORKER EXCEPTION: {exc}")
            import traceback
            _log(traceback.format_exc())
            self.end_finished.emit(f"保存失败: {exc}，可退出")

    def _finish_with_status(self, message: str) -> None:
        # 由 end_finished 信号在主线程执行
        self._status(message)
        self._finish_ending()

    @staticmethod
    def _file_ok(path: str) -> bool:
        try:
            return os.path.exists(path) and os.path.getsize(path) > 0
        except OSError:
            return False

    def _on_summary_done(self, ok: bool, info: str) -> None:
        # 由 SummaryWorker 后台线程回调，通过信号安全传递到主线程
        self.summary_done.emit(ok, info)

    def _apply_summary_done(self, ok: bool, info: str) -> None:
        manual = self.state == State.SUMMARIZING
        if ok:
            if manual:
                self._status(f"课堂纪要已生成：{info}")
                self.subtitle.notify("课堂纪要已生成", info)
            else:
                self._status("全部产物已保存（录音/双语文本/纪要），可以退出")
        else:
            self._status(f"纪要生成失败：{info}"
                         + ("" if manual else "（录音与双语文本已保存）"))
        self._finish_ending()

    # ---- 手动生成课堂纪要（「纪要」按钮）----
    def can_summarize(self) -> bool:
        """当前是否可以生成纪要：会话已结束、有转写内容、且没有正在生成。"""
        return (not self._summarizing
                and not self._ending
                and not self._starting
                and self.state == State.IDLE
                and self.session is not None
                and self.session.end_time is not None      # 已结束并落盘
                and bool(self.session.segments))           # 有转写内容

    def summarize(self) -> None:
        """手动生成课堂纪要（结束后点「纪要」触发，与「开始翻译」互不干扰）。"""
        if self._summarizing:
            self.status_changed.emit("正在生成纪要，请稍候…")
            return
        if self.state in (State.RUNNING, State.PAUSED):
            self.status_changed.emit("请先点「结束」保存本次课堂，再生成纪要")
            return
        if self.state == State.STARTING:
            self.status_changed.emit("正在启动，请稍候…")
            return
        if self.session is None or self.session.end_time is None:
            self.status_changed.emit("还没有已结束的课堂，无法生成纪要")
            return
        if not self.session.segments:
            self.status_changed.emit("本次课堂没有识别到内容，无法生成纪要")
            return
        self._summarizing = True
        self.state = State.SUMMARIZING
        seg_n = len(self.session.segments)
        self._status(f"正在生成课堂纪要…（基于 {seg_n} 条转写）")
        self.subtitle.update_controls()
        self._summary_thread = generate_summary_async(
            self.cfg, self.session, done_cb=self._on_summary_done)

    def _finish_ending(self) -> None:
        self._ending = False
        self._starting = False
        self._summarizing = False
        self.state = State.IDLE
        self.paused = False
        self._silent_since.clear()
        self._silent_warned.clear()
        # 采集已停，置空避免通道状态行显示过期信息
        self.capture = None
        self.subtitle.set_channels("")
        self.subtitle.update_controls()
        if self._quit_after_end:
            self._do_quit()

    def _do_quit(self) -> None:
        self.subtitle.close()
        self.app.quit()

    def open_sessions(self) -> None:
        """打开会议纪要目录（录音 / 双语文本 / 纪要文件）。"""
        root = self.cfg.resolve_path(self.cfg.get("session", "output_dir"))
        root.mkdir(parents=True, exist_ok=True)
        os.startfile(str(root))

    def open_config_dir(self) -> None:
        r"""打开当前生效的配置目录，方便直接查看/备份 API Key 等设置。

        打包安装版：%APPDATA%HiLanguageOL（升级、卸载都不会删除）
        源码运行：项目根目录
        """
        try:
            folder = self.cfg.path.parent if self.cfg.path else None
            if folder is None or not folder.exists():
                raise RuntimeError("配置目录不存在")
            os.startfile(str(folder))
        except Exception as exc:  # noqa: BLE001
            self.status_changed.emit(f"打开配置目录失败: {exc}")

    def quit(self) -> None:
        """退出闭环：运行中先保存录音与文本（**不生成纪要**），完成后自动退出。"""
        if self._starting:
            self._cancel_evt.set()
            self._quit_after_end = True
            self._status("正在取消启动并退出…")
            return
        if self.state in (State.RUNNING, State.PAUSED):
            self._quit_after_end = True
            self.end(generate_summary=False)   # 退出只保存录音与文本，绝不生成纪要
            return
        if self.state in (State.ENDING, State.SUMMARIZING):
            # 正在结束 / 正在生成纪要：不打断，等它收尾后自动退出
            self._quit_after_end = True
            return
        self._do_quit()

    # ---- 生命周期（各 stop 的 join 超时见对应类，控制在数秒内，不阻塞 UI） ----
    def _teardown(self) -> None:
        if self.capture:
            self.capture.stop()
        if self.asr:
            self.asr.stop()  # 先停识别并 force 清尾（尾句不丢）
        if self.translator:
            self.translator.stop()  # 再停翻译，消费完剩余识别结果
        if self.recorder:
            self.recorder.stop()
        self.capture = None
        self.asr = None
        self.translator = None
        self.recorder = None

    def session_name(self) -> str:
        from datetime import datetime

        return datetime.now().strftime("%H%M%S")

    def run(self) -> int:
        self.subtitle.show()
        # 首次启动检查：识别服务与翻译服务各自的 Key（留空时提示，不强制）
        missing = []
        if not self.cfg.asr_api_key():
            missing.append("识别服务 API Key")
        if not self.cfg.translate_api_key():
            missing.append("翻译服务 API Key")
        if missing:
            msg = QMessageBox(self.subtitle)
            msg.setWindowTitle("中英俄实时翻译软件")
            msg.setText("欢迎使用中英俄实时翻译软件！\n\n"
                        f"还缺少：{'、'.join(missing)}。\n"
                        "识别与翻译是两套独立服务，可以分别填写各自的地址与 Key。\n"
                        "你可以现在配置，也可以稍后在设置中配置。\n\n"
                        "API Key 获取地址：https://bailian.console.aliyun.com/")
            msg.setIcon(QMessageBox.Icon.Information)
            btn_now = msg.addButton("立即配置", QMessageBox.ButtonRole.AcceptRole)
            btn_later = msg.addButton("稍后配置", QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(btn_later)
            msg.exec()
            if msg.clickedButton() == btn_now:
                self.subtitle._on_settings()
        return self.app.exec()


def _ensure_single_instance(app: QApplication) -> bool:
    """单实例锁：Windows 命名 Mutex（内核对象，原子创建，杜绝同时启动竞态）。"""
    import ctypes
    from ctypes import wintypes
    CreateMutexW = ctypes.windll.kernel32.CreateMutexW
    CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    CreateMutexW.restype = wintypes.HANDLE
    GetLastError = ctypes.windll.kernel32.GetLastError
    handle = CreateMutexW(None, False, "Global\\hilanguage-ol-single-instance")
    if GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        QMessageBox.information(
            None, "中英俄实时翻译软件",
            "程序已在运行中（单实例）。\n请在系统托盘图标或任务栏中找到它，"
            "或先用托盘菜单/退出按钮关闭旧实例。",
        )
        return False
    app._singleton_mutex = handle  # type: ignore[attr-defined]  # 保持引用，进程退出时系统自动释放
    return True


def _install_crash_handler(cfg: Config) -> None:
    """安装全局崩溃处理器：sys.excepthook（主线程）+ threading.excepthook（子线程）+ faulthandler（段错误）。

    注意：PyQt6 在槽函数抛未捕获异常时若用默认 excepthook 会直接 abort 进程，
    必须装自定义 hook，异常才能被记录并让程序继续跑。
    """
    import pathlib
    import threading
    import traceback
    import faulthandler

    crash_log = pathlib.Path(cfg.resolve_path("crash.log"))

    def _write_crash(tag: str, exc_type, exc_value, exc_tb) -> None:
        try:
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {tag}\n")
                f.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        except Exception:
            pass

    def _sys_excepthook(exc_type, exc_value, exc_tb):
        _write_crash("主线程未捕获异常", exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args):
        _write_crash(f"子线程未捕获异常 (thread={args.thread})",
                     args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _sys_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook
    # faulthandler：捕获段错误等原生崩溃，打印所有线程堆栈
    # 注意：必须保持对文件对象的全局引用，否则被垃圾回收后 faulthandler 无法写入
    try:
        global _faulthandler_file
        _faulthandler_file = open(crash_log, "a", encoding="utf-8")
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
    except Exception:
        pass


def main() -> int:
    app = QApplication(sys.argv)
    theme.apply(app)   # 深色玻璃主题：调色板 + 控件样式
    if not _ensure_single_instance(app):
        return 0
    cfg = Config()
    theme.set_skin(cfg.get("ui", "skin") or "dark")   # 玻璃皮肤（dark / aurora）
    # 安装全局崩溃处理器（必须在创建 Controller 之前，确保子线程异常也能捕获）
    _install_crash_handler(cfg)
    try:
        controller = Controller(cfg)
        return controller.run()
    except Exception:  # noqa: BLE001 - pythonw 无控制台，必须兜底可见
        import pathlib
        import traceback

        crash_log = pathlib.Path("crash.log")
        try:
            crash_log.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        try:
            QMessageBox.critical(
                None, "中英俄实时翻译软件 - 启动失败",
                f"程序启动失败，详情见 crash.log：\n\n{traceback.format_exc()[-600:]}",
            )
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
