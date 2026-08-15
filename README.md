<p align="center">
  <img src="assets/download.svg" width="96" alt="下载图标">
</p>

<h1 align="center">钉钉回放下载器</h1>

<p align="center">
  面向 Windows 的中文图形化工具，用于整理和下载你有权访问的钉钉群直播回放、闪记记录，以及群文件/钉盘媒体。
</p>

<p align="center">
  <a href="https://github.com/ULing19/DingTalkDownloader/releases">下载发行版</a>
  ·
  <a href="https://github.com/NAXG/GoDingtalk">GitHub 上游仓库</a>
  ·
  <a href="https://github.com/Sophomoresty/mediago/releases/tag/v0.3.0">MediaGo</a>
  ·
  <a href="https://github.com/ULing19/DingTalkDownloader/issues">反馈问题</a>
</p>

> 本项目不是钉钉官方软件。请只处理你本人有权观看或下载的内容，并遵守钉钉服务条款、版权法规和所在组织的使用规定。

## 这份 README 怎么读

如果你只是想使用软件，按“快速开始”操作即可；如果你想学习实现方式，可以继续阅读“工作原理”和“从源码运行”。本文档中的链接参数、保存目录和账号信息均使用占位符，不包含作者或用户电脑上的真实数据。

## 项目定位

这个项目适合用来学习一个桌面下载工具如何把多个来源统一成一条任务流水线：

- 识别不同格式的 URL，并把它们分发给对应引擎；
- 复用本机登录态完成权限校验，不保存密码，也不绕过权限；
- 对 HLS/TS 分片调用 FFmpeg 合并，对普通文件保留原始扩展名；
- 将解析、下载、转换和日志展示放到图形界面中；
- 在不同链接生成同名文件时自动编号，避免覆盖已有结果。

## 界面预览

左侧输入链接，右侧查看任务和日志，底部统一控制登录、保存目录与 GitHub 仓库入口。

<p align="center">
  <img src="docs/images/gui-preview.png" width="900" alt="钉钉回放下载器界面预览">
</p>

## 支持的链接类型

| 类型 | 示例格式（仅示意） | 处理方式 | 使用条件 |
| --- | --- | --- | --- |
| 群直播回放 | `https://n.dingtalk.com/dingding/live-room/index.html?roomId=<房间ID>&liveUuid=<回放ID>` | 解析回放媒体，必要时下载分片并合并 | 账号能观看该回放 |
| 钉钉闪记 | `https://shanji.dingtalk.com/app/transcribes/<转写ID>` | 读取记录中的可播放媒体 | 需要登录和记录权限 |
| 群文件/钉盘 | `https://qr.dingtalk.com/page/yunpan?route=previewDentry&spaceId=<空间ID>&fileId=<文件ID>&type=file` | 读取 CSpace 元数据；视频走播放流，普通文件走直链 | 需要文件查看/下载权限 |

三类链接对应不同的后端对象。闪记链接中的最后一段是记录 ID，群文件链接中的 `spaceId` 和 `fileId` 是云盘对象标识，不要把它们改写成 `roomId` 或 `liveUuid`。

### 文件类型边界

群文件/钉盘视频通常通过播放流下载，并按需要由 FFmpeg 合并。PDF、Office、图片或压缩包只有在解析器拿到**直接下载地址**时才会按原始扩展名保存；预览地址、文件夹、权限不足或没有直链的条目会提示没有可下载媒体。闪记记录也可能只有音频或转写文本，不保证存在视频画面。

## 工作原理

<p align="center">
  <img src="docs/images/workflow.png" width="900" alt="登录、导入、解析、下载四步流程图">
</p>

一次下载大致经过以下步骤：

1. 用户在钉钉完成登录，程序复用本机授权状态检查访问权限。
2. GUI 读取粘贴文本、文本文件或二维码中的 URL，并做基本格式校验。
3. 解析器根据域名和参数选择 GoDingtalk、MediaGo 或钉盘文件处理分支。
4. 下载引擎取得媒体地址；HLS/TS 分片由 FFmpeg 合并，普通文件直接保存。
5. 任务列表更新进度和日志，输出文件按标题生成；如果标题重复则追加 `(1)`、`(2)` 等编号。

### 当前群回放链接采集

主程序内置“获取当前群回放链接”按钮，不需要额外运行独立采集器：

1. 在钉钉打开目标群的“直播广场”，切换到“全部”并清空搜索条件。
2. 如果列表使用分页或懒加载，滚动到列表底部，等待最后一页完成。
3. 回到下载器，点击输入区下方的“获取当前群回放链接”。
4. 首次识别某个群时，选择一个本机已有的群资料文件夹；之后程序会按群 ID 记忆位置。
5. 链接会加入输入框和任务列表，并写入所选群资料文件夹下的 `链接集.txt`。

采集器只读钉钉 CEF 日志和当前直播广场渲染进程的可读信息，不点击群聊、不发送消息、不调用钉钉 RPC。页面未稳定、群身份无法确认或保存目录不可访问时，不会覆盖原有链接文件。

## 快速开始

### 安装版（推荐）

从 [Releases](https://github.com/ULing19/DingTalkDownloader/releases) 下载 `DingTalkDownloader_1.2.0_Setup.exe`，按安装向导完成安装。向导可以创建桌面快捷方式和开始菜单项，安装后可从 Windows 设置、开始菜单卸载项或安装目录中的卸载程序移除软件。

卸载程序会保留 `video\` 和 `.goDingtalkConfig\`，以免误删下载结果与登录会话；确认不再需要时再手动删除这两个目录。Windows SmartScreen 可能显示“未知发布者”，请只从本仓库 Release 下载，并按发布页核对 SHA-256。

### 绿色版

同一 Release 提供 `DingTalkDownloader_1.2.0_Portable.zip`。解压到任意位置后双击 `DingTalkDownloader.exe` 即可使用，不写入系统安装项。以下运行时文件必须与主程序保持同一目录：

```text
DingTalkDownloader.exe
GoDingtalk_v2.5.2_windows_amd64.exe
mediago.exe
ffmpeg.exe
```

绿色版的卸载方式是退出程序后删除解压目录；删除前请备份 `video\` 和 `.goDingtalkConfig\`。

## 首次使用

1. 启动程序，点击“重新登录”，在钉钉页面完成授权。
2. 确认当前账号能在钉钉中打开目标回放、闪记或群文件。
3. 将完整 URL 粘贴到左侧输入区，或导入文本文件、二维码图片；每行一个 URL，`#` 开头的行会被忽略。
4. 点击“解析到任务列表”，检查链接类型、任务标题和保存目录。
5. 选择保存目录和线程数，点击“开始下载”；完成后点击“打开保存目录”。

登录只证明当前账号的访问能力，不会提升群成员权限。会话保存在本机 `.goDingtalkConfig\`，不会自动上传 GitHub 或发送给项目作者。不要公开 `cookies.json`、浏览器 Cookie 导出文件、二维码截图中的私密链接或带令牌的日志。

## 保存结果与重名规则

默认输出目录是程序目录下的 `video\`，也可以在界面中选择其他目录或建立子目录，例如：

```text
<保存根目录>\数学-基础\
<保存根目录>\英语-强化\
```

文件名来自任务标题。不同链接即使标题相同，也会保留每个下载结果：

```text
同名视频.mp4
同名视频 (1).mp4
同名视频 (2).mp4
```

程序会同时检查已存在的目标文件和下载中的临时文件，避免重试或多来源下载时相互覆盖。

## 从源码运行

环境要求：Windows 10/11、Python 3.9 或更高版本。GUI 依赖见 `requirements-gui.txt`。

```powershell
python -m pip install -r requirements-gui.txt
python gui_downloader.py
```

从源码运行时，请把以下运行时文件放在项目根目录（或程序查找的同目录）：

- `GoDingtalk_v2.5.2_windows_amd64.exe`：来自 [NAXG/GoDingtalk Releases](https://github.com/NAXG/GoDingtalk/releases)。
- `mediago.exe`：来自 [Sophomoresty/mediago v0.3.0 Windows Release](https://github.com/Sophomoresty/mediago/releases/tag/v0.3.0)。
- `ffmpeg.exe`：来自 [FFmpeg 官方下载页](https://ffmpeg.org/download.html)。

不要把登录配置、下载结果、真实二维码、Cookie 或包含私密参数的链接提交到 Git。建议先检查 `git status`，确认 `.goDingtalkConfig\`、`video\` 和个人测试文件未被加入提交。

## 构建与测试

```powershell
build_exe.bat
build_installer.bat
python -m pytest -q
```

构建脚本会生成 PyInstaller 输出、安装包和绿色版压缩包。发布前建议检查：

- 安装程序能创建快捷方式并在 Windows 设置中登记卸载项；
- 绿色版解压后四个运行时文件仍在同一目录；
- 登录、三类链接解析、二维码导入和同名文件编号均能完成最小链路测试；
- Release 资产只包含公开构建产物，不包含本机配置或测试数据。

## 目录结构

```text
gui_downloader.py              # CustomTkinter 图形界面与任务队列
dingtalk_media.py              # 媒体地址、文件名和输出处理
dingtalk_replay_extractor.py   # 当前群直播广场的只读链接识别
replay_link_collector.py       # 链接文件保存与位置记忆
GoDingtalk_v2.5.2_windows_amd64.exe
mediago.exe
ffmpeg.exe
requirements-gui.txt
DingTalkDownloader.spec        # PyInstaller 配置
installer\setup.iss            # 安装、快捷方式与卸载
assets\download.ico            # 应用图标
docs\images\                  # 界面预览和流程图
video\                         # 默认输出目录（本地数据）
.goDingtalkConfig\             # 本机配置与登录会话（本地数据）
```

## 常见问题

**提示“未找到下载引擎”**：确认主程序、GoDingtalk、MediaGo 和 FFmpeg 在同一目录，并检查安全软件是否隔离了其中的文件。

**提示“缺少 roomId 或 liveUuid”**：不要把闪记或群文件 URL 当成直播 URL；升级到 1.2.0，并粘贴完整原始链接。

**闪记提示缺少 `account/access_token` 或 `deviceid`**：登录会话不完整或已过期。点击“重新登录”，并确认同一账号能在浏览器打开闪记页面。

**群文件提示“不支持 URL”**：保留 `route=previewDentry`、`spaceId`、`fileId` 和 `type=file` 参数，不要手工改写查询参数。

**群文件提示“没有可下载媒体”**：先用同一账号在钉钉客户端打开原链接。若客户端也无权访问，请让文件所有者或群管理员授权；程序不会绕过权限。能预览但下载失败时，再检查转码状态或是否确实存在直接下载地址。

**只有 TS，没有 MP4**：确认同目录存在 `ffmpeg.exe` 且未被拦截，再重试失败任务；不要在下载过程中移动程序目录。

**二维码识别失败**：使用清晰原图并保留二维码四周空白；也可以直接复制二维码打开后的完整 URL。

**“获取当前群回放链接”失败**：确认钉钉停留在目标群“直播广场”，已切换“全部”并滚动到末页；确认所选保存文件夹仍存在且可写。列表仍在变化时等待几秒后重试，失败不会覆盖旧的 `链接集.txt`。

## 上游引用与许可证

本项目的 GUI 代码通过同目录引擎完成下载，并引用 [NAXG/GoDingtalk](https://github.com/NAXG/GoDingtalk)。1.2.0 还集成了 [Sophomoresty/mediago v0.3.0](https://github.com/Sophomoresty/mediago)，用于闪记和 CSpace/钉盘媒体解析；FFmpeg 用于分片合并和容器转换。

GoDingtalk、MediaGo、FFmpeg 及其他依赖分别遵循各自许可证。请阅读 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，再进行再分发。MediaGo 仓库标注 The Unlicense，但这不等同于钉钉官方授权，也不能自动解决第三方代码、平台接口或内容版权问题。

本项目自有 GUI 代码以 MIT License 发布，详见 [LICENSE](LICENSE)。

## 隐私、版权与责任边界

- `.goDingtalkConfig\` 可能包含登录会话，严禁提交到 GitHub 或公开分享。
- `video\`、链接文本、二维码截图、日志和文件名都可能包含个人或组织信息，发布前请脱敏。
- 只下载你有权访问的回放或文件，并自行确认组织授权、内容版权和平台规则。
- 项目作者不提供账号代登录、不索取密码或 Cookie，也不保证平台接口长期稳定。

当前 GUI/安装包版本：`1.2.0`。
