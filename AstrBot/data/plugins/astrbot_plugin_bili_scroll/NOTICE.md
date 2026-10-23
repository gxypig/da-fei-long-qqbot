# 来源与改造说明

大肥龙刷B站插件于2026-10-23至2026-10-24改造，运行于AstrBot，随项目按AGPL-3.0-or-later发布；上游MIT/GPL版权通知与许可继续有效。

- 需求对应来源：[holoiallo/dsh-bili-scroll](https://github.com/holoiallo/dsh-bili-scroll)，阅读版本 `03beb9c38ba592926f80b74170c73b94f7124b3d`（0.2.0），参考候选池、赤石热评评分、巡航、去重与账号操作流程
- 原作者：[c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)，MIT，Copyright (c) 2026 c1overzz。原MIT全文保留在 `LICENSE`
- 上游内嵌 [adoresever/bilibili-mcp](https://github.com/adoresever/bilibili-mcp)，MIT，Copyright (c) 2026 AGI_Ananas。许可保留在 `BILIBILI_MCP_LICENSE`
- 接口结构与私信WBI签名参考 [bilibili-api-python 17.4.1](https://pypi.org/project/bilibili-api-python/17.4.1/) 发布源码（bilibili-api-python贡献者，GPL-3.0）。`account.py` 的 `sign_dm` 适配其 `utils/network.py` 签名；`actions.py` 的评论、私信、收藏请求结构参考其对应模块/API资料。原GPL许可全文保留为 `BILIBILI_API_LICENSE`，完整适配源码随本插件提供；不引入该SDK的风控模拟、自动验证或重试代码，也不安装整个SDK

本插件重写AstrBot事件、工具、控制面板、HTTP客户端、逐收件人定时、真实送达记忆与发送前复核衔接。1.2移植上游赤石评分、召回、扫码登录、评论/@、私信、收藏及自己评论下的互动流程；新增本机凭据保护、写操作日志和账号/设置发送前复核。没有打包DSH harness、Node MCP、Whisper、FFmpeg或上游自动安装脚本，不支持视频投稿。

只读取标题、简介和对应访问权限内的评论，不下载整段视频、不冒称已看完。匿名搜索使用B站正常签发的临时游客Cookie；可选扫码登录由B站签发本机账号会话，不读取浏览器Cookie。遇到权限、风控或限流时显示原因；搬屎筛选不合格就跳过，普通搜索降级仅匹配关键词。

视频、封面、简介和评论属于各自权利人；代码许可不授予对视频内容的额外权利。分享为原始bilibili.com网址。账号写操作默认关闭，由部署者明确配置收件人和频率；不代表B站官方合作。
