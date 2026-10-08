#!/usr/bin/env python3
"""
Daily meeting-topics reminder bot for Slack.

Run it on a schedule (e.g. every 15 minutes during the workday). Each run checks
the current local time and does whatever is due, so it's safe to run often and
tolerant of late/skipped cron runs:

  * After MORNING_TIME: if today's reminder hasn't been posted yet, post it.
  * In the window [MEETING_TIME - FOLLOWUP_LEAD_MINUTES, MEETING_TIME):
      if nobody has replied in the reminder thread and no follow-up has been
      posted yet, post a follow-up reply in the same thread.

State is kept in Slack itself (via message metadata), so there's no database.

Env vars:
  SLACK_BOT_TOKEN        xoxb-... (required)
  SLACK_CHANNEL_ID       e.g. C0123ABCD (required — the ID, not the #name)
  TEAM_TZ                IANA zone, default America/Los_Angeles
  MORNING_TIME           HH:MM, default 09:00
  MEETING_TIME           HH:MM, default 15:00
  FOLLOWUP_LEAD_MINUTES  default 60
  WORKDAYS               comma list of weekday numbers, Mon=0, default 0,1,2,3,4
  SKIP_DATES             comma list of YYYY-MM-DD (holidays), optional
  BROADCAST_FOLLOWUP     "true" to also show the follow-up in the channel

CLI:
  --dry-run              print what would be posted, don't post
  --now "YYYY-MM-DD HH:MM"   pretend it's this local time (for testing)
"""

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

EVENT_TYPE = "meeting_topics_reminder"


# ---------- config ----------

def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required env var {name}")
    return val


def parse_hhmm(s):
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


def load_config():
    return {
        "token": env("SLACK_BOT_TOKEN", required=True),
        "channel": env("SLACK_CHANNEL_ID", required=True),
        "tz": ZoneInfo(env("TEAM_TZ", "America/Los_Angeles")),
        "morning": parse_hhmm(env("MORNING_TIME", "09:00")),
        "meeting": parse_hhmm(env("MEETING_TIME", "15:00")),
        "lead": int(env("FOLLOWUP_LEAD_MINUTES", "60")),
        "workdays": {int(d) for d in env("WORKDAYS", "0,1,2,3,4").split(",") if d.strip()},
        "skip_dates": {d.strip() for d in env("SKIP_DATES", "").split(",") if d.strip()},
        "broadcast": env("BROADCAST_FOLLOWUP", "false").lower() == "true",
    }


# ---------- messages ----------

def fmt_time(t):
    return dt.datetime.combine(dt.date.today(), t).strftime("%-I:%M %p")


def morning_text(cfg):
    return (
        f":wave: Good morning! Reply in this thread with any topics for today's "
        f"{fmt_time(cfg['meeting'])} meeting."
    )


def late_morning_text(cfg):
    # Used only if the morning post was missed entirely and we're already in the follow-up window.
    return (
        f":alarm_clock: Today's meeting is at {fmt_time(cfg['meeting'])}. "
        f"Reply in this thread with any topics you'd like to cover."
    )


def followup_text(cfg):
    return (
        f":alarm_clock: Meeting starts in {cfg['lead']} minutes and no topics have been "
        f"posted yet. Add anything you'd like to discuss here."
    )


# ---------- Slack helpers ----------

def metadata(kind, date_str):
    return {"event_type": EVENT_TYPE, "event_payload": {"date": date_str, "kind": kind}}


def is_ours(msg, kind, date_str):
    md = msg.get("metadata") or {}
    p = md.get("event_payload") or {}
    return md.get("event_type") == EVENT_TYPE and p.get("kind") == kind and p.get("date") == date_str


def find_root(client, channel, since_ts, date_str):
    cursor = None
    while True:
        resp = client.conversations_history(
            channel=channel, oldest=str(since_ts), include_all_metadata=True,
            limit=200, cursor=cursor,
        )
        for m in resp["messages"]:
            if is_ours(m, "morning", date_str):
                return m
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None


def thread_status(client, channel, root_ts, date_str):
    """Return (human_reply_count, followup_already_posted)."""
    humans, followed_up, cursor = 0, False, None
    while True:
        resp = client.conversations_replies(
            channel=channel, ts=root_ts, include_all_metadata=True, limit=200, cursor=cursor,
        )
        for m in resp["messages"]:
            if m.get("ts") == root_ts:
                continue
            if is_ours(m, "followup", date_str):
                followed_up = True
            elif not m.get("bot_id") and not m.get("subtype"):
                humans += 1
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return humans, followed_up


# ---------- core logic ----------

def run(client, cfg, now, dry_run=False):
    date_str = now.date().isoformat()

    if now.weekday() not in cfg["workdays"] or date_str in cfg["skip_dates"]:
        return "skip: not a meeting day"

    tz = cfg["tz"]
    morning_at = dt.datetime.combine(now.date(), cfg["morning"], tz)
    meeting_at = dt.datetime.combine(now.date(), cfg["meeting"], tz)
    followup_at = meeting_at - dt.timedelta(minutes=cfg["lead"])
    day_start = dt.datetime.combine(now.date(), dt.time(0, 0), tz)

    if now < morning_at or now >= meeting_at:
        return "skip: outside reminder hours"

    def post(text, kind, thread_ts=None, broadcast=False):
        if dry_run:
            where = f"thread {thread_ts}" if thread_ts else "channel"
            print(f"[dry-run] would post to {where}: {text}")
            return
        kwargs = dict(channel=cfg["channel"], text=text, metadata=metadata(kind, date_str))
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
            kwargs["reply_broadcast"] = broadcast
        client.chat_postMessage(**kwargs)

    root = find_root(client, cfg["channel"], day_start.timestamp(), date_str)

    if root is None:
        text = morning_text(cfg) if now < followup_at else late_morning_text(cfg)
        post(text, "morning")
        return "posted: morning reminder"

    if now < followup_at:
        return "skip: morning already posted, follow-up not due"

    humans, followed_up = thread_status(client, cfg["channel"], root["ts"], date_str)
    if humans > 0:
        return f"skip: {humans} topic repl{'y' if humans == 1 else 'ies'} already"
    if followed_up:
        return "skip: follow-up already posted"

    post(followup_text(cfg), "followup", thread_ts=root["ts"], broadcast=cfg["broadcast"])
    return "posted: follow-up"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help='local time override, "YYYY-MM-DD HH:MM"')
    args = ap.parse_args()

    from slack_sdk import WebClient

    cfg = load_config()
    now = (
        dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=cfg["tz"])
        if args.now else dt.datetime.now(cfg["tz"])
    )
    print(run(WebClient(token=cfg["token"]), cfg, now, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
