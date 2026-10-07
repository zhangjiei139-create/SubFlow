# SubFlow

Windows 桌面影片字幕及音轨整理工具。将轨道分析、字幕查找、时间纠偏、语言补齐、人工核听和重新封装组合为批处理流程，主视频以保留原编码为原则。

**当前是用于清理和评审的私有源码快照，不是已准备完成的公开发行版。**项目级许可证尚未选定；没有授予公开使用或再分发的项目级许可。第三方组件仍按各自许可管理。

## 功能

- 按偏好整理音轨和字幕语言，优先使用可用的内嵌文本字幕。
- OpenSubtitles 和 SubDL 英文字幕搜索、用途与身份筛选、候选核验。
- 基于连续 VAD 的自动对时，默认中间50%，证据不足再扩到60%、75%。
- 使用已纠偏文本检查保留的图片字幕，局部读取、增量扩展及受限后备。
- 前中后三个60秒窗口的人工核听和整排字幕调整。
- Ollama 本地翻译、简繁转换、缓存及可检测的中文漏译验收。
- MKVToolNix 封装、有限条件的 TrueHD 快速直通及输出验证。

时间模式匹配不证明台词内容正确；翻译验收也不保证语义准确。复杂剪辑差异和特殊格式仍可能拒收或需要人工处理。

## 开发启动

使用 Windows 和 Python 3.12。先创建独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item product_config.example.json product_config.json
.\.venv\Scripts\python.exe -B qml_frontend\main.py
```

配置中的工具路径是仓库相对路径示例，也可以改成自己电脑的路径。`product_config.json` 已忽略，不应提交个人配置。开发示例关闭设备授权并清空授权服务地址；原商业发行代码没有被删除，最终社区版与发行策略仍待决定。

仅安装 Python 依赖不足以处理影片。按所需功能另外准备 FFmpeg/ffprobe、MKVToolNix、Ollama；OCR 需要相应的 Tesseract/seconv 等。可执行文件放在配置指定的 `tools` 目录，或配置独立安装位置。ffsubsync 可以由 pip 安装；可选 alass 放在 `tools/alass/bin/alass-cli.exe`。

`requirements.txt` 是开发环境基线，不是经过全新电脑复现的完整依赖锁文件。部分依赖仍未固定版本，后续需要验证后锁定。首次模型下载不包含在仓库中。界面字体未打包，启动时使用系统字体回退。

## 测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py"
```

仓库保留不依赖真实影片文件的测试和模拟数据。三个依赖实际电影时间区间夹具的测试暂未纳入，真实夹具没有复制；商业安装器布局测试也暂未纳入，因为本快照没有包含其构建脚本。测试覆盖不等于全部片源和硬件都已验证。

## 仓库范围

只包含源码、必要的界面图标、示例配置、说明及离线测试。电影、下载字幕、翻译缓存、模型、工具二进制、安装包、历史截图和部署记录不在此仓库。个人工具路径已在此副本中清理；原开发目录不受影响。

目前没有纳入原商业安装器的完整构建脚本或安装产物。公开前需要另外完成可复现构建、依赖及资源许可盘点。

详见 [依赖与公开前检查](docs/RELEASE_READINESS.md) 和 [源码清理说明](docs/SOURCE_CLEANUP.md)。
