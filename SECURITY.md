# 安全策略

## 报告漏洞

请**不要**在公开 issue 里提交安全问题或任何密钥/录音内容。

- 优先：在 GitHub 仓库的 **Security → Report a vulnerability** 私密上报
- 或发邮件到：`737475099@qq.com`

请包含：影响版本、复现步骤、影响范围（例如"可读取配置文件中的 API Key"）。
我会在 7 天内确认，修复后会在 CHANGELOG 里注明。

## 密钥安全

本项目的 API Key 只保存在本机 `config.ini`：

- 打包版：`%APPDATA%\HiLanguageOL\config.ini`
- 源码运行：项目根目录 `config.ini`

如果怀疑泄露：

1. 立刻到服务商控制台**作废并重建 Key**
2. 更新本机 `config.ini`
3. 检查是否把 `config.ini` 提交进了 git（本项目默认 `.gitignore` 已忽略）

## 支持范围

只对最新发布版本提供安全修复。旧版本建议升级。
