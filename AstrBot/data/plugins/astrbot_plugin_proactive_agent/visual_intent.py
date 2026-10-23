"""Shared intent for provider generation and the existing-image plugin."""
import re


def generation_kind(text):
    text = str(text or "").strip().lower()
    text = re.sub(r"(?:不要|别|不用|不需要)(?:再|给我|帮我)?(?:生成|画|绘制|创作|制作|拍|给我看)[^，,。；;]*", "", text)
    # Questions about tools and requests to inspect supplied material aren't creation.
    if re.search(r"为什么.{0,10}(?:生成|画|拍)|(?:怎么|如何).{0,8}(?:生成|画图|拍照)|(?:解释|分析|识别|描述|评价|看看|看下|看一下).{0,10}(?:这张|这个|这幅|我发的|附件|代码|报错|日志|配置|链接|文章|题目|作业|答案)", text):
        return "none"
    create = bool(re.search(r"(?:生成|绘制|创作|重绘|画一|画个|画只|画张|画幅|画出|画点|画一下|帮.{0,3}画|给.{0,3}画|拍一|拍个|拍张|拍幅|拍下|拍照|自拍)", text)
        or re.search(r"(?:设计|制作|修改|编辑|合成).{0,20}(?:图片|表情|头像|壁纸|海报|图标|logo)", text)
        or re.search(r"(?:p图|修图|抠图|扩图|图生图|文生图|换.{0,3}背景|去掉.{0,6}背景|改成.{0,12}风格|(?:做|整|p)[一张个幅].{0,12}(?:图|头像|表情))", text)
        or re.search(r"(?:generate|draw|paint|create|render).{0,40}(?:image|picture|meme|sticker|cat|portrait|art|photo)", text)
        or re.match(r"^[ /]*画(?!面|风|质|家|中|里|图功能|图能力).+", text))
    show = re.search(r"(?:看看|看下|看一看|看一下|想看|瞅瞅|瞅一眼|瞧瞧)(.{1,60})", text)
    if not create and show:
        subject = show.group(1)
        # Show a subject, not '看看你怎么解释/有没有空/是不是懂了'.
        if not re.search(r"怎么|如何|什么|为什么|是不是|能不能|有没有|会不会|是否|说的|说啥|什么意思|解释|分析|意见|想法|办法|水平|本事|实力|厉害|战绩|成绩|操作|段位|分数|在不在|有空|聊|做题|代码|报错|日志|配置|链接|文章|题目|作业|答案|表情包|梗图|图库|现成", subject):
            create = True
    if not create:
        return "none"
    if re.search(r"自拍|你(?:自己|本人|的(?:样子|模样|样貌|脸|角|龙角|尾巴|龙尾|翅膀|照片|自拍))|(?:看看|看下|瞅瞅|瞧瞧)你(?:[呀啊呗嘛呢！!？?~～，,。 ]|$)|(?:画|拍).{0,5}(?:大肥龙|肥龙|你)", text):
        return "selfie"
    return "scene"


def generation_note(kind):
    if kind == "none":
        return ""
    subject = "主体固定为大肥龙本人：成年二次元白色长发龙娘，明显龙角和龙尾，可爱的傲娇表情，虚拟自拍构图。" if kind == "selfie" else "按这次请求的主体和构图创作。"
    return ("\n本次是实际新生图请求，不是图库搜索或文字问答。" + subject +
            "默认二次元插画画风，禁止把龙娘画成真人照片。使用可用生图能力生成并返回图片附件；不要调用表情包检索替代、只复述提示词或只承诺稍后生成。"
            "成功附图后配一句本龙拍的/自拍的角色台词，不写工具说明；失败如实说明，不能无图声称拍好了。")
