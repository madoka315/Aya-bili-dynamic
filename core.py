# -*- coding: utf-8 -*-
"""B站UP主动态轮询引擎（与 Hoshino 框架解耦，便于独立测试）。"""
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

try:
    from .hb_grpc import DynamicType, GrpcError, get_user_dynamics
except ImportError:
    # 允许把 core.py 当独立文件跑单测（生产环境走上面的相对导入）
    from hb_grpc import DynamicType, GrpcError, get_user_dynamics

logger = logging.getLogger("hoshino.Aya-bili-dynamic")


def _ev(name: str) -> int:
    try:
        return DynamicType.Value(name)
    except ValueError:
        return -1

# 会推送的动态类型 -> 文案
PUSH_LABEL = {
    _ev("forward"): "转发了一条动态",
    _ev("av"): "发布了新投稿",
    _ev("pgc"): "发布了新投稿",
    _ev("word"): "发布了新动态",
    _ev("draw"): "发布了新图文动态",
    _ev("article"): "发布了新专栏",
    _ev("music"): "发布了新音频",
    _ev("ugc_season"): "发布了新合集",
    _ev("courses"): "发布了新课程",
    _ev("live"): "开播了",
    _ev("live_rcmd"): "开播了",
}
_PUSHED = set(PUSH_LABEL) - {-1}
# 其余类型（广告/折叠/合集推荐等）不推送，仅推进 offset


def _segments_text(segments) -> str:
    return "".join(s.text for s in segments).replace("\u200b", "").strip()


def _author_name(item) -> str:
    for m in item.modules:
        if m.HasField("module_author"):
            return m.module_author.author.name or ""
    return ""


def _get_dynamic_module(item):
    for m in item.modules:
        if m.HasField("module_dynamic"):
            return m.module_dynamic
    return None


def extract(item, text_limit: int = 500) -> Optional[dict]:
    """从一条动态提取 {label, text, text_all, images, dyn_id}；不推送的类型返回 None。

    text 为展示用正文（超长截断加省略标记），text_all 为全文（关键字匹配用，
    避免关键词恰好落在截断点之后导致漏推）。
    """
    ct = item.card_type
    if ct not in _PUSHED:
        return None
    label = PUSH_LABEL.get(ct, "发布了新动态")
    ext = item.extend
    own = _segments_text(ext.desc)
    orig = _segments_text(ext.orig_desc)
    images: List[str] = []
    title = ""
    link = ""
    md = _get_dynamic_module(item)
    if md is not None:
        if md.HasField("dyn_archive"):
            title = md.dyn_archive.title or ""
            if md.dyn_archive.cover:
                images.append(md.dyn_archive.cover)
            if md.dyn_archive.bvid:
                link = f"https://www.bilibili.com/video/{md.dyn_archive.bvid}"
        elif md.HasField("dyn_pgc"):
            title = getattr(md.dyn_pgc, "title", "") or ""
            cover = getattr(md.dyn_pgc, "cover", "")
            if cover:
                images.append(cover)
            jump = getattr(md.dyn_pgc, "jump_url", "") or ""
            if jump.startswith("http"):
                link = jump
        elif md.HasField("dyn_draw"):
            images.extend(i.src for i in md.dyn_draw.items if i.src)
        elif md.HasField("dyn_article"):
            title = md.dyn_article.title or ""
            images.extend(list(md.dyn_article.covers)[:1])
            if md.dyn_article.id:
                link = f"https://www.bilibili.com/read/cv{md.dyn_article.id}"
        elif md.HasField("dyn_music"):
            title = md.dyn_music.title or ""
            if md.dyn_music.cover:
                images.append(md.dyn_music.cover)
            if md.dyn_music.id:
                link = f"https://www.bilibili.com/audio/au{md.dyn_music.id}"
        elif md.HasField("dyn_common_live"):
            # 开播卡：id 即直播间号，链接指向直播间
            lv = md.dyn_common_live
            title = lv.title or ""
            if lv.cover:
                images.append(lv.cover)
            room = str(lv.id or "")
            if room.isdigit():
                link = f"https://live.bilibili.com/{room}"
        elif md.HasField("dyn_live_rcmd"):
            # 直播推荐卡：content 是旧版卡片 JSON，尽力解析
            try:
                j = json.loads(md.dyn_live_rcmd.content)
            except (ValueError, TypeError):
                j = {}
            info = j.get("live_play_info") or j.get("live_info") or j
            title = info.get("title") or j.get("title") or ""
            cover = info.get("cover") or j.get("cover") or ""
            if cover:
                images.append(cover)
            room = str(info.get("room_id") or info.get("live_id") or j.get("room_id") or "")
            if not room:
                m2 = re.search(r"live(?:\.bilibili\.com/|/)(\d+)", str(info.get("url") or j.get("url") or ""))
                room = m2.group(1) if m2 else ""
            if room.isdigit():
                link = f"https://live.bilibili.com/{room}"
    if ct == _ev("forward"):
        # 转发：自己的评论在前，原作者名取 ext.orig_name
        parts = []
        if own:
            parts.append(own)
        if orig:
            parts.append(f"↪ {ext.orig_name or '原动态'}：{orig}")
        text = "\n———\n".join(parts)
    else:
        text = own or orig or title
    if not text and not images:
        return None
    text = text.strip()
    text_all = text
    if len(text) > text_limit:
        text = text[:text_limit].rstrip() + "……(已截断)"
    return {
        "label": label,
        "text": text,
        "text_all": text_all,
        "title": title,
        "images": images,
        "link": link,
        "dyn_id": ext.dyn_id_str,
    }


class Poller:
    """订阅状态管理 + 轮询状态机。状态落盘 <data_dir>/subs.json"""

    def __init__(self, data_dir: str, cfg: dict):
        self.cfg = cfg
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.file = os.path.join(data_dir, "subs.json")
        self.state = self._load()
        self._last_alert = 0.0

    # ---------- 存储 ----------
    def _load(self) -> dict:
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("subs"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"subs": {}}

    def save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.file)

    # ---------- 订阅管理 ----------
    def subscribe(self, uid: str, gid: int, keywords: List[str]) -> Tuple[bool, str]:
        uid = str(uid)
        if not uid.isdigit():
            return False, "UID 应该是纯数字"
        sub = self.state["subs"].get(uid)
        if sub is None:
            sub = {"name": f"UID{uid}", "offset": None, "next_ts": 0.0,
                   "consec_empty": 0, "consec_error": 0, "groups": {}}
            self.state["subs"][uid] = sub
        sub["groups"][str(gid)] = {"keywords": keywords}
        self.save()
        return True, sub["name"]

    def unsubscribe(self, uid: str, gid: int) -> Tuple[bool, str]:
        uid = str(uid)
        sub = self.state["subs"].get(uid)
        if not sub or str(gid) not in sub["groups"]:
            return False, "本群没有关注这个 UID"
        name = sub["name"]
        sub["groups"].pop(str(gid))
        if not sub["groups"]:
            del self.state["subs"][uid]
        self.save()
        # 回复不区分"是否还有其他群在关注"，避免向本群管理员泄露其他群的存在
        return True, f"已取关 {name}（{uid}）"

    def seed_defaults(self, defaults: dict) -> int:
        """按 DEFAULT_SUBS（群号 -> UID 列表）预置订阅；已存在的订阅原样保留。

        注意：手动取关过的默认订阅在下次重启会被重新加回。
        """
        added = 0
        for gid, uids in (defaults or {}).items():
            for uid in uids:
                uid = str(uid)
                sub = self.state["subs"].get(uid)
                if sub is None or str(gid) not in sub["groups"]:
                    self.subscribe(uid, int(gid), [])
                    added += 1
        if added:
            self.save()
        return added

    def list_group(self, gid: int) -> List[dict]:
        out = []
        for uid, sub in self.state["subs"].items():
            g = sub["groups"].get(str(gid))
            if g:
                out.append({"uid": uid, "name": sub["name"], "keywords": g["keywords"]})
        return out

    def group_uids(self, gid: int) -> List[str]:
        return [uid for uid, sub in self.state["subs"].items() if str(gid) in sub["groups"]]

    # ---------- 轮询 ----------
    async def poll_uid(self, uid: str, force: bool = False) -> Tuple[str, List[dict]]:
        """拉取单个 UID。

        返回 (status, pushes)：
        - ("init", [])   首次拉取，已建立基线
        - ("new", [...]) 有新动态，pushes 为 extract() 结果
        - ("none", [])   无新动态
        - ("gated", [])  被软门控（空包），已安排快速重试
        - ("error", [])  请求异常，已退避
        """
        sub = self.state["subs"].get(uid)
        if not sub:
            return "none", []
        now = time.time()
        if not force and now < sub.get("next_ts", 0):
            return "none", []
        try:
            rsp = await get_user_dynamics(
                uid,
                access_key=self.cfg.get("ACCESS_KEY", ""),
                proxy=self.cfg.get("PROXY", ""),
                timeout=10,
            )
        except GrpcError as e:
            sub["consec_error"] = sub.get("consec_error", 0) + 1
            sub["next_ts"] = now + self.cfg.get("ERROR_BACKOFF", 180) * min(sub["consec_error"], 5)
            self.save()
            logger.warning(f"拉取动态失败 {uid}: {e}")
            return "error", []
        except Exception as e:
            sub["consec_error"] = sub.get("consec_error", 0) + 1
            sub["next_ts"] = now + self.cfg.get("ERROR_BACKOFF", 180)
            self.save()
            logger.exception(f"拉取动态异常 {uid}: {e}")
            return "error", []
        sub["consec_error"] = 0

        items = list(rsp.list)
        if not items:
            # 软门控：空列表不推进 offset，安排快速重试；持续门控则退避+告警
            sub["consec_empty"] = sub.get("consec_empty", 0) + 1
            if sub["consec_empty"] >= self.cfg.get("GATE_ALERT_N", 30):
                sub["next_ts"] = now + self.cfg.get("GATE_ALERT_BACKOFF", 600)
                if now - self._last_alert > 3600:
                    self._last_alert = now
                    logger.error(
                        f"B站动态接口疑似持续风控（{uid} 连续 {sub['consec_empty']} 次空包），"
                        "建议在插件目录 config.py 配置 ACCESS_KEY 降级"
                    )
            else:
                sub["next_ts"] = now + self.cfg.get("RETRY_INTERVAL", 12)
            self.save()
            return "gated", []

        sub["consec_empty"] = 0
        name = _author_name(items[0])
        if name:
            sub["name"] = name
        ids = [int(i.extend.dyn_id_str) for i in items]
        if sub["offset"] is None:
            # 首次拉取只建基线不推历史；置顶可能占首位，取前两条较大值
            sub["offset"] = str(max(ids[:2]))
            sub["next_ts"] = now + self.cfg.get("POLL_INTERVAL", 60)
            self.save()
            logger.info(f"已建立 {sub['name']}({uid}) 的动态基线 offset={sub['offset']}")
            return "init", []
        new = [i for i in items if int(i.extend.dyn_id_str) > int(sub["offset"])]
        if new:
            new.sort(key=lambda i: int(i.extend.dyn_id_str))
            sub["offset"] = str(int(new[-1].extend.dyn_id_str))
        sub["next_ts"] = now + self.cfg.get("POLL_INTERVAL", 60)
        self.save()
        if not new:
            return "none", []
        limit = self.cfg.get("TEXT_LIMIT", 500)
        return "new", [d for d in (extract(i, limit) for i in new) if d]

    def due_uids(self, now: float) -> List[str]:
        return [u for u, s in self.state["subs"].items() if now >= s.get("next_ts", 0)]

    async def preview(self, uid: str, limit: int = 2) -> List[dict]:
        """拉取最新动态并渲染成推送预览，不读写任何推送状态（「测试动态」指令用）。

        被门控（空列表）时返回 []；硬错误抛 GrpcError。
        """
        rsp = await get_user_dynamics(
            uid,
            access_key=self.cfg.get("ACCESS_KEY", ""),
            proxy=self.cfg.get("PROXY", ""),
            timeout=10,
        )
        items = sorted(rsp.list, key=lambda i: int(i.extend.dyn_id_str), reverse=True)
        if not items:
            return []
        sub = self.state["subs"].get(uid)
        name = _author_name(items[0]) or (sub and sub.get("name")) or f"UID{uid}"
        out = []
        for it in items:
            d = extract(it, self.cfg.get("TEXT_LIMIT", 500))
            if d:
                d["name"] = name
                out.append(d)
                if len(out) >= limit:
                    break
        return out
