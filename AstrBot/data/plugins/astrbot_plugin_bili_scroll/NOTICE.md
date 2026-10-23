# 来源与改造说明

大肥龙刷B站插件于 2026-10-23 改造，运行于 AstrBot，随本项目按 AGPL-3.0-or-later 发布；上游 MIT 版权通知及授权继续有效。

- 需求对应来源：[holoiallo/dsh-bili-scroll](https://github.com/holoiallo/dsh-bili-scroll)，阅读版本 `03beb9c38ba592926f80b74170c73b94f7124b3d`（0.2.0）。感谢 holoiallo 的候选池、公开评论反应评分、定时巡航和去重思路
- 原作者：[c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)，MIT，Copyright (c) 2026 c1overzz。原 MIT 全文保留在本目录 `LICENSE`
- 上游内嵌 [adoresever/bilibili-mcp](https://github.com/adoresever/bilibili-mcp)，MIT，Copyright (c) 2026 AGI_Ananas。阅读其公开视频信息及评论评分实现，许可保留在 `BILIBILI_MCP_LICENSE`

本插件重写了 AstrBot 事件、工具、控制面板、匿名 HTTP 客户端、逐收件人定时、真实送达记忆与发送前复核衔接。没有打包 DSH harness、Node MCP、B站登录、评论/私信/投稿写接口、Whisper、FFmpeg或上游自动安装脚本。

只读取公共标题、简介和可匿名获取的评论，不下载整段视频、不冒称已看完、不保证字幕可读。匿名搜索会访问B站公开搜索页取得临时游客 Cookie，由网站正常签发；无账号、不使用用户浏览器 Cookie、不写入磁盘。遇到权限、风控或限流时显示原因，搜索降级只在热门标题里匹配关键词，不把无关热门伪装成搜索结果。

视频、封面、简介和评论属于各自权利人；本仓库代码许可不授予对视频内容的额外权利。分享为 `https://www.bilibili.com/video/BV...` 原网址，不评论、不投稿，不代表B站官方合作。
