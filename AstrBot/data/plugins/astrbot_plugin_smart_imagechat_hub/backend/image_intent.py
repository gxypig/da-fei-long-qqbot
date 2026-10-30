"""Keep image creation/questions out of the existing-image search handler."""
import re
try:
    from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind
except ImportError:
    generation_kind = None


def is_generation_request(text: str) -> bool:
    if generation_kind is not None:
        return generation_kind(text) != "none"
    text = str(text or "").strip().lower()
    # Ignore explicitly negated creation clauses ("不要画，找个现成的表情包").
    text = re.sub(r"(?:不要|别|不用|不需要)(?:再|给我|帮我)?(?:生成|画|绘制|创作|制作)[^，,。；;]*", "", text)
    return bool(
        re.search(r"(?:生成|绘制|创作|重绘|画一|画个|画只|画张|画幅|画出|画点|画一下|帮.{0,3}画|给.{0,3}画)", text)
        or re.search(r"(?:设计|制作|修改|编辑|合成).{0,20}(?:图片|表情|头像|壁纸|海报|图标|logo)", text)
        or re.search(r"(?:p图|修图|抠图|扩图|图生图|文生图|换.{0,3}背景|去掉.{0,6}背景|改成.{0,12}风格|(?:做|整|p)[一张个幅].{0,12}(?:图|头像|表情))", text)
        or re.search(r"(?:generate|draw|paint|create|render).{0,40}(?:image|picture|meme|sticker|cat|portrait|art|photo)", text)
        or re.match(r"^[ /]*(?:画)(?!面|风|质|家|中|里|图功能|图能力).+", text)
    )


def is_library_request(text: str) -> bool:
    try:
        from data.plugins.astrbot_plugin_proactive_agent.conversation_controls import image_stop
        if image_stop(text):return False
    except ImportError:pass
    if is_generation_request(text):
        return False
    text = str(text or "").strip().lower()
    if re.search(r"(?:什么意思|怎么用|怎么做|怎么生成|怎么画|为什么|如何|解释|描述|识别|看懂|分析)", text):
        return False
    image_word = re.search(r"表情包|梗图|动图|照片|美图|图片|[张个幅].{0,8}图|meme|sticker|picture|photo", text)
    request_word = re.search(r"找|搜|来|发|给|挑|选|有没有|求|分享|send|find|show|give", text)
    return bool(image_word and request_word) or text in {"表情包", "梗图"}
