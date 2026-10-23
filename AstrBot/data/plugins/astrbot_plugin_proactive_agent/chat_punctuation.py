"""Conservative prose punctuation; never modify a URL, decimal or command receipt."""
import re

PUNCTUATION_RULE = "日常聊天和动态不要用句号收尾；相邻短句可换行。保留问号、感叹号、省略号；网址、小数、文件名及管理回执保持准确。"


def casual_punctuation(text, mode="chat"):
    if mode == "management" or "```" in text:
        return text
    # Chinese full stops delimit prose; preserve punctuation inside a URL/token.
    protected = re.compile(r"https?://[^\s。]+|`[^`]*`", re.I)
    chunks, start = [], 0
    for match in protected.finditer(text):
        chunks.append(_prose(text[start:match.start()]))
        chunks.append(match.group())
        start = match.end()
    chunks.append(_prose(text[start:]))
    return "".join(chunks).strip() or text


def _prose(text):
    # Keep ellipses, decimals and extensions; remove only prose line-ending dots.
    text = re.sub(r"(?m)(?<![.\d])\.(?=[\t ”’\"」』）)]*$)", "", text)
    text = re.sub(r"。+(?=[\s”’\"」』）)]|$)", "", text)
    return re.sub(r"。+", "\n", text)
