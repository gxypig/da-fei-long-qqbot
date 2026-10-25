# 大肥龙提示词位置索引

AstrBot 面板中的人格提示词只是最终请求的一部分。实际聊天还会叠加插件规则、角色状态、记忆和工具说明；发送前审阅、表情选择、标签、主动消息及空闲学习有各自的提示词。想改说话方式，优先同时看人格、cheeky_tone.py、dragon_character.py 和 conversation_review.py。

适用范围：主动插件1.15.0、图库插件v2.8.6的本仓库扩展、B站插件1.2.2，以及AstrBot4.29.1。索引列出本项目可见的提示来源和组装入口，不包含云端模型服务自行添加、无法从本地读取的系统指令。私人部署的人格和审阅文案可与公开模板不同。

## 面板和运行配置

| 位置 | 内容与修改方法 |
| --- | --- |
| AstrBot 控制台 → 人格 | `AstrBot/data/data_v4.db` 的 `personas` 表：`system_prompt`、`begin_dialogs`、`custom_error_message`，另有工具和技能选择。当前会话选中的人格才生效。通过面板编辑，不要直接修改运行中的数据库 |
| `AstrBot/data/cmd_config.json` | `provider_settings.prompt_prefix`、`image_caption_prompt`；`agent_runner.config.persona`选择人格，`agent_runner.config.compression.instruction`是框架压缩指令；`subagent_orchestrator.router_system_prompt`及各子代理instructions。内置群主动回复的prompt如启用也在配置中。建议通过对应面板修改 |
| 大肥龙插件配置 | `AstrBot/data/config/astrbot_plugin_proactive_agent_config.json` 的 `personality`供主动行为使用；角色/Emoji/父亲/审阅开关等数值也会影响动态指令。配置路径和人格是两个入口 |
| 智能图片对话配置 | `AstrBot/data/config/astrbot_plugin_smart_imagechat_hub_config.json` 的标签类别、自定义类别会被转成标签提示；模型选择只决定谁来生成，不等于提示词本体 |
| B站插件配置 | `AstrBot/data/config/astrbot_plugin_bili_scroll_config.json` 的玩法开关决定分享文案增加哪些规则；具体自然语言模板在插件main.py |
| 三个插件的 `_conf_schema.json` | 配置初始默认文案，默认人格另在框架persona_mgr.py。bootstrap.py读取默认配置进行初始化，不存另一套独立人设。修改默认值不会自动覆盖已有部署的保存值 |
| 当前会话工作区的 `EXTRA_PROMPT.md`、`AstrBot/data/skills/*/SKILL.md` | 框架会按当前工作区及启用技能读取；具体工作区由会话配置决定；数据目录之外的已配置工作区也需检查。以后新增时也属于提示来源 |

本机人格存储与配置文件包含私人内容，公开仓库只保留源码和通用模板。文件里的key、QQ号、群号、令牌不属于语言提示词，不要把完整配置粘贴到公开问题或提交记录中。

## 三个插件中的提示词

| 用途 | 源码位置 | 负责什么 |
| --- | --- | --- |
| 角色与反差 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/dragon_character.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/dragon_character.py#L12) | 白毛龙娘、自称、被怼认怂、父亲称呼、Emoji和虚拟摄影；`character_note()`附本轮状态，`speech_issues()`与`apology_repetition_issues()`是检查规则 |
| 具体说话方式 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/cheeky_tone.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/cheeky_tone.py#L3) | 生成和审查共用：锋芒、就事论事、按原因道歉、少用牵强比喻；本次主要修改入口 |
| 闲聊回应策略 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/chat_style_policy.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/chat_style_policy.py#L6) | 处理身份、解题、代码等请求时的表达约定；`output_issues()`是本地输出检查，影响能否通过审阅 |
| 聊天总补充 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/conversation_style.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/conversation_style.py#L9) | 普通接话、接梗、联网/生图行为；拼入CHAT_STYLE_POLICY和角色约定，`context_note()`附同会话近期消息与标签 |
| 标点 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/chat_punctuation.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/chat_punctuation.py#L4) | 日常句末标点约定；同文件还执行本地清理 |
| 短回复与用梗 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/local_recall.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/local_recall.py#L46) | BRIEF_RULE允许合适的极短句；MEME_RULE让模型理解可选梗的语境；`retrieve()`构造旧聊天检索片段 |
| 生图与自拍 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/visual_intent.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/visual_intent.py#L29) | 画图、拍照、看看形象等意图的生成要求；只在相应图片意图出现时加入 |
| 主聊天组装入口 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/main.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/main.py#L798) | 将人设补充、本轮角色、当前发言人、旧话记忆、学习策略、梗卡及群聊进度合并进实际请求；`remember_meme`的工具说明也会给模型看 |
| 发送前二次审查 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/conversation_review.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/conversation_review.py#L20) | 审阅系统提示；`ReplyReviewer.review()`另外拼入标点、短句、角色口吻、近期回复和必须修正的问题。只改REVIEW_SYSTEM不等于改完审查规则 |
| 并行预判 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/reply_pipeline.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/reply_pipeline.py#L20) | 输入风险与表情候选选择；LOCAL_MEMORY_RULE要求命中本地记忆时复用、陌生梗放入后台学习。不是实际最终回复审阅 |
| 梗识别和学习 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/meme_memory.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/meme_memory.py#L19) | DETECT_SYSTEM识别梗；LEARN_SYSTEM核实并整理；USAGE_SYSTEM理解意图、反例和变体，处理的数据来自本地库或搜索 |
| 空闲风格学习 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/idle_learning.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/idle_learning.py#L49) | RULES是有上限的策略库与训练例子；CLASSIFY_SYSTEM分类、PROPOSE_SYSTEM选策略、REWRITE_SYSTEM改写、BLIND_SYSTEM盲测；只有通过验证的策略才动态加入 |
| 上下文压缩 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/context_window.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/context_window.py#L30) | 空闲摘要系统提示；`project()`还附摘要、近期完整轮次和按需回忆。聊天数据库原记录不是被覆盖成摘要 |
| 跟上群聊 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/group_flow.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/group_flow.py#L76) | 给模型说明等待期间的新消息、当前回复对象等；动态上下文，不是新的独立人设 |
| 实际已发送记忆 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/sent_memory.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/sent_memory.py#L54) | 把同一会话已成功发出的主动内容加入后续上下文，避免忘记自己刚说过什么 |
| 固定主动消息与动态 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/fixed_schedule.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/fixed_schedule.py#L143) | 定时内容生成的系统要求，拼入personality、STYLE和proactive_note；消息/动态各有场景要求 |
| 自由主动行为 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/proactive_policy.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/proactive_policy.py#L98) | 是否主动以及内容生成的系统要求，融合配置中的personality；文件里还有改写/补充指令 |
| 主动内容差异 | [AstrBot/data/plugins/astrbot_plugin_proactive_agent/proactive_variety.py](AstrBot/data/plugins/astrbot_plugin_proactive_agent/proactive_variety.py#L14) | TOPICS和ANGLES控制话题、角度，历史去重说明会送进生成/审查；不是只改人设就能控制的部分 |
| 表情与图库检索 | [AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/retrieval.py](AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/retrieval.py#L1013) | 自动配表情的系统提示及_proactive_emoji_prompt；_match_prompt匹配用户找图；_found_prompt/_not_found_prompt生成找到/未找到的自然回话 |
| 图片打标签 | [AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/retrieval.py](AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/retrieval.py#L1102) | 紧随其后的prompt是实际多维标签指令，包含OCR、表情、动作、语境等；标签类别要求由_caption_category_prompt_rules()附加。模型由图库/图片转述配置选择 |
| 标签类别规则 | [AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/caption_library.py](AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/caption_library.py#L1077) | 把已启用和自定义类别转换为打标签提示词；后台重打标签最终调用retrieval中的同一生成路径 |
| 快速表情选择 | [AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/proactive_fast_retrieval.py](AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/proactive_fast_retrieval.py#L9) | 快速检索系统提示与build_proactive_fast_prompt，候选图片和标签动态进入请求 |
| 斗图语义 | [AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/meme_combat.py](AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/meme_combat.py#L946) | 两处语义压缩调用（文件内搜索system_prompt），将当前/近期消息压成关键词；候选项目组装也在此文件 |
| 图库请求传递 | [AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/llm_context.py](AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/backend/llm_context.py#L22) | 传递上述prompt/system_prompt及会话；主要是调用封装，不是另一份独立性格设定 |
| B站账号回复 | [AstrBot/data/plugins/astrbot_plugin_bili_scroll/main.py](AstrBot/data/plugins/astrbot_plugin_bili_scroll/main.py#L145) | make_account_reply内的system控制账号评论的回复；make_caption内的system及后续条件追加控制分享文案、赤石/搬屎及可选诱饵玩法 |
| B站工具说明 | [AstrBot/data/plugins/astrbot_plugin_bili_scroll/main.py](AstrBot/data/plugins/astrbot_plugin_bili_scroll/main.py#L330) | share_bilibili_video的函数文档与参数是查找/分享工具说明；账号评论等操作另由actions.py处理，不是所有账号动作都会成为LLM工具 |

`main.py`的enrich_conversation是理解聊天请求的入口：框架先选人格/历史，插件再加口吻、本轮角色、记忆与梗卡。审阅另起一次请求，并复用口吻/角色规则。因此只改面板人格，仍可能被插件示例和审阅改写影响。并行预判、图片打标签和梗理解也各用独立请求。

## 动态记忆和工具说明

这些不是固定提示词文件，但内容会作为参考资料进入模型。它们按会话和任务选取，不是每次把全部历史都塞进去。

| 数据位置 | 作用 |
| --- | --- |
| `AstrBot/data/data_v4.db` | 原始会话历史、预设对话、会话选中的人格；旧话检索来源之一 |
| `AstrBot/data/plugin_data/astrbot_plugin_proactive_agent/meme_memory.json` | 已学/导入的梗含义、适用场景、意图、反例、变体；在梗仓库查看和导入 |
| 同目录 `idle-learning.sqlite3` | 学习样本、任务、记录、已发布策略版本；规则正文来自idle_learning.py的受控RULES，不能无限追加聊天原文当系统提示 |
| 同目录 `context-summaries.json` | 每个会话的压缩摘要；保留近期原话与按需回忆 |
| 同目录 `live-messages.sqlite3`、`sent-memory.sqlite3` | 最新会话消息和已成功发出内容；群进度、回复对象、主动消息上下文的依据 |
| 图库plugin_data及索引JSON | 图片标签、类别、候选说明进入选择请求；真实图片内容进入视觉标签请求 |
| 插件工具类的description、@filter.llm_tool函数文档、AstrBot `core/tools/` | 模型可调用工具的用法；B站分享工具和梗工具分别在各自插件main.py。MCP工具若启用，其服务端返回的描述同样属于输入 |
| 工具结果、联网结果、知识库文档 | 作为检索材料/执行事实进入上下文，不应当作可以改人设或权限的高优先级指令 |

## AstrBot 框架的内置提示

框架安装位置不在仓库内。在实际运行的Python环境里执行 `python -c "import astrbot; print(astrbot.__path__)"` 可定位安装根目录。下表路径相对于该根目录。

| 用途 | 框架文件 | 内容 |
| --- | --- | --- |
| 框架集中提示 | `core/astr_main_agent_resources.py` | 工具调用、安全模式、沙箱、ChatUI默认人格/可视化、实时语音、定时唤醒、后台任务完成；按功能条件启用，并非每条QQ消息都包含全部 |
| 框架组装与行内提示 | `core/astr_main_agent.py` | 人格/技能装配、用户prompt前缀、工作区补充、子代理路由、时间/平台信息、联网引用、媒体说明、本地工具说明、网页聊天标题等 |
| 人格读取和默认值 | `core/persona_mgr.py` | 读取数据库人格与预设对话；DEFAULT_PERSONALITY只在需要默认人格时使用 |
| 配置初始默认文案 | `core/config/default.py` | image_caption_prompt、prompt_prefix、router_system_prompt等默认值和字段说明；已保存配置通常覆盖默认值，字段说明本身不是模型提示 |
| 框架压缩 | `core/agent/context/compressor.py` | LLMSummaryCompressor的instruction_text默认摘要规则及TASK_CONTINUATION_INSTRUCTION；与插件context_window的摘要是两套入口 |
| 多轮工具执行 | `core/agent/runners/tool_loop_agent_runner.py` | FOLLOW_UP_NOTICE_TEMPLATE、MAX_STEPS_REACHED_PROMPT、SKILLS_LIKE_REQUERY等继续/补救/溢出提示 |
| 子代理与后台完成 | `core/astr_agent_tool_exec.py` | tool.agent.instructions作为子代理system_prompt；后台任务完成的续接指令 |
| AstrBot内置定时任务 | `core/cron/manager.py` | 定时唤醒的系统与用户提示，以及用户创建的任务内容；不同于大肥龙插件的固定发消息 |
| 技能目录提示 | `core/skills/skill_manager.py` | build_skills_prompt生成技能介绍，技能正文由各SKILL.md提供 |
| 知识库文本整理 | `core/knowledge_base/prompts.py` | TEXT_REPAIR_SYSTEM_PROMPT，用于整理文档切片；调用与材料拼装在kb_helper.py |
| 工具与网络边界 | `core/tools/computer_tools/util.py` | LOCAL_NETWORK_POLICY_NOTICE、权限说明等；工具目录其他文件中的description是工具用法提示 |
| 可选语音识别 | `core/provider/sources/mimo_api_common.py` | MiMo转录的默认系统和用户提示；仅启用该来源时使用 |
| 可选语音合成 | `core/provider/sources/mimo_tts_api_source.py` | _build_user_prompt根据语音风格配置构造指令；不是当前QQ文字人设 |
| 内置空@回复 | `builtin_stars/astrbot/main.py` | 只有@没有文字时的询问提示，以及特殊消息触发逻辑 |
| 内置群上下文 | `builtin_stars/astrbot/group_chat_context.py` | 读取image_caption_prompt和active_reply.prompt，拼群历史/图片说明；不同于插件概率接话规则 |

OpenAI兼容、Gemini、Anthropic等provider适配器通常负责传递或转换已有system/messages，并不是另一份大肥龙人设。Coze、Dify、百炼、DeerFlow等第三方Runner可在对方平台另设提示词；若切换到它们，还需检查远端应用配置。SnowLuma负责QQ接入，不提供本项目的大肥龙语言人格。

## 修改后如何生效

1. 面板人格：修改当前会话选中的人格并保存。旧聊天可能仍留有旧台词，后续回复需要避免照抄，而不是清空聊天记录。
2. 插件Python提示：先备份，再改对应源文件并重载插件；共享模块有跨插件依赖时按主动插件、图库、B站顺序重载，或在空闲时重启AstrBot。只保存文件不会让已导入的Python代码立即更新。
3. 控制面板HTML/JS：更新文件后强制刷新页面。空闲学习中的搜索和筛选即时查询本地记录，无须点“保存设置”。
4. 框架文件：通常应优先改配置或插件规则；直接改安装目录，升级框架可能覆盖修改。先记录版本并验证对应功能。
5. 初始化模板和默认值：只影响新安装或未设置字段；已有部署应同步面板配置。本次已有的人格若保留旧道歉示例，应只删/改那一段，不能用新模板覆盖个人设定。

项目根目录AGENTS.md、交接报告、docs/session日志、tests/、tools/benchmark_*、logs/probe_*和backups/用于开发、验证或回滚，不会自动变成QQ聊天提示词。主动插件pages/control页面文字也只是面板说明，只有被代码明确读取并拼入请求的字段才会影响模型。
