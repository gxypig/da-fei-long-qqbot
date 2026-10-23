"""Authenticated native AstrBot plugin page endpoints; one existing config source."""
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from quart import jsonify, request

PROJECT = Path(__file__).resolve().parents[4]
FIELDS = {
    "group_chat_ids": "", "group_chat_probability": 10,
    "group_wake_words": "大肥龙,肥龙", "group_wake_anywhere": True,
    "enable": True, "allow_group": True, "allow_private": False, "allow_qzone": True,
    "interval_minutes": 30, "quiet_hours": "23:00-07:00",
    "discretionary_enabled": True,
    "fixed_messages_enabled": False, "fixed_group_ids": "", "fixed_private_ids": "",
    "fixed_message_interval_minutes": 30,
    "fixed_qzone_enabled": False, "fixed_qzone_interval_minutes": 360,
}


def settings(plugin):
    values = {key: plugin._cfg(key, default) for key, default in FIELDS.items()}
    revision = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return values, revision


def validate(data):
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        raise ValueError("设置字段不完整，请刷新页面再保存")
    values = dict(data)
    for key, default in FIELDS.items():
        if type(values[key]) is not type(default):
            raise ValueError("设置类型不正确：" + key)
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
            ("panel_groups", self.panel_groups_api, ["GET"]),
            ("panel_friends", self.panel_friends_api, ["GET"]),
            ("panel_schedule", self.panel_schedule_api, ["GET"]),
            ("panel_health", self.panel_health_api, ["GET", "POST"]),
        ]:
            self.context.register_web_api("/astrbot_plugin_proactive_agent/" + name, handler, methods, "大肥龙控制面板")

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
        body = await request.get_json(silent=True) or {}
        mode = body.get("mode") if isinstance(body, dict) else None
        if mode not in {"Check", "Deep", "Repair"}:
            return jsonify({"status": "error", "message": "无效的检查类型"}), 400
        async with self._panel_lock:
            job = current_job()
            if job and job.get("status") == "running":
                return jsonify({"status": "error", "message": "检查正在执行，请等待结果"}), 409
            job_id = str(uuid.uuid4())
            job = {"id": job_id, "mode": mode, "status": "running", "started": datetime.now(timezone.utc).isoformat()}
            # A short-lived launcher orphans the worker before any AstrBot repair;
            # otherwise the exact descendant shutdown would kill its own repair job.
            try:
                save_job(job)
                command = [sys.executable, str(PROJECT / "manage.py"), "panel-job", "--mode", mode, "--job-id", job_id]
                kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
                subprocess.Popen(command, cwd=str(PROJECT), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs)
            except OSError:
                job.update(status="failed", message="检查任务未能启动")
                save_job(job)
                return jsonify({"status": "error", "message": "检查任务未能启动，请查看本机日志"}), 500
            return jsonify({"status": "ok", "data": {"job_id": job_id}})
