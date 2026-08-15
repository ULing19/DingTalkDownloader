<p align="center">
  <img src="assets/download.svg" width="96" alt="下载图标">
</p>

<h1 align="center">钉钉回放下载器</h1>

<p align="center">
  面向 Windows 的中文图形化下载工具：整理钉钉群直播回放、闪记记录，以及群文件/钉盘中的媒体文件。
</p>

<p align="center">
  <a href="https://github.com/ULing19/DingTalkDownloader/releases">下载 1.2.0</a>
  ·
  <a href="https://github.com/NAXG/GoDingtalk">GoDingtalk 上游引擎</a>
  ·
  <a href="https://github.com/Sophomoresty/mediago/releases/tag/v0.3.0">MediaGo v0.3.0</a>
  ·
  <a href="https://github.com/ULing19/DingTalkDownloader/issues">反馈问题</a>
</p>

> 本项目不是钉钉官方软件。下载功能只适用于你本人有权观看或下载的内容；请遵守钉钉服务条款、版权法规和组织的使用规定。

## 界面预览

左侧输入链接，右侧查看任务和日志，底部统一控制登录、保存目录与 GitHub 仓库入口。预览图使用真实 GUI 截图，便于在下载前确认操作区域。

<p align="center">
  <img src="docs/images/gui-preview.png" width="900" alt="钉钉回放下载器界面预览">
</p>

## 支持范围

| 链接类型 | 示例格式 | 处理方式 | 版本说明 |
| --- | --- | --- | --- |
| 群直播回放 | `https://n.dingtalk.com/dingding/live-room/index.html?roomId=...&liveUuid=...` | 解析回放媒体并下载/合并 | 稳定入口 |
| 钉钉闪记 | `https://shanji.dingtalk.com/app/transcribes/<转写ID>` | 读取闪记记录中的可播放媒体 | 1.2.0 适配，需登录 |
| 群文件/钉盘文件 | `https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=<空间ID>&fileId=<文件ID>&type=file` | 读取 CSpace 元数据；视频走播放流，普通文件走直接下载地址 | 1.2.0 适配，需文件权限 |

闪记链接中的最后一段是记录 ID；群文件链接中的 `spaceId` 和 `fileId` 是云盘对象标识。不要把它们改写成 `roomId` 或 `liveUuid`，三类对象的后端接口不同。

### 非视频文件边界

群文件/钉盘视频会走播放流并按需要由 FFmpeg 合并。普通文档、表格、图片或压缩包在解析器返回**直接下载地址**时，也可以按响应 MIME、文件名或 URL 的扩展名原样保存（例如 `.pdf`、`.docx`、`.zip`），不会经过 FFmpeg，也不会被伪装成 MP4。只有预览地址、无直接下载地址、文件夹或权限不足的条目会提示“没有可下载媒体”；这不代表所有文件类型都得到平台保证。

## 工作流程

<p align="center">
  <img src="docs/images/workflow.png" width="900" alt="登录、导入、解析、下载四步流程图">
</p>

1. 点击“重新登录”，使用有权限的钉钉账号完成授权。
2. 粘贴链接，或导入文本文件、二维码图片；也可以先在钉钉直播广场点击“获取当前群回放链接”。
3. 点击“解析到任务列表”，先检查链接类型和任务标题。
4. 选择保存目录与线程数，点击“开始下载”；完成后打开保存目录查看文件。

登录只证明当前账号的访问能力，不会提升群成员权限。闪记记录可能只有音频或转写内容，群文件视频也可能已过期、被删除或被管理员限制下载。

### 当前群回放链接一键获取

主程序已经内置原“回放链接一键获取”功能，不需要另外启动 `G:\客户资料` 中的 EXE。使用前请：

1. 在钉钉打开目标群的“直播广场”，切换到“全部”，清空搜索条件。
2. 如果列表有分页或懒加载，滚动到列表底部并等待最后一页完成。
3. 回到下载器，点击输入区下方的“获取当前群回放链接”。
4. 程序会把识别到的链接加入左侧输入框和右侧任务列表，并保存到 `G:\客户资料\<群文件夹>\链接集.txt`。

首次识别某个群时选择对应群文件夹；之后会按群 ID 自动记忆。保存位置只能是 `G:\客户资料` 目录下的已有文件夹。若磁盘未连接、页面不是直播广场、列表仍在加载或无法确认群身份，程序会提示错误并且不会覆盖原有 `链接集.txt`。

该功能只读钉钉 CEF 日志和当前直播广场渲染进程内存，不点击群聊、不发送消息、不调用钉钉 RPC；请仍然只处理你有权访问的内容。

## 安装与发布包

### 安装版（推荐）

从 [Releases](https://github.com/ULing19/DingTalkDownloader/releases) 下载：

```text
DingTalkDownloader_1.2.0_Setup.exe
```

安装向导会把 GUI、GoDingtalk、MediaGo、FFmpeg 和说明文件放在同一应用目录，并创建开始菜单项。向导默认勾选“创建桌面快捷方式”，可以取消。安装后可从“设置 → 应用”、开始菜单中的卸载项或安装目录的卸载程序移除软件。

卸载程序**始终保留** `video\` 和 `.goDingtalkConfig\`，避免误删视频与登录会话；确认不再需要时再手动删除这两个目录。Windows SmartScreen 可能提示“未知发布者”，请只从本仓库 Release 下载，并按 Release 页面核对 SHA-256。

### 绿色版

同一 Release 目录提供：

```text
DingTalkDownloader_1.2.0_Portable.zip
```

解压到任意位置即可使用，不写入系统安装项。请保持以下文件在同一目录：

```text
DingTalkDownloader.exe
GoDingtalk_v2.5.2_windows_amd64.exe
mediago.exe                    # MediaGo v0.3.0，Windows amd64
ffmpeg.exe
```

绿色版卸载方式是退出程序后删除整个解压目录；删除前请先备份 `video\` 和 `.goDingtalkConfig\`。

## 首次使用与授权

1. 启动程序并点击“重新登录”。
2. 在钉钉页面完成登录，确认账号能在浏览器中打开目标回放、闪记或群文件视频。
3. 返回 GUI，粘贴链接并解析。

登录会话只保存在本机 `.goDingtalkConfig\`，不会自动上传 GitHub 或发送给项目作者。不要把 `cookies.json`、浏览器 Cookie 导出文件、二维码截图中的私密链接提交到仓库或发给他人。会话过期、账号切换或权限变化后，请重新登录。

## 保存目录与输出

默认输出目录为程序目录下的 `video\`，可在界面中改为其他磁盘或按科目建立子目录，例如：

```text
video\数学-基础\
video\英语-强化\
```

直播回放通常先得到 TS/HLS 分片，再由 FFmpeg 合并为 MP4；闪记和钉盘视频按源媒体返回的播放结果下载。钉盘普通文件若有直接下载地址，则按原始扩展名保存，不调用 FFmpeg。文件名来自任务标题；不同链接即使标题相同，也会自动追加 `(1)`、`(2)` 等编号，保留每个下载结果，不覆盖已有文件。

## 从源码运行

环境：Windows 10/11、Python 3.9+。GUI 依赖见 `requirements-gui.txt`。

```powershell
python -m pip install -r requirements-gui.txt
python gui_downloader.py
```

从源码运行时需把以下运行时文件放在项目根目录（或程序查找的同目录）：

- `GoDingtalk_v2.5.2_windows_amd64.exe`：来自 [NAXG/GoDingtalk Releases](https://github.com/NAXG/GoDingtalk/releases)。
- `mediago.exe`：从 [Sophomoresty/mediago v0.3.0 Windows Release](https://github.com/Sophomoresty/mediago/releases/tag/v0.3.0) 解压得到，建议核对发布页校验和。
- `ffmpeg.exe`：来自 [FFmpeg 官方下载页](https://ffmpeg.org/download.html)。

构建脚本：

```powershell
build_exe.bat
build_installer.bat
```

构建结果位于 `dist\DingTalkDownloader_1.2.0\`，包含安装版所需文件和绿色版压缩包。应用、安装程序和桌面快捷方式共用 `assets\download.ico` 下载箭头图标；采集器源码 `dingtalk_replay_extractor.py` 和 `replay_link_collector.py` 会随主程序一起打包。

## 目录结构

```text
gui_downloader.py              # CustomTkinter 图形界面
GoDingtalk_v2.5.2_windows_amd64.exe
mediago.exe                    # MediaGo v0.3.0 Windows amd64
ffmpeg.exe
requirements-gui.txt
DingTalkDownloader.spec
installer\setup.iss            # 安装、快捷方式与卸载
assets\download.ico
docs\images\                  # 界面预览和流程图
video\                         # 默认输出目录
.goDingtalkConfig\             # 本机配置与登录会话
```

## 常见问题排查

**提示“未找到下载引擎”**：确认 `DingTalkDownloader.exe`、`GoDingtalk_v2.5.2_windows_amd64.exe`、`mediago.exe` 和 `ffmpeg.exe` 没有被拆到不同目录，也没有被杀毒软件隔离。

**提示“缺少 roomId 或 liveUuid”**：这是把普通闪记或群文件链接交给了旧版直播解析器。升级到 1.2.0，并保留完整 URL；不要手工拼接参数。

**闪记提示缺少 `account/access_token` 或 `deviceid`**：当前登录会话不完整或已失效。点击“重新登录”，并确认同一账号能在浏览器打开闪记页面。

**群文件提示“不支持 URL”**：确认使用的是 1.2.0 及以上版本，并保留 `route=previewDentry`、`spaceId`、`fileId` 和 `type=file` 参数。旧版 GoDingtalk/MediaGo 可能只认识直播 URL。

**群文件提示“没有可下载媒体”**：先在钉钉客户端用同一账号打开原链接。若客户端也提示无权访问，应由文件所有者或群管理员授权；程序不会绕过权限。能预览但仍失败时，再检查视频是否仍在转码，或普通文件是否确实提供直接下载地址。只有预览地址、没有直接下载地址的条目仍可能无法保存。

**只有 TS、没有 MP4**：确认同目录有 `ffmpeg.exe`，再重试失败任务；不要在下载过程中移动程序目录。

**二维码识别失败**：1.2.0 会先尝试 ZBar，再用 OpenCV 放大、补白边和灰度/反色识别，适合钉钉卡片中的小二维码或中央带播放按钮的二维码。仍失败时请使用原始清晰图片并保留二维码四周空白，也可以直接复制二维码打开后的完整 URL。

**“获取当前群回放链接”失败**：确认钉钉当前窗口停留在目标群“直播广场”，已切换“全部”并滚动到末页；确认 `G:\客户资料` 磁盘已连接且对应群文件夹已创建。列表仍在变化时请等待几秒后重试。失败不会覆盖旧的 `链接集.txt`。

## 上游引用、许可与来源说明

本项目的 GUI 代码通过同目录引擎完成下载，并引用：[NAXG/GoDingtalk](https://github.com/NAXG/GoDingtalk)。GoDingtalk 的许可、版权和发行条件以其仓库当前声明为准。

1.2.0 还集成了 [Sophomoresty/mediago v0.3.0](https://github.com/Sophomoresty/mediago)，其仓库标注 **The Unlicense**，用于闪记和 CSpace/钉盘媒体解析；同时内置当前群回放链接只读采集器。该项目钉钉实现的源码注释提到由反编译的 `Dingtalk_Live_Client.pyc` 移植；Unlicense 只涉及其作者能够授予的权利，不能自动解决第三方代码、平台接口或数据内容的权利问题。本项目仅按公开 Release 做兼容集成，不声称得到钉钉官方授权，也不保证上游接口长期稳定。

FFmpeg 的许可取决于发布构建配置，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。再分发时请保留各组件的许可证和来源说明。

## 隐私、版权与免责声明

- `.goDingtalkConfig\` 中可能含有登录会话，严禁提交到 GitHub 或公开分享。
- `video\`、链接文本、二维码截图和账号信息都属于用户数据；发布问题日志前请先脱敏。
- 只下载你有权访问的回放或文件，并自行确认组织授权、内容版权和平台规则。
- 项目作者不提供账号代登录、不索取密码或 Cookie，也不负责因平台策略、权限变化或误用造成的损失。

## 许可证

本项目自有 GUI 代码以 MIT License 发布，详见 [LICENSE](LICENSE)。GoDingtalk、MediaGo、FFmpeg 及其他组件分别遵循各自许可；完整归属和注意事项见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

当前 GUI/安装包版本：`1.2.0`。
