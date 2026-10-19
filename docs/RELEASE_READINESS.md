# 依赖与发行范围

源码运行需要 Windows、Python 3.12 和 requirements.txt 中的依赖。该清单是开发基线，尚不是跨机器验证的完整锁文件。

## 外部组件

按功能使用 FFmpeg/ffprobe、MKVToolNix、ffsubsync、可选 alass、Ollama/Qwen 模型、Tesseract、pgsrip、Subtitle Edit seconv、RapidOCR/ONNX Runtime、OpenCC、PySide6/Qt、NumPy、PyAV、OpenCV、Pillow 和 pysrt。源码仓库不提供其二进制或模型；用户自行准备并配置路径。

OpenSubtitles、SubDL 需要用户自己的 API Key。设备授权模块保留，示例配置不连接正式授权服务。源码启动与已发布商业安装包的许可/配置方式不同。

本仓库仅附五款运行所需的原样开源字体，完整来源、SHA-256、版权及 OFL 文件见 [字体说明](THIRD_PARTY_FONTS.md)。Windows 自带字体不复制或分发。

## 已验证与边界

2.0.82 完整包已在开发机器构建、覆盖安装并冷启动核验；全部 5,418 个主程序文件与构建清单匹配。构建和安装运行相同的 947 项桌面冻结回归，另有独立命令行和安装布局检查。测试范围见 [测试说明](TESTING.md)。这不是全新 Windows 环境的完整复现证明，也不保证任意影片的字幕、翻译或封装都能成功。

项目级许可证仍待作者选定。第三方组件的版本、许可和分发义务需继续按发行物核对；公开源码不替代这些义务。商业构建脚本、二进制供应链和真实影片夹具未在本仓库公开，不能据此声称可重建与下载包逐字节相同的安装器。
