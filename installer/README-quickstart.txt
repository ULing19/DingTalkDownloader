钉钉回放下载器 @VERSION@
本项目: https://github.com/ULing19/DingTalkDownloader
上游引擎: https://github.com/NAXG/GoDingtalk
MediaGo: https://github.com/Sophomoresty/mediago/releases/tag/v0.3.0

1. 双击 DingTalkDownloader.exe
2. 首次运行点击“重新登录”并完成钉钉授权
3. 粘贴群回放、闪记或群文件链接
4. 选择保存目录并点击“开始下载”

也可以在钉钉登录并保持一个或多个目标群“直播广场”打开，然后点击“一键获取已打开群回放”。
程序会通过登录态只读接口自动读取每个群的全部分页；选择一个已有可写根目录后，会按群名建立目录，将链接保存为“链接集.txt”并加入任务列表。
该功能不会自动打开或枚举账号中未打开的群；群身份无法确认或保存目录不可写时不会覆盖旧文件。

请保持 DingTalkDownloader.exe、GoDingtalk、mediago.exe 和 ffmpeg.exe 在同一目录。
登录会话保存在 .goDingtalkConfig\，默认下载目录为 video\。
同名下载会自动追加编号（例如 `(1)`、`(2)`），不会覆盖已有视频。
若任务提示音视频轨结束时间异常，文件仍会保留；请先用原链接重试。
中文完整说明: 使用说明.txt
采集功能说明: 回放链接一键获取说明.txt
