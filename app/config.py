"""配置模块：ini 读取、默认值合并、类型转换。"""

from __future__ import annotations

import configparser
import os
import sys
import shutil
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 打包后（PyInstaller）配置和数据存到 %APPDATA%\HiLanguageOL，避免 Program Files 只读问题
if getattr(sys, "frozen", False):
    APP_NAME = "HiLanguageOL"
    APP_DATA_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG = APP_DATA_DIR / "config.ini"
    # 首次运行时生成默认配置
    if not DEFAULT_CONFIG.exists():
        # 按优先级查找打包的 config.example.ini
        exe_dir = Path(sys.executable).parent
        candidates = [
            exe_dir / "config.example.ini",
            exe_dir / "_internal" / "config.example.ini",
            Path(getattr(sys, "_MEIPASS", "")) / "config.example.ini",
        ]
        copied = False
        for candidate in candidates:
            if candidate.exists():
                shutil.copy2(candidate, DEFAULT_CONFIG)
                copied = True
                break
        # 兜底：用内置默认值生成 config.ini
        if not copied:
            parser = configparser.ConfigParser()
            parser.read_dict(_DEFAULTS)
            with open(DEFAULT_CONFIG, "w", encoding="utf-8") as f:
                parser.write(f)
else:
    APP_DATA_DIR = PROJECT_ROOT
    DEFAULT_CONFIG = PROJECT_ROOT / "config.ini"

def resource_path(rel: str) -> Path:
    """随程序分发的只读资源路径（图标等）。打包后取 _MEIPASS，开发时取项目根目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = PROJECT_ROOT
    return base / rel


LANG_NAMES = {
    "ru": "俄语", "zh": "中文", "en": "英语", "auto": "自动检测", "none": "不翻译",
}

_DEFAULTS: dict[str, dict[str, str]] = {
    "audio": {
        "loopback_device": "auto",
        "mic_device": "auto",
        "sample_rate": "48000",
        "record_format": "wav",
        # auto/both：老师+我 两个通道都启用（谁有声音就翻谁，双通道有声音就双通道翻译）
        # teacher：只翻老师（回环）；me：只翻我（麦克风）
        "capture_mode": "auto",
        # 通道设备打开失败后的重试间隔（插上麦克风/切回设备后自动恢复）
        "device_retry_seconds": "10",
        # 连续静音多少秒后在状态栏提示"没听到声音"
        "silence_hint_seconds": "10",
    },
    "language": {
        "source_lang": "ru",
        "target_lang": "zh",
    },
    # ---- 语音识别服务（可与翻译服务完全不同的厂商/账号）----
    "asr": {
        # 识别引擎：aliyun（阿里云百炼 WebSocket）/ yandex（Yandex SpeechKit gRPC，俄罗斯本地稳定）
        "engine": "aliyun",
        "model": "qwen-audio-3.0-asr-flash-streaming",
        "ws_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        # 识别服务自己的 Key；留空则回退用 [translate] api_key（兼容旧配置）
        "api_key": "",
        "sample_rate": "16000",
        "format": "pcm",
        "frame_ms": "100",
        "max_sentence_silence": "1000",
        # 长句边识别边翻译：说到标点边界就先翻一段，不用等整句说完（体感延迟大幅下降）
        "chunk_stream": "true",
        "chunk_max_chars": "120",   # 没有标点时，最长攒到多少字符就切一段
        "chunk_min_chars": "30",    # 标点处至少要攒够多少字符才切（太短会译得不准）
        # WebSocket 心跳间隔（秒），0=不发心跳；长课堂防 NAT/代理断连
        "heartbeat_sec": "20",
        "reconnect_buffer_seconds": "2",
        "timeout_sec": "10",
    },
    # ---- Yandex SpeechKit（俄罗斯本地 ASR，从俄罗斯访问稳定）----
    "yandex": {
        # 服务账号 API Key（在 Yandex Cloud 创建服务账号，分配 ai.speechkit-stt.user 角色后获取）
        "api_key": "",
        # 语言代码：ru-RU / en-US / auto（自动检测）
        "language_code": "ru-RU",
    },
    "translate": {
        "engine": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
        # 2026-10 实测（5 句真实俄语课句 + App 原提示词，华北2）：
        #   速度：qwen-flash 565ms / qwen3.6-flash 881ms / qwen-plus 930ms / qwen3.6-plus 1.2s
        #   质量：qwen-plus ≈ qwen3.6-plus > qwen3.6-flash > qwen-flash
        #   （flash 会把「变化率」译成「变化速度」、丢掉语气词，只适合做兜底）
        # qwen3.x 默认开思考模式，一句话要 7~23 秒且吃掉 max_tokens —— translator 已自动
        #   加 enable_thinking=false，选 qwen3.x 才不会变慢/空回复。
        # 已知不可用：qwen-turbo 持续 429、qwen-mt-turbo 已下线(404)、qwen3.7-plus 免费额度耗尽。
        "model_name": "qwen-plus",
        "fallback_model_name": "qwen3.6-plus",
        # 翻译服务自己的 Key（识别服务用 [asr] api_key）
        "api_key": "",
        "topic": "",
        "glossary": "",
        "context_sentences": "5",
        "timeout_sec": "30",
        # 并发翻译句数（1=串行；2~3 可在语速快时不积压，字幕仍严格按顺序输出）
        "workers": "2",
        # 单句超过这么多秒还没翻完，先把原文显示出来（后台结果回来仍写进文件）
        "stuck_flush_sec": "12",
        "max_tokens": "256",
        "temperature": "0.2",
        "auto_correct": "true",
    },
    "ui": {
        "font_size": "10",
        "opacity": "85",
        # 玻璃皮肤：dark（深色）/ aurora（极光）
        "skin": "dark",
        # 字幕模式最多同时显示几段（1 段 = 一句原文 + 它的译文）
        "caption_segments": "3",
        # 字幕模式文字颜色：auto = 跟随底色自动切黑/白；也可写 #FFFFFF / #000000
        "caption_text_color": "auto",
        "bg_color": "#1A1B1C",
        "text_color": "#FFFFFF",
        "teacher_color": "#FFFFFF",
        "me_color": "#52C41A",
        "history_lines": "50",
        "show_my_translation": "true",
        "window_width": "480",
        "window_height": "220",
        # 上次关闭时的窗口位置（-1 = 没记录过，用系统默认位置）
        "window_x": "-1",
        "window_y": "-1",
        # 不翻译时静置多少分钟自动收起到托盘（0 = 从不）
        "auto_hide_minutes": "5",
    },
    "session": {
        "output_dir": "sessions",
        "file_prefix": "课堂",
        # 结束会话后是否自动生成纪要（默认关闭：结束后点工具栏「纪要」按钮手动生成）
        "auto_summary": "false",
        # 录音落盘间隔：崩溃时最多丢这么久的录音（原来 300 秒偏长）
        "flush_interval": "60",
        "summary_block_chars": "4000",
        "summary_model_name": "",
    },
}

_BOOLS = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}


class Config:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CONFIG
        self._parser = configparser.ConfigParser()
        self._parser.read_dict(_DEFAULTS)
        if self.path.exists():
            try:
                self._parser.read(self.path, encoding="utf-8")
            except configparser.Error as exc:
                raise RuntimeError(f"配置文件解析失败: {self.path} ({exc})") from exc
        for section in _DEFAULTS:
            setattr(self, section, _SectionProxy(self._parser, section))

    def get(self, section: str, key: str, fallback: str = "") -> str:
        if key == "api_key":
            if section == "asr":
                return self.asr_api_key()
            if section == "translate":
                return self.translate_api_key()
        try:
            return self._parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    # ---- 识别 / 翻译 两套服务各自独立（地址与 Key 都可分开配）----
    def asr_api_key(self) -> str:
        """识别服务 Key：HILANG_ASR_API_KEY > [asr] api_key > 回退 [translate] api_key。"""
        env = os.environ.get("HILANG_ASR_API_KEY")
        if env:
            return env
        key = (self._parser.get("asr", "api_key") or "").strip()
        if key:
            return key
        # 向后兼容：老配置只有 [translate] api_key，两个服务共用同一账号
        return self.translate_api_key()

    def translate_api_key(self) -> str:
        """翻译服务 Key：HILANG_TRANSLATE_API_KEY > HILANG_API_KEY > [translate] api_key。"""
        for env_name in ("HILANG_TRANSLATE_API_KEY", "HILANG_API_KEY"):
            env = os.environ.get(env_name)
            if env:
                return env
        return (self._parser.get("translate", "api_key") or "").strip()

    def asr_base_url(self) -> str:
        """识别服务地址（WebSocket 网关）。"""
        return (self._parser.get("asr", "ws_url") or "").strip()

    def translate_base_url(self) -> str:
        """翻译服务地址（OpenAI 兼容 / Ollama）。"""
        return (self._parser.get("translate", "base_url") or "").strip()

    def asr_key_source(self) -> str:
        """识别 Key 的来源说明（设置页提示用）。"""
        if os.environ.get("HILANG_ASR_API_KEY"):
            return "环境变量 HILANG_ASR_API_KEY"
        if (self._parser.get("asr", "api_key") or "").strip():
            return "[asr] api_key"
        if self.translate_api_key():
            return "回退到翻译服务 Key（[translate] api_key，建议单独配置）"
        return "未配置"

    def raw(self, section: str, key: str) -> str:
        """读取配置文件里的原始值（不走环境变量/回退逻辑，设置页编辑用）。"""
        try:
            return (self._parser.get(section, key) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def get_int(self, section: str, key: str) -> int:
        return self._parser.getint(section, key)

    def get_float(self, section: str, key: str) -> float:
        return self._parser.getfloat(section, key)

    def get_bool(self, section: str, key: str) -> bool:
        value = self._parser.get(section, key).strip().lower()
        if value not in _BOOLS:
            raise ValueError(f"[{section}] {key} 不是合法布尔值: {value!r}")
        return _BOOLS[value]

    def set(self, section: str, key: str, value: str) -> None:
        if not self._parser.has_section(section):
            raise KeyError(section)
        self._parser.set(section, key, value)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                self._parser.write(f)
        except OSError as exc:
            raise RuntimeError(f"配置写入失败: {self.path} ({exc})") from exc

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        if p.is_absolute():
            return p
        # 打包后相对路径基于 APP_DATA_DIR（%APPDATA%），开发时基于项目根目录
        return APP_DATA_DIR / p

    def source_lang(self) -> str:
        lang = self.get("language", "source_lang")
        return lang if lang != "auto" else "ru"

    def target_lang(self) -> str:
        return self.get("language", "target_lang")


class _SectionProxy:
    def __init__(self, parser: configparser.ConfigParser, section: str) -> None:
        self._parser = parser
        self._section = section

    def get(self, key: str) -> str:
        # 需要环境变量/回退逻辑的键（api_key）请用 Config.get(section, key) 或 asr_api_key()
        return self._parser.get(self._section, key)

    def __getattr__(self, key: str) -> Any:
        try:
            return self._parser.get(self._section, key)
        except (configparser.NoSectionError, configparser.NoOptionError) as exc:
            raise AttributeError(key) from exc
