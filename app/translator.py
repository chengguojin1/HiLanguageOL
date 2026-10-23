"""M4 翻译链路：源语言→目标语言（Ollama 本地 LLM / OpenAI 兼容 API 双引擎）。

只服务"实时翻译"这一条链路（音频→识别→翻译→字幕/落盘），设计要点：

- **保序并发**：多句同时翻译（默认 2 并发），但字幕严格按识别顺序输出，
  慢句不会让后面的句子插队，也不会让整条链路停住（超过 stuck_flush_sec 秒
  仍未回来就先按原文显示，后台结果回来后再补进文件）。
- **连接复用**：全程复用一个 requests.Session（HTTP keep-alive），
  省掉每句一次的 TLS 握手，实测每句延迟明显下降。
- **立即占位**：识别一出来先把俄语原文打到字幕上（"翻译中…"），
  译文回来原地替换，用户不会盯着空窗。
- **省调用**：相同句子走缓存；连续重复句直接复用上一句译文；
  语气词/过短碎片不调用大模型。
- **错误分级**：401（Key 无效）/403（无权限或免费额度用尽）/404（模型不存在）
  /429（限流）/5xx/超时 分别处理；主模型不可用时自动切备用模型，并且只提示一次。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import requests

from .asr_ws import TranscriptSegment
from .config import LANG_NAMES
from .logs import append_log


def _lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def build_system_prompt(source_lang: str, target_lang: str, auto_correct: bool) -> str:
    """根据语言对动态生成系统提示词。"""
    src = _lang_name(source_lang)
    tgt = _lang_name(target_lang)
    if auto_correct:
        return (
            f"你是大学网课的实时同传翻译员，把{src}课堂内容译为{tgt}。\n"
            f"第一步，先检查{src}原文是否有语音识别错误（错词、漏词、不成句、语义怪异）。\n"
            f"- 若明显有错：第一行必须输出“{src}纠正：<你判断的最可能正确的{src}原文>”，"
            f"换行后第二行再输出{tgt}译文。\n"
            f"- 若基本正确：直接只输出一行{tgt}译文，绝对不要加任何前缀。宁可保守，不要把正确文本改成错误。\n"
            "绝对禁止：注释、括号说明、反问、建议、提问、补充背景或任何译文以外的文字。\n"
            "翻译策略：译文简洁自然，像给同学转述课堂内容；长句拆成短句，保持通顺；"
            "专业术语结合课程主题翻译，全文保持一致；"
            "句子若被截断（实时字幕常见），直接翻译已给出的部分，不补全、不加省略号。"
        )
    return (
        f"你是大学网课的实时同传翻译员，把{src}课堂内容译为{tgt}。\n"
        f"输出要求：只输出一行{tgt}译文；不加引号、不解释、不重复{src}原文、不加任何引导语。\n"
        "绝对禁止：注释、括号说明、反问、建议、提问、补充背景或任何译文以外的文字。\n"
        "翻译策略：\n"
        "1. 译文简洁自然，像给同学转述课堂内容；长句拆成短句，保持通顺。\n"
        "2. 专业术语结合课程主题翻译，全文保持一致；人名、地名、课程名保留原文或通用译名。\n"
        "3. 句子若被截断（实时字幕常见），直接翻译已给出的部分，不补全、不加省略号。"
    )


# 模型偶尔会把提示词末尾的引导语一起回显出来（实测遇到过 "中文译文：xxx"），
# 这里统一剥掉，避免字幕里出现"中文译文："这种前缀。
_ECHO_PREFIXES = ("中文译文：", "中文译文:", "中文翻译：", "中文翻译:",
                  "译文：", "译文:", "翻译：", "翻译:",
                  "中文：", "中文:", f"中文answer：")


def _strip_echo(text: str) -> str:
    out = (text or "").strip()
    for _ in range(2):  # 最多剥两层（例如"译文：中文译文：xxx"）
        for pfx in _ECHO_PREFIXES:
            if out.startswith(pfx):
                out = out[len(pfx):].strip()
                break
        else:
            break
    return out


def parse_correction(text: str, source_lang: str = "ru") -> tuple[str, str]:
    """解析可能带“XX纠正：”前缀的模型返回，返回 (纠正后原文或空串, 译文)。"""
    text = (text or "").strip()
    src = _lang_name(source_lang)
    for prefix in (f"{src}纠正：", f"{src}纠正:", "俄语纠正：", "俄语纠正:"):
        if text.startswith(prefix):
            rest = text[len(prefix):]
            parts = rest.split("\n", 1)
            corrected = parts[0].strip()
            translated = parts[1].strip() if len(parts) > 1 else ""
            return corrected, _strip_echo(translated)
    return "", _strip_echo(text)


KEEP_ALIVE = "2h"

ENGINE_OLLAMA = "ollama"
ENGINE_OPENAI = "openai"


def build_translate_messages(context: list[tuple[str, str]], current_text: str,
                             source_lang: str = "ru", target_lang: str = "zh",
                             topic: str = "", glossary: str = "",
                             auto_correct: bool = True) -> list[dict]:
    system = build_system_prompt(source_lang, target_lang, auto_correct)
    if topic.strip():
        system += (f"\n本次课程主题：{topic.strip()}。"
                   "请结合该主题的领域知识翻译专业术语，术语在全场保持一致。")
    if glossary.strip():
        system += (f"\n用户术语表（源=目标；必须优先采用）：{glossary.strip()}。"
                   "术语表仅用于术语对应，不要把它复述到译文中。")
    src = _lang_name(source_lang)
    tgt = _lang_name(target_lang)
    user_lines = ["上文（供术语与指代一致参考）："]
    for src_txt, tgt_txt in context[-5:]:
        user_lines.append(f"[{src}] {src_txt}")
        user_lines.append(f"[{tgt}] {tgt_txt}")
    user_lines.append("")
    user_lines.append(f"待翻译（{src}）：{current_text}")
    user_lines.append(f"{tgt}译文：")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def list_ollama_models(base_url: str, timeout: float = 3.0) -> list[str]:
    """拉取 Ollama 已安装模型名列表（设置页动态下拉用；失败返回空）。"""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", []) if m.get("name")]
    except Exception:  # noqa: BLE001
        return []


# ---- 错误分级 ----
class _Retryable(Exception):
    """连接/超时/限流/5xx：可自动重试。"""


class _ServiceDown(_Retryable):
    pass


class _ServiceTimeout(_Retryable):
    pass


class _OOM(Exception):
    """本地模型显存不足，需要换轻量模型。"""


class _AuthError(Exception):
    """API Key 无效 / 无权限：重试无意义。"""


class _QuotaError(Exception):
    """免费额度用尽或无该模型权限，切备用模型。"""


class _ModelGone(Exception):
    """模型不存在或接口路径错误，切备用模型。"""


class _ContentBlocked(Exception):
    """内容审核拦截（400 data_inspection 等）：这句跳过即可，换模型也没用。"""


def _is_oom(message: str) -> bool:
    low = message.lower()
    return any(k in low for k in ("oom", "out of memory", "memory", "vram", "capacity"))


def _norm_key(text: str) -> str:
    """缓存键归一化：忽略大小写、标点、多余空白。"""
    out = []
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
    return "".join(out).strip()


def _err_text(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                msg = str(err.get("message", ""))
                code = str(err.get("code", "") or err.get("type", ""))
                return f"{msg}（{code}）" if code else msg
            return str(err)
        return json.dumps(data, ensure_ascii=False)[:200]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]


class Translator(threading.Thread):
    """识别结果 → 译文：保序并发 + 连接复用 + 立即占位。"""

    def __init__(self, cfg, in_q: "queue.Queue[TranscriptSegment]",
                 out_q: "queue.Queue[TranscriptSegment]",
                 get_context, status_cb=None, on_segment=None):
        super().__init__(name="Translator", daemon=True)
        self._cfg = cfg
        self._in_q = in_q
        self._out_q = out_q
        self._get_context = get_context
        self._status_cb = status_cb or (lambda msg: None)
        self._on_segment = on_segment
        self._stop_evt = threading.Event()
        self._model = cfg.get("translate", "model_name")
        self._fallback = cfg.get("translate", "fallback_model_name")
        self._engine = cfg.get("translate", "engine")
        self._source_lang = cfg.source_lang()
        self._target_lang = cfg.target_lang()
        self._workers = max(1, min(4, cfg.get_int("translate", "workers")))
        self._flush_after = max(3.0, cfg.get_float("translate", "stuck_flush_sec"))

        # 连接复用：keep-alive 连接池，省掉每句一次 TLS 握手
        self._http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self._workers + 1, pool_maxsize=self._workers + 2)
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)

        self._pool = ThreadPoolExecutor(max_workers=self._workers,
                                        thread_name_prefix="Trans")
        # 保序输出缓冲：key 为内部递增序号（与 UI 的 seq 无关，避免冲突）
        self._lock = threading.Lock()
        self._order: list[int] = []
        self._segs: dict[int, TranscriptSegment] = {}
        self._done: set[int] = set()
        self._abandoned: set[int] = set()
        self._submitted: dict[int, float] = {}
        self._next_key = 0

        # 译文缓存（重复句子不重复调用大模型）
        self._cache: dict[str, str] = {}
        self._cache_keys: list[str] = []
        self._last_norm = ""
        self._last_zh = ""

        self._primary_dead = False
        self._last_err = ""
        self._last_err_ts = 0.0
        self._stats = {"total": 0, "ok": 0, "fail": 0, "cache_hit": 0, "dup": 0,
                       "last_ms": 0, "avg_ms": 0, "last_ts": 0.0}

        # 智能过滤：语气词和太短的句子不翻译（减少大模型调用）
        self._skip_phrases = {
            "嗯", "啊", "哦", "唉", "诶", "呃", "这个", "那个", "就是",
            "да", "нет", "ну", "вот", "это", "то есть", "так", "хорошо",
        }

    # ---- 生命周期 ----
    def stop(self) -> None:
        self._stop_evt.set()
        self.join(timeout=5)

    def warmup(self) -> None:
        """预热：把模型和连接都热起来，避免首句冷启动超时。"""
        if self._target_lang == "none":
            return
        try:
            self._call(self._model, "Привет")
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> dict:
        with self._lock:
            data = dict(self._stats)
            data["pending"] = len(self._order)
        return data

    # ---- 主循环 ----
    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                seg = self._in_q.get(timeout=0.1)
            except queue.Empty:
                seg = None
            if seg is not None:
                try:
                    self._handle(seg)
                except Exception as exc:  # noqa: BLE001
                    self._status_cb(f"翻译调度异常: {exc}")
            self._drain()
        # 收尾：给最后几句一点时间，再把剩下的强制放出去（不丢字幕）
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self._pending_count() > 0:
            self._drain()
            time.sleep(0.05)
        self._drain(force=True)
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass

    def _pending_count(self) -> int:
        with self._lock:
            return len(self._order)

    def _handle(self, seg: TranscriptSegment) -> None:
        """分类处理一条识别结果：直接出结果 or 提交后台翻译（保持顺序）。"""
        if seg.status != "recognized":
            self._enqueue(seg, ready=True)
            return
        if seg.speaker == "me" and not self._cfg.get_bool("ui", "show_my_translation"):
            seg.zh_text = ""
            seg.status = "skipped"
            self._enqueue(seg, ready=True)
            return
        if self._target_lang == "none":
            seg.zh_text = ""
            seg.status = "translated"
            self._enqueue(seg, ready=True)
            return
        text = (seg.ru_text or "").strip()
        norm = _norm_key(text)
        if not norm or len(text) <= 2 or text.lower() in self._skip_phrases:
            seg.zh_text = ""
            seg.status = "translated"
            self._enqueue(seg, ready=True)
            return
        with self._lock:
            cached = self._cache.get(norm)
            if cached is None and norm == self._last_norm and self._last_zh:
                cached = self._last_zh
                self._stats["dup"] += 1
        if cached is not None:
            seg.zh_text = cached
            seg.status = "translated"
            with self._lock:
                self._stats["cache_hit"] += 1
                self._last_norm, self._last_zh = norm, cached
            self._enqueue(seg, ready=True)
            return
        # 真正要调大模型：先占位显示原文，再后台翻译
        key = self._enqueue(seg, ready=False)
        self._emit_placeholder(seg)
        with self._lock:
            self._stats["total"] += 1
        self._pool.submit(self._translate_task, key, seg, norm)

    def _enqueue(self, seg: TranscriptSegment, ready: bool) -> int:
        with self._lock:
            self._next_key += 1
            key = self._next_key
            self._order.append(key)
            self._segs[key] = seg
            self._submitted[key] = time.monotonic()
            if ready:
                self._done.add(key)
            return key

    def _drain(self, force: bool = False) -> None:
        """按识别顺序输出：先完成的等前面的，等太久就按原文先放出去。"""
        while True:
            with self._lock:
                if not self._order:
                    return
                key = self._order[0]
                seg = self._segs.get(key)
                ready = key in self._done
                waited = time.monotonic() - self._submitted.get(key, time.monotonic())
            if seg is None:
                with self._lock:
                    self._order.pop(0)
                continue
            if ready:
                with self._lock:
                    self._order.pop(0)
                    self._segs.pop(key, None)
                    self._submitted.pop(key, None)
                self._emit(seg)
                continue
            if force or waited > self._flush_after:
                with self._lock:
                    self._order.pop(0)
                    self._segs.pop(key, None)
                    self._abandoned.add(key)
                    self._stats["fail"] += 1
                if seg.status == "recognized":
                    seg.status = "failed"
                self._emit(seg)
                self._status_cb(f"这句翻译超过 {int(waited)} 秒还没回来，先显示原文")
                continue
            return

    # ---- 单句翻译（线程池里执行）----
    def _translate_task(self, key: int, seg: TranscriptSegment, norm: str) -> None:
        t0 = time.monotonic()
        ok = False
        fatal = False  # 鉴权类错误：不必再试备用模型
        chain = self._model_chain()
        for model in chain:
            if ok or fatal:
                break
            for attempt in (0, 1):
                try:
                    result = self._call(model, seg.ru_text)
                    corrected, translated = parse_correction(result or "", self._source_lang)
                    if translated:
                        seg.corrected_ru = corrected
                        seg.zh_text = translated
                        seg.status = "translated"
                        with self._lock:
                            self._remember(norm, translated)
                            self._last_norm, self._last_zh = norm, translated
                        ok = True
                    break
                except _AuthError as exc:
                    self._notify(str(exc))
                    fatal = True
                    break
                except (_QuotaError, _ModelGone) as exc:
                    if model == self._model and len(chain) > 1:
                        self._primary_dead = True
                        self._notify(f"{exc}；已自动切换到备用模型 {self._fallback}")
                    else:
                        self._notify(f"翻译失败（保留原文）: {exc}")
                    break  # 换下一个模型
                except _OOM as exc:
                    if model != self._fallback:
                        self._primary_dead = True
                        self._notify(f"显存不足，已切换到轻量模型 {self._fallback}")
                    else:
                        self._notify(f"翻译失败（保留原文）: {exc}")
                    break
                except _ContentBlocked as exc:
                    self._notify(str(exc))
                    break
                except _Retryable as exc:
                    if attempt == 0:
                        time.sleep(1.5)  # 连接/超时/限流：退避后重试一次
                        continue
                    self._notify(f"翻译失败（保留原文）: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001
                    self._notify(f"翻译失败（保留原文）: {exc}")
                    break
        if not ok and seg.status == "recognized":
            seg.zh_text = ""
            seg.status = "failed"
        cost = int((time.monotonic() - t0) * 1000)
        with self._lock:
            self._done.add(key)
            self._stats["last_ms"] = cost
            self._stats["last_ts"] = time.monotonic()
            if ok:
                self._stats["ok"] += 1
            prev = self._stats["avg_ms"]
            n = max(1, self._stats["ok"])
            self._stats["avg_ms"] = int(prev + (cost - prev) / n) if prev else cost
        self._log_call(seg, cost, ok)

    def _model_chain(self) -> list[str]:
        chain = []
        if not self._primary_dead:
            chain.append(self._model)
        if self._fallback and self._fallback != self._model:
            chain.append(self._fallback)
        return chain or [self._model]

    def _remember(self, norm: str, zh: str) -> None:
        """写入缓存（带容量上限，FIFO 淘汰）。"""
        if norm in self._cache:
            return
        self._cache[norm] = zh
        self._cache_keys.append(norm)
        if len(self._cache_keys) > 500:
            for k in self._cache_keys[:250]:
                self._cache.pop(k, None)
            self._cache_keys = self._cache_keys[250:]

    def _notify(self, msg: str) -> None:
        """同一条错误 30 秒内只提示一次，避免刷屏。"""
        now = time.monotonic()
        if msg == self._last_err and now - self._last_err_ts < 30:
            return
        self._last_err, self._last_err_ts = msg, now
        self._status_cb(msg)

    def _log_call(self, seg: TranscriptSegment, cost_ms: int, ok: bool) -> None:
        append_log(self._cfg, "translate_call.log",
                   f"[{time.strftime('%H:%M:%S')}] {'OK ' if ok else 'FAIL'} {cost_ms}ms | "
                   f"{seg.ru_text[:60]} -> {(seg.zh_text or '')[:60]}\n")

    # ---- 输出 ----
    def _emit_placeholder(self, seg: TranscriptSegment) -> None:
        """占位：先把俄语原文发到 UI（显示"翻译中…"），不发 on_segment。"""
        preview = replace(seg)
        try:
            self._out_q.put_nowait(preview)
        except queue.Full:
            pass

    def _emit(self, seg: TranscriptSegment) -> None:
        try:
            self._out_q.put_nowait(seg)
        except queue.Full:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out_q.put_nowait(seg)
            except queue.Full:
                return
        if self._on_segment is not None:
            try:
                self._on_segment(seg)
            except Exception:  # noqa: BLE001
                pass

    # ---- 调用 ----
    def _call(self, model: str, text: str) -> str:
        if self._engine == ENGINE_OPENAI:
            return self._call_openai(model, text)
        return self._call_ollama(model, text)

    # ---- Ollama 原生 API ----
    def _call_ollama(self, model: str, text: str) -> str:
        context = self._get_context(self._cfg.get_int("translate", "context_sentences"))
        messages = build_translate_messages(
            context, text, self._source_lang, self._target_lang,
            self._cfg.get("translate", "topic"),
            self._cfg.get("translate", "glossary"),
            auto_correct=self._cfg.get_bool("translate", "auto_correct"))
        base = self._cfg.translate_base_url().rstrip("/")
        try:
            resp = self._http.post(
                f"{base}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": {
                        "num_predict": self._cfg.get_int("translate", "max_tokens"),
                        "temperature": self._cfg.get_float("translate", "temperature"),
                    },
                },
                timeout=self._cfg.get_float("translate", "timeout_sec"),
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise _ServiceDown("翻译服务不可达（请确认已运行 ollama serve）") from exc
        except requests.exceptions.ConnectionError as exc:
            raise _ServiceDown("翻译服务未启动（请运行 ollama serve）") from exc
        except requests.exceptions.ReadTimeout as exc:
            raise _ServiceTimeout("翻译超时（模型加载或GPU繁忙）") from exc
        except requests.exceptions.Timeout as exc:
            raise _ServiceTimeout("翻译请求超时") from exc
        except requests.exceptions.RequestException as exc:
            raise _Retryable(f"翻译请求失败: {exc}") from exc
        if resp.status_code >= 500:
            raise _Retryable(f"翻译服务暂时不可用（{resp.status_code}）")
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            if _is_oom(str(data["error"])):
                raise _OOM(str(data["error"]))
            raise RuntimeError(str(data["error"]))
        return ((data.get("message") or {}).get("content") or "").strip()

    # ---- OpenAI 兼容 API ----
    def _call_openai(self, model: str, text: str) -> str:
        context = self._get_context(self._cfg.get_int("translate", "context_sentences"))
        messages = build_translate_messages(
            context, text, self._source_lang, self._target_lang,
            self._cfg.get("translate", "topic"),
            self._cfg.get("translate", "glossary"),
            auto_correct=self._cfg.get_bool("translate", "auto_correct"))
        base = self._cfg.translate_base_url().rstrip("/")
        headers = {"Content-Type": "application/json"}
        api_key = self._cfg.translate_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": self._cfg.get_float("translate", "temperature"),
            "max_tokens": self._cfg.get_int("translate", "max_tokens"),
        }
        if model.lower().startswith("qwen"):
            # qwen3.x 默认开启思考模式：一句话要 7~23 秒，而且思考 token 会吃光
            # max_tokens（导致正文为空）。翻译是确定任务，必须关掉。
            payload["enable_thinking"] = False
        try:
            resp = self._http.post(
                f"{base}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._cfg.get_float("translate", "timeout_sec"),
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise _ServiceDown("翻译服务不可达（请检查地址与网络）") from exc
        except requests.exceptions.ConnectionError as exc:
            raise _ServiceDown("翻译服务连接失败（请检查地址/端口/网络）") from exc
        except requests.exceptions.ReadTimeout as exc:
            raise _ServiceTimeout("翻译超时（服务繁忙或网络慢）") from exc
        except requests.exceptions.Timeout as exc:
            raise _ServiceTimeout("翻译请求超时") from exc
        except requests.exceptions.RequestException as exc:
            raise _Retryable(f"翻译请求失败: {exc}") from exc
        if resp.status_code == 400:
            msg = _err_text(resp)
            low = msg.lower()
            if "arrear" in low or "overdue" in low or "欠费" in msg:
                raise _AuthError(
                    "百炼账户欠费或被停用（400 Arrearage）。请到阿里云百炼控制台结清欠费、"
                    "确认免费额度；或换一个可用的 API Key（设置 → AI 模型）")
            if "data_inspection" in low or "inappropriate" in low or "content" in low and "filter" in low:
                raise _ContentBlocked(f"内容审核未通过，这句保留原文（{msg[:80]}）")
            raise _Retryable(f"翻译请求被拒绝（400）：{msg[:160]}")
        if resp.status_code == 401:
            raise _AuthError("API Key 无效（401），请在 设置→AI 模型 里更新")
        if resp.status_code == 403:
            msg = _err_text(resp)
            low = msg.lower()
            if "quota" in low or "free tier" in low or "额度" in msg:
                raise _QuotaError(f"模型 {model} 免费额度已用尽（403）")
            raise _AuthError(f"访问被拒绝（403）: {msg[:120]}")
        if resp.status_code == 404:
            raise _ModelGone(f"模型 {model} 不存在或接口路径错误（404，"
                             f"期望 {base}/v1/chat/completions）")
        if resp.status_code == 429:
            raise _Retryable("请求过于频繁（429），稍后自动重试")
        if resp.status_code >= 500:
            raise _Retryable(f"翻译服务暂时不可用（{resp.status_code}）")
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            msg = str(data["error"])
            if _is_oom(msg):
                raise _OOM(msg)
            raise RuntimeError(msg)
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"响应格式异常: {data}") from exc
