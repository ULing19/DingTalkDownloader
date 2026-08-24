# 第三方组件与来源说明

本文件记录 1.3.5 发布包中随软件使用或再分发的主要组件。各组件仍由其原作者负责维护；本项目不把第三方组件的许可扩大为钉钉官方授权。

## GoDingtalk

- 项目：<https://github.com/NAXG/GoDingtalk>
- 发布组件：`GoDingtalk_v2.5.2_windows_amd64.exe`
- 用途：群直播回放的登录、解析、下载和分片处理。
- 归属：本 GUI 通过子进程调用该引擎，未修改其源代码。
- 许可核对：上游 README 曾提到 MIT，但仓库当前是否随每个 Release 附带独立 `LICENSE` 文件需要以其最新仓库和发布页为准。再分发时请保留上游说明，不要仅凭本项目 README 推断许可。

## MediaGo v0.3.0

- 项目：<https://github.com/Sophomoresty/mediago>
- 固定版本：`v0.3.0`，Windows amd64 发布资产 `mediago_0.3.0_windows_amd64.zip`
- 用途：钉钉闪记 `/app/transcribes/<uuid>` 和 CSpace/钉盘视频预览媒体的解析。
- 上游许可证：仓库 API 和 `LICENSE` 文件标注 **The Unlicense**（公共领域放弃声明，按适用法律执行）。
- 发布包校验：v0.3.0 Windows amd64 ZIP 的 SHA-256 为
  `2d88d1741815382d6fc79cf1aeaadd261ae4ba9f3d44a56f9efa1f8d3379e98c`。
- 来源风险：上游钉钉 extractor 的源码注释写明其 LWP 客户端由反编译的 `Dingtalk_Live_Client.pyc` 移植。Unlicense 只覆盖上游作者能够授予的权利，不能替第三方代码、平台接口、协议或数据内容提供授权保证。发布者应在部署前自行完成来源和合规审查。
- 兼容边界：本项目只使用其公开 Release 做兼容集成；钉钉接口、登录态和云盘权限变化可能导致功能失效。群文件普通文档/压缩包只有在解析器返回直接下载地址时才会按原始扩展名保存；仅有预览地址的文件仍可能无法下载。

## FFmpeg

- 项目：<https://ffmpeg.org/>
- 用途：合并 HLS/TS 分片、输出 MP4 或其他容器。
- 许可：FFmpeg 可按 LGPL 或 GPL 构建，实际义务取决于随包二进制的构建配置和所启用库。发布者应在发布页或包内保留对应版本的许可证、构建信息和源码获取方式。
- 本项目不声称 `ffmpeg.exe` 的构建配置与 FFmpeg 官方发行版完全一致；请以该二进制随附信息为准。

## Python 图形界面依赖

源码运行会使用以下 PyPI 包；它们不一定作为独立文件复制到安装包中：

| 包 | 用途 | 许可核对 |
| --- | --- | --- |
| CustomTkinter | 图形控件和主题 | 以 PyPI/上游仓库当前许可证为准 |
| Pillow | 图片读取和二维码预处理 | 以 PyPI/上游仓库当前许可证为准 |
| opencv-python-headless | 二维码识别 | 以 PyPI/上游仓库当前许可证及其二进制依赖说明为准 |
| pyzbar / ZBar | 遮挡二维码的备用识别 | pyzbar 0.1.9 按 MIT 发布；随 Windows wheel 提供的 ZBar 动态库按 LGPL-2.1 发布 |
| websocket-client | 连接钉钉只读 LWP 回放列表接口 | Apache License 2.0 |

发布包额外附带 `PYZBAR-LICENSE.txt`（pyzbar MIT）、`ZBAR-LICENSE.txt`（ZBar LGPL-2.1）、`LIBICONV-NOTICE.txt`（随 pyzbar Windows wheel 一起分发的 libiconv 动态库来源与许可证说明）和 `WEBSOCKET_CLIENT_LICENSE.txt`（Apache-2.0）。ZBar 和 libiconv 仅作为二维码识别依赖，不属于钉钉官方 SDK。动态库保持独立文件随包分发，便于按 LGPL 条款替换或重新链接。

安装或再分发源码环境时，请保留各包的版权和许可证文件，不要把本项目 MIT 许可证套用到这些依赖。

## Windows、浏览器授权与当前群链接采集器

- `dingtalk_replay_extractor.py` 从钉钉 CEF 日志识别当前群，优先使用登录态下的只读回放列表 RPC 自动翻页；旧版客户端或 RPC 不可用时，再回退读取当前直播广场渲染进程的已提交可读内存。
- `replay_link_collector.py` 负责在用户选择的群资料目录下发现或记忆群目录，并通过原子写入保存 `链接集.txt`。
- 采集器不注入进程、不模拟点击、不发送消息；仅在本机登录态下调用前述回放列表只读 RPC，不调用写入类 RPC。页面或分页证据不完整时不会覆盖原文件。

某些引擎可以使用用户明确提供的 Netscape Cookie 文件或浏览器授权状态。Cookie 属于账号凭据，不是第三方组件授权；本项目不会索取密码、代导出 Cookie 或绕过登录。发布问题日志前必须删除 Cookie、账号标识、私密 URL 参数和文件名。

## 本项目

- 项目：<https://github.com/ULing19/DingTalkDownloader>
- 自有 GUI 代码：MIT License，见仓库根目录 `LICENSE`。
- 本项目仅负责 GUI、任务队列、文件布局和发布包装；上列引擎的代码、商标、接口及数据权利归其各自权利人所有。

## 合规提示

请只下载本人或所属组织明确授权访问的直播、闪记和钉盘视频。平台的接口调用频率、地域、账号权限和服务条款可能限制自动化访问；遇到 401/403、权限不足或接口变化时，应通过正常登录、管理员授权或官方渠道解决，不要尝试绕过限制。
