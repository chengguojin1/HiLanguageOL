# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 打包配置：HiLanguageOL 实时翻译助手

用法（在项目自带的虚拟环境里执行）：
    .venvScriptspyinstaller.exe HiLanguageOL.spec --noconfirm
产物：dist/HiLanguageOL/HiLanguageOL.exe
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
project_root = Path(SPECPATH)  # 跟随 spec 所在目录，不再写死盘符

# 收集 google.protobuf 所有子模块（struct_pb2 等 proto 生成代码依赖）
_protobuf_submodules = collect_submodules('google.protobuf')

# 只打包"模板"配置；用户自己的 config.ini（含 API Key）绝不能进包
datas = [
    # 打包默认配置模板（首次运行时复制到 %APPDATA%\HiLanguageOL）
    (str(project_root / 'config.example.ini'), '.'),
    # 应用图标（窗口/托盘运行时读取）
    (str(project_root / 'app' / 'icon' / 'app.ico'), 'app/icon'),
    # Yandex SpeechKit gRPC 生成的 proto Python 代码（运行时导入需要）
    (str(project_root / 'app' / 'yandex_proto'), 'app/yandex_proto'),
    # 许可证与第三方声明：随二进制一起分发，满足 GPL 的告知义务
    (str(project_root / 'LICENSE'), '.'),
    (str(project_root / 'THIRD-PARTY-NOTICES.md'), '.'),
]
_bad = [src for src, _ in datas if Path(src).name == 'config.ini']
if _bad:
    raise SystemExit(f"安全护栏：禁止把用户配置文件打进包 -> {_bad}")

# 模板里的 api_key 必须为空（用 configparser 正规解析，避免行尾/空格差异导致误判）
import configparser as _configparser
_cp = _configparser.ConfigParser()
_cp.read(project_root / 'config.example.ini', encoding='utf-8')
_leaks = [s for s in ('asr', 'translate', 'yandex')
          if _cp.has_section(s) and (_cp.get(s, 'api_key', fallback='') or '').strip()]
if _leaks:
    raise SystemExit(f"安全护栏：config.example.ini 的 api_key 必须留空，但发现非空配置段 -> {_leaks}")

a = Analysis(
    [str(project_root / 'main.py')],
    # app/yandex_proto 是运行期通过 sys.path 注入的"裸包"（yandex.* / google.api / google.rpc），
    # 不告诉 Analysis 去哪里找，PyInstaller 就会报一堆 Hidden import not found，Yandex 识别也就打包不全。
    pathex=[str(project_root),
            str(project_root / 'app' / 'yandex_proto')],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # 采集用（audio.py 里是函数内 import，显式声明更稳）
        'pyaudiowpatch',
        'soundfile',
        'websocket',
        'numpy',
        'requests',
        # Yandex SpeechKit gRPC
        'grpc',
        'grpc_tools',
        'google.protobuf',
    ] + _protobuf_submodules + [
        'yandex.cloud.ai.stt.v3.stt_pb2',
        'yandex.cloud.ai.stt.v3.stt_pb2_grpc',
        'yandex.cloud.ai.stt.v3.stt_service_pb2',
        'yandex.cloud.ai.stt.v3.stt_service_pb2_grpc',
        'yandex.cloud.api.operation_pb2',
        'yandex.cloud.operation.operation_pb2',
        'yandex.cloud.validation_pb2',
        'google.api.http_pb2',
        'google.api.annotations_pb2',
        'google.rpc.status_pb2',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'pandas',
        'PIL',
        'pytest',
        'sounddevice',   # 未使用（历史遗留），显式排除避免误打包
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HiLanguageOL',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # 不显示控制台窗口
    icon=str(project_root / 'app' / 'icon' / 'app.ico'),          # exe 图标
    version=str(project_root / 'version_info.txt'),               # Windows 版本信息
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HiLanguageOL',
)
