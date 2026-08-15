钉钉回放下载器 @VERSION@
本项目: https://github.com/ULing19/DingTalkDownloader
上游引擎: https://github.com/NAXG/GoDingtalk
MediaGo: https://github.com/Sophomoresty/mediago/releases/tag/v0.3.0

1. 双击 DingTalkDownloader.exe
2. 首次运行点击“重新登录”并完成钉钉授权
3. 粘贴群回放、闪记或群文件链接
4. 选择保存目录并点击“开始下载”

也可以先在钉钉目标群的“直播广场”切换到“全部”、滚动到末页，点击“获取当前群回放链接”。
首次使用时选择任意可写的保存根目录，再选择群文件夹；链接会保存为该文件夹中的“链接集.txt”，并加入任务列表。
该功能只读钉钉页面数据；页面未稳定或保存文件夹不可写时不会覆盖旧文件。

请保持 DingTalkDownloader.exe、GoDingtalk、mediago.exe 和 ffmpeg.exe 在同一目录。
登录会话保存在 .goDingtalkConfig\，默认下载目录为 video\。
同名下载会自动追加编号（例如 `(1)`、`(2)`），不会覆盖已有视频。
中文完整说明: 使用说明.txt
采集功能说明: 回放链接一键获取说明.txt
