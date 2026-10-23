# 贡献指南

欢迎提 Issue 和 PR。这个项目是个人维护的，回复可能不及时，但都会看。

## 开发环境

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

需要 Windows（音频采集依赖 WASAPI，`PyAudioWPatch` 仅 Windows 可用）。

## 提交前

1. 跑一遍冒烟测试（不需要真机音频，不会发网络请求）：

   ```bat
   .venv\Scripts\python tools\dev\smoke_ui.py
   ```

2. 如果改了界面，跑一下截图脚本看效果：

   ```bat
   .venv\Scripts\python tools\dev\preview_app.py
   ```
   输出在 `tools/dev/out/`（已 gitignore，不会提交）。

3. 确保没把 `config.ini`、日志、录音、`dist/` 带进提交。

## 代码约定

- Python 3.10+，类型标注能用就用，不加过度抽象。
- 注释和提示文案用中文；标识符用英文。
- 后台线程不要直接碰 Qt 控件，统一走信号回主线程（参考 `Controller` 里的写法）。
- 网络请求必须有超时；错误要分级（可重试 / 鉴权失败 / 额度耗尽 / 模型不存在），别把所有异常都吞成一句"失败"。

## PR

- 一个 PR 只做一件事，说明"改了什么、为什么"。
- 涉及界面改动的，附一张改动前后的截图。
- 提交即表示你同意以 **GPL-3.0** 授权你的贡献。

## 不接受的改动

- 引入遥测、统计上报、自动上传录音或转写内容。
- 把 API Key 写进代码或示例配置。
- 改变 GPL-3.0 许可的改动（例如附加限制条款）。
