#!/usr/bin/env python3
"""
Daily meeting-topics reminder bot for Slack.

Run it on a schedule (e.g. every 15 minutes during the workday). Each run checks
the current local time and does whatever is due, so it's safe to run often and
tolerant of late/skipped cron runs:

  * After MORNING_TIME: if today's reminder hasn't been posted yet, post it.
  * In the window [MEETING_TIME - FOLLOWUP_LEAD_MINUTES, MEETING_TIME - CANCEL_LEAD_MINUTES):
      if nobody has replied in the reminder thread and no follow-up has been
      posted yet, post a follow-up reply in the same thread.
  * In the window [MEETING_TIME - CANCEL_LEAD_MINUTES, MEETING_TIME):
      if still nobody has replied, post a "we can skip today's meeting" reply
      in the same thread (also shown in the channel by default).

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
  MEETING_NAME           what messages call the meeting, default StandDown
  MENTION_GROUP_ID       user-group ID (S…) to @mention in the morning post, optional
  CANCEL_LEAD_MINUTES    default 15 (set to 0 to turn the skip notice off)
  BROADCAST_CANCEL       "false" to keep the skip notice in the thread only (default true)

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
        "name": env("MEETING_NAME", "StandDown"),
        "mention_group": (env("MENTION_GROUP_ID", "") or "").strip(),
        "cancel_lead": int(env("CANCEL_LEAD_MINUTES", "15")),
        "broadcast_cancel": env("BROADCAST_CANCEL", "true").lower() == "true",
    }


# ---------- messages ----------

def local_time(meeting_at):
    """Slack date token: each reader sees the time in their own Slack time zone.
    The text after | is the fallback for notifications/clients that can't render it."""
    fallback = meeting_at.strftime("%-I:%M %p %Z")
    return f"<!date^{int(meeting_at.timestamp())}^{{time}}|{fallback}>"


def mention(cfg):
    """User-group mention, e.g. <!subteam^S0123ABCD>, which notifies the group. Empty if not set."""
    gid = cfg["mention_group"]
    return f"<!subteam^{gid}> " if gid else ""


def morning_text(cfg, meeting_at):
    return (
        f"{mention(cfg)}:wave: Good morning! Reply in this thread with any topics for today's "
        f"{cfg['name']} at {local_time(meeting_at)}."
    )


def late_morning_text(cfg, meeting_at):
    # Used only if the morning post was missed entirely and we're already in the follow-up window.
    return (
        f"{mention(cfg)}:alarm_clock: Today's {cfg['name']} is at {local_time(meeting_at)}. "
        f"Reply in this thread with any topics you'd like to cover."
    )


def followup_text(cfg, meeting_at):
    return (
        f":alarm_clock: {cfg['name']} starts in {cfg['lead']} minutes ({local_time(meeting_at)}) "
        f"and no topics have been posted yet. Add anything you'd like to discuss here."
    )


def cancel_text(cfg, meeting_at):
    return f":no_entry_sign: We can skip today's {cfg['name']} since no topics were posted."


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
    """Return (human_reply_count, set of our reply kinds already posted)."""
    humans, ours, cursor = 0, set(), None
    while True:
        resp = client.conversations_replies(
            channel=channel, ts=root_ts, include_all_metadata=True, limit=200, cursor=cursor,
        )
        for m in resp["messages"]:
            if m.get("ts") == root_ts:
                continue
            kind = next((k for k in ("followup", "cancel") if is_ours(m, k, date_str)), None)
            if kind:
                ours.add(kind)
            elif not m.get("bot_id") and m.get("subtype") in (None, "thread_broadcast"):
                humans += 1
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return humans, ours


# ---------- core logic ----------

def run(client, cfg, now, dry_run=False):
    date_str = now.date().isoformat()

    if now.weekday() not in cfg["workdays"] or date_str in cfg["skip_dates"]:
        return "skip: not a meeting day"

    tz = cfg["tz"]
    morning_at = dt.datetime.combine(now.date(), cfg["morning"], tz)
    meeting_at = dt.datetime.combine(now.date(), cfg["meeting"], tz)
    followup_at = meeting_at - dt.timedelta(minutes=cfg["lead"])
    cancel_at = meeting_at - dt.timedelta(minutes=cfg["cancel_lead"]) if cfg["cancel_lead"] > 0 else meeting_at
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
        if now >= cancel_at:
            # Never asked the team for topics today, so don't cancel on them.
            return "skip: no reminder was posted today; too late to start one"
        text = morning_text(cfg, meeting_at) if now < followup_at else late_morning_text(cfg, meeting_at)
        post(text, "morning")
        return "posted: morning reminder"

    if now < followup_at:
        return "skip: morning already posted, follow-up not due"

    humans, ours = thread_status(client, cfg["channel"], root["ts"], date_str)
    if humans > 0:
        return f"skip: {humans} topic repl{'y' if humans == 1 else 'ies'} already"

    if now >= cancel_at:
        if "cancel" in ours:
            return "skip: skip notice already posted"
        post(cancel_text(cfg, meeting_at),"cancel", thread_ts=root["ts"], broadcast=cfg["broadcast_cancel"])
        return "posted: skip-meeting notice"

    if "followup" in ours:
        return "skip: follow-up already posted"
    post(followup_text(cfg, meeting_at),"followup", thread_ts=root["ts"], broadcast=cfg["broadcast"])
    return "posted: follow-up"


def parse_now(raw, tz):
    """Accept 'YYYY-MM-DD HH:MM' or just 'HH:MM' (today), tolerating quotes and stray spaces."""
    s = raw.strip().strip("\"'").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%H:%M"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            t = dt.datetime.combine(dt.datetime.now(tz).date(), t.time())
        return t.replace(tzinfo=tz)
    sys.exit(f'Could not read --now value {raw!r}. Use e.g. 2026-10-08 13:20 or 13:20 (no quotes needed).')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help='local time override, "YYYY-MM-DD HH:MM"')
    args = ap.parse_args()

    from slack_sdk import WebClient

    cfg = load_config()
    now = parse_now(args.now, cfg["tz"]) if args.now and args.now.strip() else dt.datetime.now(cfg["tz"])
    result = run(WebClient(token=cfg["token"]), cfg, now, dry_run=args.dry_run)
    if args.dry_run:
        result = result.replace("posted:", "would post:") + "  (dry run — nothing sent)"
    print(result)


if __name__ == "__main__":
    main()
