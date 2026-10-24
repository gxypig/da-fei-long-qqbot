"""Authenticated native AstrBot plugin page endpoints; one existing config source."""
import asyncio
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from quart import jsonify, request

PROJECT = Path(__file__).resolve().parents[4]
FIELDS = {
    "father_qq": "", "father_nickname": "", "emoji_probability": 18,
    "father_address_probability": 20, "auto_quote_enabled": False, "meme_separate_probability": 50,
    "group_flow_enabled": True, "group_passive_max_age": 25, "chat_low_latency": False,
    "group_chat_ids": "", "group_chat_probability": 10,
    "group_wake_words": "大肥龙,肥龙", "group_wake_anywhere": True,
    "enable": False, "allow_group": False, "allow_private": False, "allow_qzone": False,
    "interval_minutes": 30, "quiet_hours": "23:00-07:00",
    "discretionary_enabled": True,
    "fixed_messages_enabled": False, "fixed_group_ids": "", "fixed_private_ids": "",
    "fixed_message_interval_minutes": 30,
    "fixed_qzone_enabled": False, "fixed_qzone_interval_minutes": 360,
    "latency_optimization_enabled": False, "review_policy": "all",
    "reply_prediction_enabled": True, "prediction_provider_id": "",
    "idle_learning_enabled": False, "style_learning_enabled": False,
    "reply_audit_sample_percent": 10, "emoji_late_grace_seconds": 3,
    "reply_latency_target_seconds": 30, "model_concurrency": 2,
    "reply_hedge_after_seconds": 15,
    "context_window_enabled": False, "context_recent_turns": 12, "context_recent_tokens": 6000,
}


def settings(plugin):
    values = {key: plugin._cfg(key, default) for key, default in FIELDS.items()}
    revision = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return values, revision


EMOJI_CONFIG_KEY = "proactive_emoji_reply"
EMOJI_UNAVAILABLE = "智能图片对话插件未加载或未启用，不能修改表情包概率；请启用图库后重新读取"


def emoji_settings(gallery):
    """Read the live gallery's source; never store a second probability here."""
    effective = gallery._proactive_emoji_config()
    probability = Decimal(str(effective["trigger_probability"]))
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise ValueError("图库中的表情包概率不正确，请先检查图库配置")
    # A plugin reload or any gallery configuration edit invalidates an old page.
    source = {"instance": id(gallery), "config_instance": id(gallery.config),
              "config": dict(gallery.config)}
    revision = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
                                        allow_nan=False).encode()).hexdigest()
    enabled = effective.get("enabled") is True
    return {"available": True, "writable": True, "percent": float(probability * 100),
            "revision": revision,
            "message": ("图库自动配图当前开启，后续回复读取此概率" if enabled else
                        "图库自动配图当前关闭，可以预先设置概率；保存概率不会自动开启")}


def validate(data):
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        raise ValueError("设置字段不完整，请刷新页面再保存")
    values = dict(data)
    for key, default in FIELDS.items():
        if type(values[key]) is not type(default):
            raise ValueError("设置类型不正确：" + key)
    if values["review_policy"] not in ("all", "speed_first"):
        raise ValueError("审查策略只能选择 all 或 speed_first")
    for key, minimum, maximum, label in [
        ("reply_audit_sample_percent", 0, 100, "回复审计抽样比例"),
        ("emoji_late_grace_seconds", 0, 10, "表情包迟到宽限秒数"),
        ("reply_latency_target_seconds", 5, 120, "正文回复目标秒数"),
        ("model_concurrency", 1, 4, "模型并发数"),
        ("reply_hedge_after_seconds", 0, 30, "慢请求补救等待秒数"),
        ("context_recent_turns", 4, 40, "近期完整对话轮数"),
        ("context_recent_tokens", 2000, 16000, "近期上下文估算预算"),
    ]:
        if not minimum <= values[key] <= maximum:
            raise ValueError(f"{label}须为{minimum}至{maximum}的整数")
    values["prediction_provider_id"] = values["prediction_provider_id"].strip()
    if not 0 <= values["emoji_probability"] <= 100:
        raise ValueError("Emoji概率必须为0至100的整数")
    for key in ['father_address_probability', 'meme_separate_probability']:
        if not 0 <= values[key] <= 100:
            raise ValueError('概率必须为0至100的整数：' + key)
    if not 5 <= values['group_passive_max_age'] <= 120:
        raise ValueError('概率接话最长等待须为5至120秒')
    values["father_qq"] = values["father_qq"].strip()
    if values["father_qq"] and not re.fullmatch(r"[0-9]{5,20}", values["father_qq"]):
        raise ValueError("父亲QQ请填写有效数字，或留空")
    if len(values["father_nickname"]) > 80:
        raise ValueError("昵称不能超过80字")
    if not 0 <= values["group_chat_probability"] <= 100:
        raise ValueError("接话概率必须为0至100的整数")
    if not 5 <= values["interval_minutes"] <= 1440:
        raise ValueError("主动检查间隔须为5至1440分钟")
    for key in ["fixed_message_interval_minutes", "fixed_qzone_interval_minutes"]:
        if not 5 <= values[key] <= 10080:
            raise ValueError("固定发送间隔须为5至10080分钟")
    for key in ["fixed_group_ids", "fixed_private_ids"]:
        ids = [v for v in re.split(r"[,，\s]+", values[key]) if v]
        if len(ids) > 200 or any(not re.fullmatch(r"[0-9]{1,20}", v) for v in ids):
            raise ValueError("定时目标只能填写数字，最多200个")
        values[key] = ",".join(sorted(set(ids)))
    groups = [g for g in re.split(r"[,，\s]+", values["group_chat_ids"]) if g]
    if len(groups) > 200 or any(not re.fullmatch(r"[0-9]{1,20}", g) for g in groups):
        raise ValueError("群号只能填写数字，多个群用逗号分隔")
    values["group_chat_ids"] = ",".join(sorted(set(groups)))
    words = values["group_wake_words"]
    if len(words) > 200 or any(len(w) > 40 for w in re.split(r"[,，\s]+", words)):
        raise ValueError("唤醒词过长，单个不超过40字、总共不超过200字")
    quiet = values["quiet_hours"].strip()
    if quiet and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", quiet):
        raise ValueError("免打扰请填写23:00-07:00这样的时间段，或留空")
    values["quiet_hours"] = quiet
    return values


def local_json(name):
    path = PROJECT / "logs" / name
    try:
        if path.stat().st_size > 200_000:
            return None
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def save_job(job):
    path = PROJECT / "logs/panel-health-job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def current_job():
    job = local_json("panel-health-job.json")
    if job and job.get("status") == "running":
        try:
            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(job["started"]).timestamp()
            stale = age > 900
        except (ValueError, KeyError, TypeError):
            stale = True
        if stale:
            # A vanished worker must not permanently disable the control buttons.
            job = {**job, "status": "failed", "message": "检查任务已超时，请查看本机日志或重新检查"}
    return job


class PanelMixin:
    def register_panel(self):
        self._panel_lock = asyncio.Lock()
        for name, handler, methods in [
            ("panel_settings", self.panel_settings_api, ["GET", "POST"]),
            ("emoji_settings", self.emoji_settings_api, ["GET", "POST"]),
            ("panel_groups", self.panel_groups_api, ["GET"]),
            ("panel_friends", self.panel_friends_api, ["GET"]),
            ("panel_schedule", self.panel_schedule_api, ["GET"]),
            ("panel_health", self.panel_health_api, ["GET"]),
            ("learning_action", self.learning_action_api, ["POST"]),
        ]:
            self.context.register_web_api("/astrbot_plugin_proactive_agent/" + name, handler, methods, "大肥龙控制面板")

    def _emoji_library(self):
        lookup = getattr(self.context, "get_registered_star", None)
        meta = lookup("astrbot_plugin_smart_imagechat_hub") if callable(lookup) else None
        gallery = getattr(meta, "star_cls", None) if getattr(meta, "activated", False) else None
        if (gallery is None or not isinstance(getattr(gallery, "config", None), dict)
                or not isinstance(getattr(gallery, "_lock", None), asyncio.Lock)
                or not callable(getattr(gallery, "_proactive_emoji_config", None))
                or not callable(getattr(gallery.config, "save_config", None))):
            return None
        return gallery

    async def emoji_settings_api(self):
        # The native /api/plug registration uses AstrBot's dashboard authentication.
        body = None
        if request.method == "POST":
            body = await request.get_json(silent=True)
            if (not isinstance(body, dict) or set(body) != {"percent", "revision"}
                    or type(body.get("percent")) not in (int, float)
                    or not isinstance(body.get("revision"), str) or not body["revision"]):
                return jsonify({"status": "error", "message": "请提供0至100的百分比和当前版本，刷新后重试"}), 400
            percent = Decimal(str(body["percent"]))
            if not percent.is_finite() or not 0 <= percent <= 100:
                return jsonify({"status": "error", "message": "表情包概率须为0至100的百分比"}), 400
        gallery = self._emoji_library()
        if gallery is None:
            if body is None:
                return jsonify({"status": "ok", "data": {"available": False, "writable": False,
                    "percent": None, "revision": None, "message": EMOJI_UNAVAILABLE}})
            return jsonify({"status": "error", "message": EMOJI_UNAVAILABLE}), 503
        config = gallery.config
        # Share the gallery editor's own lock, including edits from its native page.
        async with gallery._lock:
            if self._emoji_library() is not gallery or gallery.config is not config:
                return jsonify({"status": "error", "message": "图库实例已变更，请重新读取后再保存"}), 409
            try:
                current = emoji_settings(gallery)
            except (KeyError, TypeError, ValueError, InvalidOperation):
                return jsonify({"status": "error", "message": "无法读取图库配置，请检查图库后重新读取"}), 503
            if body is None:
                return jsonify({"status": "ok", "data": current})
            if not current["writable"]:
                return jsonify({"status": "error", "message": current["message"]}), 503
            if body["revision"] != current["revision"]:
                return jsonify({"status": "error", "message": "图库设置已在其他地方变更，请重新读取后再保存"}), 409
            previous = config.get(EMOJI_CONFIG_KEY)
            if not isinstance(previous, dict):
                return jsonify({"status": "error", "message": "图库自动表情包配置不完整，请先检查图库"}), 503
            updated = copy.deepcopy(previous)
            updated["trigger_probability"] = format((percent / 100).normalize(), "f")
            try:
                config[EMOJI_CONFIG_KEY] = updated
                if config.save_config() is False:
                    raise OSError("Gallery did not confirm persistence")
            except Exception:
                # Restore only our section; never replace the shared Config instance.
                config[EMOJI_CONFIG_KEY] = previous
                try:
                    if config.save_config() is False:
                        raise OSError("Gallery rollback did not confirm persistence")
                except Exception:
                    return jsonify({"status": "error", "message":
                        "保存失败，内存概率已恢复；配置文件恢复未能确认，请检查磁盘与权限后重新读取"}), 500
                return jsonify({"status": "error", "message": "保存失败，原图库配置已恢复，请重新读取后重试"}), 500
            return jsonify({"status": "ok", "data": emoji_settings(gallery)})

    async def panel_settings_api(self):
        if request.method == "GET":
            values, revision = settings(self)
            return jsonify({"status": "ok", "data": {"values": values, "revision": revision}})
        body = await request.get_json(silent=True)
        try:
            if not isinstance(body, dict):
                raise ValueError("请求格式不正确")
            values = validate(body.get("values"))
            async with self._panel_lock:
                if body.get("revision") != settings(self)[1]:
                    return jsonify({"status": "error", "message": "设置已在其他地方变更，请刷新后再保存"}), 409
                previous = copy.deepcopy(dict(self.config))
                try:
                    self.config.update(values)
                    self.config.save_config()
                except Exception:
                    self.config.clear()
                    self.config.update(previous)
                    return jsonify({"status": "error", "message": "保存失败，原设置已保留，请检查配置文件权限"}), 500
            saved, revision = settings(self)
            return jsonify({"status": "ok", "data": {"values": saved, "revision": revision}})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

    async def learning_action_api(self):
        # Registered through the same authenticated plugin API as panel_settings.
        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"action", "version"} or body.get("action") != "rollback":
            return jsonify({"status": "error", "message": "仅支持 rollback 回退操作，请选择学习版本"}), 400
        version = body["version"]
        if (type(version) not in (str, int)
                or (isinstance(version, str) and not version.strip())
                or (type(version) is int and version < 0)):
            return jsonify({"status": "error", "message": "学习版本不正确，请刷新版本列表后重试"}), 400
        learning = getattr(self, "_learning", None)
        if not callable(getattr(learning, "rollback", None)):
            return jsonify({"status": "error", "message": "学习服务尚未就绪，请确认插件已升级并重载后重试"}), 503
        try:
            result = await learning.rollback(version)
            if result is False:
                return jsonify({"status": "error", "message": "学习版本不可用，请刷新版本列表后重试"}), 404
            return jsonify({"status": "ok", "data": result})
        except (ValueError, LookupError):
            return jsonify({"status": "error", "message": "学习版本不可用，请刷新版本列表后重试"}), 400
        except Exception:
            return jsonify({"status": "error", "message": "学习回退未完成，请刷新状态后重试"}), 500

    async def panel_groups_api(self):
        try:
            groups = await asyncio.wait_for(self._call_onebot("get_group_list"), timeout=5)
            rows = [{"id": str(g["group_id"]), "name": str(g.get("group_name") or "未命名群")} for g in (groups or [])]
            return jsonify({"status": "ok", "data": rows})
        except Exception:
            return jsonify({"status": "error", "message": "暂时无法读取QQ群；已有设置保留，也可手动填写群号"}), 503

    async def panel_friends_api(self):
        try:
            friends = await asyncio.wait_for(self._call_onebot("get_friend_list"), timeout=5)
            rows = [{"id": str(f["user_id"]), "name": str(f.get("remark") or f.get("nickname") or "好友")} for f in (friends or [])]
            return jsonify({"status": "ok", "data": rows})
        except Exception:
            return jsonify({"status": "error", "message": "暂时无法读取好友；已有选择保留，可手动填写QQ号"}), 503

    async def panel_schedule_api(self):
        from .fixed_schedule import STATE_KEY
        state = await self.get_kv_data(STATE_KEY, {})
        return jsonify({"status": "ok", "data": {"quiet_now": self._quiet_now(),
            "rows": [{k: v for k, v in {"target": target, **entry}.items() if k != "recent"}
                     for target, entry in state.items()]}})

    async def panel_health_api(self):
        if request.method == "GET":
            return jsonify({"status": "ok", "data": {"report": local_json("health-status.json"), "job": current_job()}})
        # The repair runner must survive AstrBot being offline. No panel execution.
        return jsonify({"status": "error", "message": "请在项目目录运行一键检查修复.bat，面板仅展示最近报告"}), 405
