# SubFlow

SubFlow 是 Windows 影片字幕与音轨整理工具：批量分析影片，寻找并核验字幕，按偏好补齐语言，生成整理后的新文件。主视频保留原编码，原片保留。

当前版本：**2.0.82 Beta**。本仓库同步正式版本的必要源码、界面资源和测试，保留少量公开版便携路径处理。项目级许可证尚未选定；公开源码不等于已授予任意再分发许可，第三方组件遵循各自许可。

## 下载

- [Windows 完整安装包](https://freezhang.com/downloads/Setup-SubFlow-2.0.82-Beta-Full.exe)（2,440,547,288 字节，约 2.27 GiB）
- [激活码购买（爱发电，¥69 一次买断）](https://afdian.com/a/subflow)：安装后在爱发电私信我领取激活码
- [产品与图文说明](https://freezhang.com/products/subtitle-track-tool-pro-max/)
- [安装说明](https://freezhang.com/downloads/SubFlow-2.0.82-README.txt) · [SHA-256 校验文件](https://freezhang.com/downloads/SubFlow-2.0.82-SHA256.txt) · [更新记录](CHANGELOG.md)


完整包 SHA-256：

```text
6BDEF7A08ED7E311B87E9A67BA70B493C5EFE35AD085C652DA90C3990C20E93D
```

完整包包含主程序和运行工具，翻译模型约 5 GB，按需另行准备。源码仓库不存放安装器、模型或工具二进制。安装包首次使用和已有用户升级的具体要求以安装说明为准。

## 主要功能

- 按四套独立偏好整理字幕语言和音轨，优先复用可用的内嵌文本字幕；必要时搜索 OpenSubtitles、SubDL。
- 结合影片 Hash、片名、年份、作品 ID 和完整别名筛选同片来源；自动下载前核对影片身份。明确的评论、花絮和片段字幕不当作完整正片对白。
- 自动检查和修正字幕整体偏移；证据不足时保留候选，人工核听前、中、后三段。核听可切换参照音轨。
- 参照音轨先排除明确非正片用途，再优先使用成本较低的合适音轨；多份不同时间轴核验失败后，仅复核一次主音轨。
- 本地翻译补齐缺少的语言，保留有效缓存；部分生成异常可受限拆批恢复，失败原因与待检查译文可查看。
- 文本字幕支持字体、颜色和自动字号。按可见字高适配字体大小，整理普通对白换行与完全重复事件，保留说话人和定位边界；内置五款开源字体并按需补字。
- 默认输出 MKV，可在分析前选择 MP4。MKV 支持图片字幕与字体附件；MP4 使用普通文本字幕，样式与图片字幕有兼容限制。发布成品前检查封装结果。

时间模式匹配与作品身份确认都不等于逐句语义正确。字幕剪辑不一致、复杂漂移或证据不足仍可能失败；未标明的评论轨也不能保证自动识别。本地模型可能误译，保留人工检查入口。2.0.82 没有放宽原时间核验门槛，也没有新增识别模型。

## 源码运行

开发基线为 Windows + Python 3.12。以下为源码启动方式；尚未声称可在全新 Windows 环境一键复现完整商业安装包：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item product_config.example.json product_config.json
.\.venv\Scripts\python.exe -B qml_frontend\main.py
```

按功能另行准备 FFmpeg/ffprobe、MKVToolNix、Ollama 和翻译模型，修改示例中的相对工具路径。OCR 路线另需相应工具。字幕服务使用用户自己的 API Key。示例不含服务凭据且关闭设备授权连接；请勿强制提交个人 `product_config.json`、授权或字幕服务设置。

`requirements.txt` 是开发环境依赖基线，并非完整锁文件；外部工具和字体约束见 [依赖说明](docs/RELEASE_READINESS.md) 与 [字体来源和许可](docs/THIRD_PARTY_FONTS.md)。

## 仓库内容与验证

包含必要 Python/QML 源码、图标、五款原样 OFL 字体及授权、示例配置和测试。个人配置、真实影片/下载字幕、时间夹具、缓存、模型、安装包、私人构建部署脚本与诊断历史均不纳入提交。

正式 2.0.82 构建版与安装版分别通过相同的 947 项桌面冻结回归；不能加成 1,894 个不同测试，也不代表所有影片都能通过。公开源码的检查范围、外部工具要求与排除项见 [测试说明](docs/TESTING.md)。

## 反馈

欢迎通过 GitHub Issues 反馈系统版本、处理目标、出错阶段及必要日志。提交前隐藏私人路径、API Key、授权码与账号信息。
