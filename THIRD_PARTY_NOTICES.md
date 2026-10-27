# 来源、许可和用途说明

核对日期：2026-10-27。下面区分原创维护代码、修改过的第三方源码、运行时依赖与不分发组件。不能用一段“免责声明”替代这些组件实际要求的许可。

| 组成 | 来源与作者 | 本仓库的处理 |
| --- | --- | --- |
| AstrBot 4.29.1 | [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)，AstrBot 开源贡献者；[版本源码](https://github.com/AstrBotDevs/AstrBot/tree/v4.29.1) | 通过 PyPI 安装，AGPL-3.0-or-later；遵守上游 [LICENSE](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/LICENSE) 和适用通知 |
| 智能图片对话插件 2.8.6 | [QingchenWait/astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub)，青尘工作室 | 包含修改版完整程序源码与页面；目录内原 GPL-3.0 LICENSE 保留，原作者和仓库地址保留 |
| 主动插件、部署脚本、本仓库文档 | 本项目维护者 gxypig；开发使用过 DeepSeek harness 与 Codex 等 AI 辅助工具 | 可适用权利范围内按仓库根目录 AGPL-3.0-or-later 发布；不宣称原创了 AstrBot、图库上游或其依赖 |
| 大肥龙刷B站插件 | [holoiallo/dsh-bili-scroll](https://github.com/holoiallo/dsh-bili-scroll) `03beb9c38ba592926f80b74170c73b94f7124b3d`；原作者 c1overzz；上游内嵌 bilibili-mcp 作者 AGI_Ananas | AstrBot改造版，保留两份MIT许可与署名；1.2含可选扫码、评论/@、私信、收藏、回评；私信签名与接口参考bilibili-api-python 17.4.1（GPL-3.0），附BILIBILI_API_LICENSE；改造范围见插件NOTICE.md；不含投稿和视频下载 |
| AstrBot 插件页面兼容补丁 | 针对 AstrBot 4.29.1 的局部修改 | 只提供匹配检查及补丁脚本，保留鉴权与 iframe 沙箱；上游源码链接与 AGPL 义务保留 |
| Python 与 PyPI 依赖 | 各软件包原作者，具体依赖随 AstrBot 包元数据解析 | 不把所有依赖改称本项目许可；原许可与声明继续有效，可用 `pip show` / `pip freeze` 查看安装版本 |
| SnowLuma | [SnowLuma/SnowLuma](https://github.com/SnowLuma/SnowLuma)，SnowLumaDevs | **不包含、不自动安装、不修改、不再分发**。需要者自行按其 [LICENSE](https://github.com/SnowLuma/SnowLuma/blob/main/LICENSE) / [EULA](https://github.com/SnowLuma/SnowLuma/blob/main/EULA.md) 使用、取得必要授权 |
| QQ、腾讯商标及客户端 | 腾讯及各权利人 | 不分发客户端，不声称获得授权或背书，不提供 QQ 账号 |
| 模型和搜索 API | 部署者选用的服务商 | 密钥、费用、服务条款和数据处理由部署者自行安排；不附带作者额度 |
| 图片与表情 | 上传者及各图片权利人 | 仓库无私人图库；开源代码许可不意味着第三方图片也获得同样许可 |

主动生活的事件、动机与时机分层参考 [HDS-Interlude agency.ts](https://github.com/ISEKAI-MomoiCore/HDS-Interlude/blob/13a623fdc23c0558173fbfa11ad9cdf51c8330e0/src/agency.ts#L101-L186) 的设计思想；本仓库在AstrBot中独立实现，没有复制该源码。

## 修改说明

项目基于已有 AstrBot 部署整理，图库插件安装元数据显示 2.8.6，原安装未保留可核实的上游 Git commit，**不编造一个提交号**。本仓库以 `SOURCE_MANIFEST.json` 记录分发文件的 SHA-256，并由仓库提交固定具体源码。

图库本地修改包括：私聊/群聊图片生成意图与现有图库检索分流，多维度具体标签，人工标签保留，重打标失败保留旧数据，冷启动与模型失败退避，回复准备的并行处理。修改发生在 2026-10-22 至 2026-10-23；完整修改源码随目录提供。

主动插件包括会话隔离、公共梗记忆、可选的模型审阅、句末标点处理、群聊概率/唤醒规则、逐目标固定定时和控制面板。发布版使用通用提示词和无账号默认配置，不复制作者的私人提示词定制、聊天记录、密钥或登录态。

## 使用、修改与分发

维护者把本项目用于自用、学习和非商业技术交流；这表达项目用途，**不是对 GPL/AGPL 组件附加“禁止商业使用”的限制**。各文件适用的原始许可优先。GPL/AGPL 允许的使用权不会被这份文档收回；SnowLuma 等未分发软件的独立限制也不会被本仓库解除。

修改/再分发时保留作者信息、版权和许可文本，明确标注自己的修改，并履行相应源码提供义务。若通过网络向他人提供修改后的 AGPL 程序交互服务，应按其许可提供对应源码获取方式。不要把本项目或上游软件包装成自己独占的闭源成果。

这是一个第三方部署与扩展项目，与腾讯、QQ、AstrBot、SnowLuma 和模型服务商不存在本仓库所声称的官方合作、授权或背书。相关名称、商标和标识归其各自权利人所有。

## 免责声明与实际责任

软件按现状提供，在适用法律允许范围内不作可用性、特定用途适用性、不侵权或不中断的保证；责任限制以各组件许可和适用法律为准。自动恢复是尽力而为，不保证账号安全、消息投递、模型正确性或外部服务持续可用。

使用者应确保拥有所处理数据与图片的相应权利，遵守适用法律、平台和服务商规定，尊重他人隐私和接收消息的意愿。不应利用主动发送骚扰、批量垃圾信息、欺诈或冒用身份。AI 生成内容可能出错，涉及实际操作和重要信息应核实。

“仅供学习”“风险自负”不能豁免法定义务、消除侵权，也不能授权本来无权分发的第三方组件。本说明是项目来源和使用边界记录，不构成对所有部署情形的法律合规保证。

对话页面兼容修改（v1.10）：`dashboard_live_patch.py`对固定AstrBot 4.29.1的对话工作台添加自动刷新、最新优先和即时收发切换，原页面代码来源为上文AstrBot项目，继续遵守AGPL-3.0。补丁不分发完整框架构建，保留版本校验和原文件备份。
