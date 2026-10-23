# 第三方组件与许可证

本项目以 **GPL-3.0** 发布，下面是随程序分发（或参与构建）的第三方组件及其许可证。
分发本项目的二进制包时，请一并保留本文件。

## 运行时依赖

| 组件 | 版本（开发环境） | 许可证 | 用途 |
|---|---|---|---|
| [PyQt6](https://pypi.org/project/PyQt6/) | 6.11 | **GPL-3.0-only**（或商业授权） | 界面框架 |
| [grpcio](https://pypi.org/project/grpcio/) | 1.84 | Apache-2.0 | Yandex gRPC 识别 |
| [protobuf](https://pypi.org/project/protobuf/) | 7.x | BSD-3-Clause | gRPC 消息编解码 |
| [numpy](https://pypi.org/project/numpy/) | 2.5 | BSD-3-Clause（含 0BSD/MIT/Zlib/CC0 片段） | 音频与图像处理 |
| [soundfile](https://pypi.org/project/soundfile/) | 0.14 | BSD-3-Clause | WAV 读写（内含 libsndfile，LGPL-2.1） |
| [requests](https://pypi.org/project/requests/) | 2.34 | Apache-2.0 | 翻译服务 HTTP 调用 |
| [websocket-client](https://pypi.org/project/websocket-client/) | 1.9 | Apache-2.0 | 阿里云流式识别 WebSocket |
| [PyAudioWPatch](https://pypi.org/project/PyAudioWPatch/) | 0.2.12 | Apache-2.0 | WASAPI 回环 + 麦克风采集 |
| [certifi](https://pypi.org/project/certifi/) / charset-normalizer / idna / urllib3 | — | MPL-2.0 / MIT / BSD / MIT | requests 的间接依赖 |

> **关于 PyQt6**：PyQt6 采用 GPLv3 与商业授权双许可。本项目按 GPLv3 使用其开源授权，因此整个程序（含本仓库代码）以 GPLv3 分发。若你需要闭源分发，必须向 [Riverbank Computing](https://riverbankcomputing.com/commercial-license) 购买商业授权，或改用 PySide6（LGPLv3）。

## 随包分发的协议定义

| 来源 | 许可证 | 位置 |
|---|---|---|
| [Google APIs proto](https://github.com/googleapis/googleapis)（`google/api`、`google/rpc`） | Apache-2.0 | `proto/google/`、`app/yandex_proto/google/` |
| [Yandex Cloud API proto](https://github.com/yandex-cloud/cloudapi)（`yandex/cloud/...`） | Apache-2.0 | `proto/yandex/`、`app/yandex_proto/yandex/` |

这些 proto 文件由对应上游项目生成，仅用于协议定义，未做修改。

## 构建与打包工具

| 工具 | 许可证 | 说明 |
|---|---|---|
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later（含启动器例外） | 打包 exe；其例外条款允许打包闭源程序 |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | Inno Setup License（允许免费商用） | 生成安装包；安装包可自由分发 |

## 图标素材

`app/icon/` 下的图标为本项目自制/委托制作，随本项目按 GPLv3 分发。

## 依赖许可证全文

各依赖的完整许可证文本随对应 wheel 一起分发，也可在下列地址获取：

- GPL-3.0：<https://www.gnu.org/licenses/gpl-3.0.txt>
- Apache-2.0：<https://www.apache.org/licenses/LICENSE-2.0>
- BSD-3-Clause：<https://opensource.org/licenses/BSD-3-Clause>
- LGPL-2.1：<https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt>
