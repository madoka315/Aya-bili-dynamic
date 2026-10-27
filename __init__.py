# -*- coding: utf-8 -*-
import logging
import os
import time

from hoshino import Service, get_bot, priv
from hoshino.typing import CQEvent, MessageSegment

from .core import Poller
from .hb_grpc import GrpcError

try:
    from .config import (
        ACCESS_KEY, ERROR_BACKOFF, GATE_ALERT_BACKOFF, GATE_ALERT_N,
        DEFAULT_SUBS, IMAGES_LIMIT, MAX_POLLS_PER_TICK, MAX_USER_SUBS,
        POLL_INTERVAL, PROXY, RETRY_INTERVAL, TEXT_LIMIT, TICK_SECONDS,
    )
except ImportError:
    ACCESS_KEY = ""
    PROXY = ""
    POLL_INTERVAL = 300
    RETRY_INTERVAL = 60
    ERROR_BACKOFF = 600
    GATE_ALERT_N = 20
    GATE_ALERT_BACKOFF = 1800
    TICK_SECONDS = 60
    MAX_POLLS_PER_TICK = 2
    IMAGES_LIMIT = 3
    DEFAULT_SUBS = {}
    TEXT_LIMIT = 500
    MAX_USER_SUBS = 1

CFG = {
    "ACCESS_KEY": ACCESS_KEY, "PROXY": PROXY,
    "POLL_INTERVAL": POLL_INTERVAL, "RETRY_INTERVAL": RETRY_INTERVAL,
    "ERROR_BACKOFF": ERROR_BACKOFF, "GATE_ALERT_N": GATE_ALERT_N,
    "GATE_ALERT_BACKOFF": GATE_ALERT_BACKOFF,
    "TEXT_LIMIT": TEXT_LIMIT,
}

sv = Service("B站动态推送", help_="""B站UP主动态实时推送（游客模式）
[关注动态 UID 关键词1/关键词2] 关注UP主，有关键词则只推含关键词的动态
[取关动态 UID] 取消关注（普通用户只能取消自己添加的，群管理可取消本群任意关注）
[动态关注列表] 查看本群关注
[更新动态] 立即检查本群关注（群管）
[测试动态] 预览最新2条动态的真实推送效果（不计入推送状态，群管）
普通用户每人最多关注 1 个 UID，群管理不限""")
logger = logging.getLogger("hoshino.Aya-bili-dynamic")

poller = Poller(os.path.join(os.path.dirname(__file__), "data"), CFG)
_seeded = poller.seed_defaults(DEFAULT_SUBS)
if _seeded:
    logger.info(f"已按 DEFAULT_SUBS 预置 {_seeded} 条默认订阅")


def render(name: str, d: dict) -> str:
    parts = [f"{name} {d['label']}："]
    if d["text"]:
        parts.append(d["text"])
    for u in d["images"][:IMAGES_LIMIT]:
        parts.append(str(MessageSegment.image(u)))
    parts.append(d.get("link") or f"https://t.bilibili.com/{d['dyn_id']}")
    return "\n".join(p for p in parts if p)


async def push(uid: str, pushes: list):
    sub = poller.state["subs"].get(uid)
    if not sub or not pushes:
        return
    bot = get_bot()
    for gid_s, g in sub["groups"].items():
        kws = g.get("keywords") or []
        for d in pushes:
            haystack = (d.get("text_all") or d["text"] or "") + "\n" + (d["title"] or "")
            if kws and not any(k in haystack for k in kws):
                continue
            try:
                await bot.send_group_msg(
                    group_id=int(gid_s), message=render(sub["name"], d)
                )
            except Exception:
                logger.exception(f"推送动态到群 {gid_s} 失败（动态 {d['dyn_id']}）")


@sv.scheduled_job("interval", seconds=TICK_SECONDS)
async def _poll_tick():
    due = poller.due_uids(time.time())[:MAX_POLLS_PER_TICK]
    for uid in due:
        status, pushes = await poller.poll_uid(uid)
        if status == "new":
            logger.info(f"{poller.state['subs'][uid]['name']}({uid}) 有 {len(pushes)} 条新动态")
            await push(uid, pushes)


def _parse_args(ev: CQEvent, prefix: str):
    text = ev.message.extract_plain_text().strip()
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text.strip()


def _sender_name(ev: CQEvent) -> str:
    """记录关注者的群名片/昵称，仅用于列表展示；取不到就退回 QQ 号。"""
    try:
        sender = ev.sender or {}
        return sender.get("card") or sender.get("nickname") or str(ev.user_id)
    except (AttributeError, TypeError):
        return str(ev.user_id)


def _is_admin(ev: CQEvent) -> bool:
    return priv.check_priv(ev, priv.ADMIN)


def _group_only(ev: CQEvent) -> bool:
    """订阅是按群隔离的，私聊没有群号可用，直接拒绝。"""
    return ev["message_type"] == "group"


@sv.on_prefix("关注动态")
async def follow(bot, ev: CQEvent):
    if not _group_only(ev):
        await bot.send(ev, "该指令仅群聊可用")
        return
    arg = _parse_args(ev, "关注动态")
    parts = arg.split()
    if not parts or not parts[0].isdigit():
        await bot.send(
            ev,
            "用法：关注动态 <UID> [关键词1/关键词2]\n如：关注动态 517327498\n"
            f"普通用户每人最多关注 {MAX_USER_SUBS} 个 UID，群管理不限",
        )
        return
    uid = parts[0]
    kws = []
    for sep in parts[1:]:
        kws.extend(k for k in sep.replace("，", "/").replace(",", "/").split("/") if k)
    kws = list(dict.fromkeys(k.strip() for k in kws if k.strip()))[:10]

    admin = _is_admin(ev)
    if not admin:
        owned = poller.list_user(ev.group_id, ev.user_id)
        if uid not in [o["uid"] for o in owned] and len(owned) >= MAX_USER_SUBS:
            if owned:
                cur = owned[0]
                detail = (
                    f"你当前关注的是 {cur['name']}（{cur['uid']}）\n"
                    f"如需更换请先发送「取关动态 {cur['uid']}」"
                )
            else:
                detail = "当前未开放普通用户关注，请联系群管理"
            await bot.send(
                ev,
                f"普通用户每人最多关注 {MAX_USER_SUBS} 个 UID\n{detail}",
            )
            return
        existing = poller.get_sub(ev.group_id, uid)
        if existing and existing["owner"] != str(ev.user_id):
            await bot.send(ev, f"本群已关注 {existing['name']}（{uid}），无需重复关注")
            return

    ok, name = poller.subscribe(
        uid, ev.group_id, kws,
        owner="" if admin else str(ev.user_id),
        owner_name="" if admin else _sender_name(ev),
    )
    if not ok:
        await bot.send(ev, name)
        return
    tip = f"已关注 {name}（{uid}）" if not name.startswith("UID") else f"已关注 {uid}（首次拉取后将显示昵称）"
    if kws:
        tip += f"\n关键词过滤：{' / '.join(kws)}"
    if not admin:
        tip += f"\n（普通用户每人限 {MAX_USER_SUBS} 个，发送「取关动态 {uid}」可取消）"
    await bot.send(ev, tip)


@sv.on_prefix("取关动态")
async def unfollow(bot, ev: CQEvent):
    if not _group_only(ev):
        await bot.send(ev, "该指令仅群聊可用")
        return
    args = _parse_args(ev, "取关动态").split()
    if not args or not args[0].isdigit():
        owned = poller.list_user(ev.group_id, ev.user_id)
        tip = "用法：取关动态 <UID>"
        if owned:
            tip += "\n你当前关注：" + "、".join(f"{o['name']}（{o['uid']}）" for o in owned)
        await bot.send(ev, tip)
        return
    uid = args[0]
    existing = poller.get_sub(ev.group_id, uid)
    if not existing:
        await bot.send(ev, "本群没有关注这个 UID")
        return
    # 权限：群管理及以上可取消本群任意关注；普通用户只能取消自己添加的那条
    if not _is_admin(ev) and existing["owner"] != str(ev.user_id):
        await bot.send(ev, "这条关注不是你添加的，只有群管理可以取消")
        return
    ok, msg = poller.unsubscribe(uid, ev.group_id)
    await bot.send(ev, msg)


@sv.on_fullmatch("动态关注列表")
async def list_subs(bot, ev: CQEvent):
    if not _group_only(ev):
        await bot.send(ev, "该指令仅群聊可用")
        return
    rows = poller.list_group(ev.group_id)
    if not rows:
        await bot.send(
            ev,
            "本群还没有关注任何UP主\n"
            f"群成员可用「关注动态 <UID>」添加（每人最多 {MAX_USER_SUBS} 个）",
        )
        return
    lines = ["本群的动态关注："]
    for r in rows:
        kw = f"（关键词：{' / '.join(r['keywords'])}）" if r["keywords"] else ""
        who = f"（{r['owner_name'] or r['owner']} 关注）" if r["owner"] else ""
        lines.append(f"- {r['name']}（{r['uid']}）{kw}{who}")
    await bot.send(ev, "\n".join(lines))


@sv.on_fullmatch("更新动态")
async def force_update(bot, ev: CQEvent):
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.send(ev, "仅群管理可用")
        return
    uids = poller.group_uids(ev.group_id)[:5]
    if not uids:
        await bot.send(ev, "本群还没有关注任何UP主")
        return
    total = 0
    for uid in uids:
        status, pushes = await poller.poll_uid(uid, force=True)
        if status == "new":
            await push(uid, pushes)
            total += len(pushes)
    await bot.send(ev, f"检查完成，本群共 {total} 条新动态" if total else "检查完成，暂无新动态")


@sv.on_fullmatch("测试动态")
async def preview_dyn(bot, ev: CQEvent):
    """立即拉取最新动态并按真实渲染效果发到本群，不读写任何推送状态"""
    if not priv.check_priv(ev, priv.ADMIN):
        await bot.send(ev, "仅群管理可用")
        return
    rows = poller.list_group(ev.group_id)
    if not rows:
        await bot.send(ev, "本群还没有关注任何UP主")
        return
    uid = rows[0]["uid"]
    try:
        previews = await poller.preview(uid, limit=2)
    except GrpcError as e:
        await bot.send(ev, f"拉取失败：{e}")
        return
    if not previews:
        await bot.send(ev, "拉取被 B站 风控软门控（空列表），稍后再试")
        return
    for d in previews:
        try:
            await bot.send(ev, render(d["name"], d))
        except Exception:
            logger.exception(f"预览发送失败（动态 {d['dyn_id']}）")
    await bot.send(ev, "—— 以上为渲染预览（最新2条），不计入推送状态 ——")
