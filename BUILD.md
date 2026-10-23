# HiLanguageOL 打包与发布说明

> 目标：一条命令产出「可直接分发的安装包」，并且**不会把 API Key 打进包**。

## 一、准备（只做一次）

1. **项目虚拟环境**（已存在则跳过）
   ```bat
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
2. **Inno Setup 6**（生成安装包用，非必须但推荐）
   下载：https://jrsoftware.org/isdl.php
   默认装到 `%LOCALAPPDATA%\Programs\Inno Setup 6` 或 `C:\Program Files (x86)\Inno Setup 6`，脚本会自动找到。

## 二、打包（以后每次就这一步）

**双击 `build.bat`** 即可，它会依次：

| 步骤 | 内容 |
|---|---|
| 1 | 环境自检：虚拟环境、PyInstaller（缺了自动装）、ISCC 路径、**程序是否正在运行**（在运行会问你关不关） |
| 2 | 显示当前版本，**直接回车 = 自动 +1**（如 1.0.2 → 1.0.3）；也可手填 `2.0.0`。写入 `installer.iss`（AppVersion + VersionInfoVersion）与 `version_info.txt` |
| 3 | PyInstaller 打包 → `dist\HiLanguageOL\HiLanguageOL.exe` |
| 4 | Inno Setup 编译 → `installer\HiLanguageOL-Setup-<版本>.exe` |
| 5 | **安检**：扫描发布产物里有没有 API Key 等敏感信息，不通过就拒绝发布 |

静默模式（不弹任何问题，适合脚本调用）：`build.bat /auto`

## 三、产物

```
dist\HiLanguageOL\HiLanguageOL.exe          ← 免安装版（整个 dist\HiLanguageOL 目录一起拷走即可运行）
installer\HiLanguageOL-Setup-1.0.0.exe      ← 安装包（推荐分发这个，38MB 左右）
```

## 四、升级与配置（重要）

- **升级安装不会清除配置**：安装包用同一个 `AppId`，直接覆盖安装；用户数据全在
  `%APPDATA%\HiLanguageOL\`，安装/升级/卸载都不动它。
- **AI 大模型配置在这里**（识别服务、翻译服务各自的地址与 Key）：
  - 安装版：`C:\Users\<用户名>\AppData\Roaming\HiLanguageOL\config.ini`
  - 源码运行：项目根目录的 `config.ini`
  - 程序里可在**托盘右键菜单 →「打开配置目录（API Key 在这里）」**直达。
- 发新版本：直接回车即可（版本号自动 +1，并记在 `installer.iss` 里），安装时 Windows 会识别为升级。

## 五、安全约定（已内置护栏）

1. `HiLanguageOL.spec` 里有硬校验：**若把 `config.ini` 列进打包清单、或模板 `config.example.ini` 的 `api_key` 非空，直接中断打包**。
2. 打包进包的是 `config.example.ini`（`api_key` 留空的模板），程序首次运行会复制到 `%APPDATA%\HiLanguageOL\config.ini`，用户自己填 Key。
3. `tools\check_package.py` 会扫描 `dist/` 与 `installer/` 全部文件，按 `sk-…` / `Bearer …` 特征匹配，命中即失败。
4. `config.ini` 已在 `.gitignore` 中，不会被提交到仓库。

## 六、常见问题

| 现象 | 处理 |
|---|---|
| 提示"HiLanguageOL 正在运行" | 关掉托盘里的程序（或让脚本自动关）后重试；程序在运行会锁住 `dist` 里的文件 |
| 提示找不到 ISCC.exe | 安装 Inno Setup 6；实在不装也能打包，只是不生成安装包 |
| 安检未通过 | 按输出的文件路径排查；最常见原因是把含 Key 的文件放进了 `config.example.ini` 或 `datas` |
| 打包后图标没变 | 换掉 `app\icon\` 下的 PNG 后重跑 build.bat（`app.ico` 已生成好，如需重新生成见 `tools`） |
| 在 Git Bash 里调用安装包卡住 | 加 `MSYS_NO_PATHCONV=1`，否则 `/VERYSILENT` 会被转成路径；双击运行不受影响 |

## 七、辅助脚本

| 脚本 | 用途 |
|---|---|
| `build.bat` | 一键打包（exe + 安装包 + 安检） |
| `tools\bump_version.py` | 版本号：`--show` 查看 / `--next` 下一个版本 / `--auto` 直接 +1 / `1.1.0` 指定 |
| `tools\check_package.py` | 发布产物敏感信息扫描 |
| `start.bat` | 源码方式启动程序（用项目自带 `.venv`） |
