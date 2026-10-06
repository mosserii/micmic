"""Owner-side token management. Runs against the same SQLite file as the server.

    python -m proxy.admin mint --plan free [--label "zohar laptop"]
    python -m proxy.admin mint --plan pro --write-credentials PATH --proxy-url URL
    python -m proxy.admin list
    python -m proxy.admin revoke <hash prefix>        # from `list`
    python -m proxy.admin revoke dev_<id>             # the device id a user can quote
    python -m proxy.admin revoke --token-stdin        # when you hold the token itself
    python -m proxy.admin plan <hash prefix | dev_id> free|pro
    python -m proxy.admin delete <hash prefix | dev_id> --yes   # a Mac user's email request
    python -m proxy.admin diagnostics <dev_ prefix | hash prefix> [--limit 50] [--days 7] [--json]
    python -m proxy.admin diagnostics-purge                    # drop rows past DIAG_RETENTION_DAYS

A token is shown exactly once, at mint time, and only its hash is kept. Lose it and
the fix is to mint another and revoke the old one, which is the point: nothing the
server stores can be turned back into a working token.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import billing
from .config import Config
from .store import PLANS, PRO_STATUSES, Store, hash_token


def _one(store: Store, prefix: str) -> str | None:
    if prefix.startswith("dev_"):
        d = store.device(prefix)
        if d is None:
            print("no device with that id", file=sys.stderr)
            return None
        return d["token_hash"]
    if len(prefix) < 8:
        print("give at least 8 hex characters of the hash", file=sys.stderr)
        return None
    hits = store.resolve_prefix(prefix)
    if len(hits) != 1:
        print(f"{len(hits)} tokens match that prefix; need exactly one", file=sys.stderr)
        return None
    return hits[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m proxy.admin")
    ap.add_argument("--db", help="SQLite file (default MICMIC_PROXY_DB or proxy/data/proxy.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="create a device token and show it once")
    m.add_argument("--plan", choices=PLANS, required=True)
    m.add_argument("--label", default="")
    m.add_argument("--write-credentials", metavar="PATH",
                   help="write {proxy_url, token} to PATH (mode 600) instead of printing")
    m.add_argument("--proxy-url", default=None,
                   help="proxy URL to put in the credentials file")
    sub.add_parser("list", help="hashes, plans and today's usage; never tokens")
    r = sub.add_parser("revoke", help="stop a token working, immediately")
    r.add_argument("prefix", nargs="?")
    r.add_argument("--token-stdin", action="store_true")
    p = sub.add_parser("plan", help="move a token between free and pro")
    p.add_argument("prefix")
    p.add_argument("plan", choices=PLANS)
    d = sub.add_parser("delete", help="delete a device's account and data, for good "
                                      "(cancels its Stripe subscription first)")
    d.add_argument("prefix")
    d.add_argument("--yes", action="store_true", help="really delete; without it, only show")
    g = sub.add_parser("diagnostics", help="a device's recent diagnostics (Share diagnostics), "
                                          "newest last")
    g.add_argument("prefix", help="device id or its beginning (dev_...), or 8+ hex of the hash")
    g.add_argument("--limit", type=int, default=50)
    g.add_argument("--days", type=float, default=None, help="only the last N days")
    g.add_argument("--json", action="store_true", help="one JSON object per line, as stored")
    sub.add_parser("diagnostics-purge", help="delete diagnostics older than DIAG_RETENTION_DAYS")
    a = ap.parse_args(argv)

    cfg = Config.from_env()
    db = Path(a.db).expanduser() if a.db else cfg.db_path
    store = Store(db)

    if a.cmd == "mint":
        token = store.mint(a.plan, a.label)
        if a.write_credentials:
            url = a.proxy_url or f"http://127.0.0.1:{os.environ.get('MICMIC_PROXY_PORT', '8810')}"
            out = Path(a.write_credentials).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"proxy_url": url, "token": token}, f)
            print(f"minted {a.plan} token {hash_token(token)[:12]}, written to {out}")
        else:
            print(token)
            print(f"(plan {a.plan}, hash {hash_token(token)[:12]}; this is the only time "
                  f"the token is shown)", file=sys.stderr)
        return 0

    if a.cmd == "list":
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = store.list_tokens(day)
        print(f"{'hash':14} {'device':21} {'plan':5} {'status':8} {'os':4} {'pro via':8} "
              f"{'apple env':10} {'jev':>6} {'gemini':>6}  label")
        for t in rows:
            status = "revoked" if t["revoked_at"] else "active"
            print(f"{t['token_hash'][:12]:14} {t['device_id']:21} {t['plan']:5} {status:8} "
                  f"{t['platform']:4} {t['pro_source'] or '-':8} {t['apple_env'] or '-':10} "
                  f"{t['jev_today']:>6} {t['gemini_today']:>6}  {t['label']}")
        print(f"{len(rows)} token(s); usage is for UTC {day}")
        return 0

    if a.cmd == "revoke":
        if a.token_stdin:
            h = hash_token(sys.stdin.readline().strip())
        elif a.prefix:
            h = _one(store, a.prefix)
            if h is None:
                return 1
        else:
            print("give a hash prefix or --token-stdin", file=sys.stderr)
            return 1
        if store.revoke(h):
            print(f"revoked {h[:12]}")
            return 0
        print("no active token with that hash", file=sys.stderr)
        return 1

    if a.cmd == "plan":
        h = _one(store, a.prefix)
        if h is None or not store.set_plan(h, a.plan):
            return 1
        print(f"{h[:12]} is now {a.plan}")
        return 0

    if a.cmd == "delete":
        # Steps 2 and 4 of PURCHASES_ACCOUNT.md 3.3. The brain is iPhone-only, and an
        # iPhone user deletes from the app, which also erases the brain's notes.
        h = _one(store, a.prefix)
        if h is None:
            return 1
        acct = store.account(h)
        live_stripe = bool(acct["stripe_subscription_id"] and acct["sub_status"] in PRO_STATUSES
                           and not acct["sub_cancel_at_period_end"])
        if not a.yes:
            print(f"would delete {h[:12]} {acct['device_id']} ({acct['platform']}, "
                  f"{acct['label']}){'; cancels its Stripe subscription' if live_stripe else ''}"
                  "; run again with --yes")
            return 1
        if live_stripe:
            try:
                billing.cancel_for_deletion(cfg, acct["stripe_subscription_id"])
            except (billing.PaymentsUnavailable, billing.StripeFailed) as e:
                print(f"Stripe cancel failed ({type(e).__name__}); nothing deleted",
                      file=sys.stderr)
                return 1
        now = int(datetime.now(timezone.utc).timestamp())
        res = store.delete_account(h, now, cfg.apple_expiry_leeway)
        if res is None:
            print("no device with that hash", file=sys.stderr)
            return 1
        print(f"deleted {h[:12]} {res['device_id']}"
              + ("; it has a live Apple subscription, which only the user can cancel"
                 if res["apple_subscription_active"] else ""))
        return 0
    if a.cmd == "diagnostics":
        device_id = _device(store, a.prefix)
        if device_id is None:
            return 1
        since = None
        if a.days is not None:
            since = int(datetime.now(timezone.utc).timestamp() - a.days * 86400)
        rows = store.diagnostics_for(device_id, a.limit, since)
        for r in rows:
            if a.json:
                print(json.dumps({"received_at": r["received_at"], "client_at": r["client_at"],
                                  "kind": r["kind"], "app_version": r["app_version"],
                                  "item": json.loads(r["body"])}, ensure_ascii=False))
            else:
                print(_diag_line(r))
        print(f"{len(rows)} item(s) for {device_id}", file=sys.stderr)
        return 0

    if a.cmd == "diagnostics-purge":
        cutoff = int(datetime.now(timezone.utc).timestamp()) - cfg.diag_retention_days * 86400
        n = store.purge_diagnostics(cutoff)
        print(f"purged {n} diagnostics row(s) older than {cfg.diag_retention_days} days")
        return 0
    return 1


def _device(store: Store, prefix: str) -> str | None:
    """A device id from a dev_ prefix (any length that matches one device) or from 8+
    hex characters of the token hash."""
    if prefix.startswith("dev_"):
        hits = store.resolve_device_prefix(prefix)
        if len(hits) != 1:
            print(f"{len(hits)} devices match that prefix; need exactly one", file=sys.stderr)
            return None
        return hits[0]
    h = _one(store, prefix)
    if h is None:
        return None
    acct = store.account(h)
    return acct["device_id"] if acct else None


def _diag_line(r: dict) -> str:
    """One turn, readable: when, how it started, what was heard, what was sent, what
    came back, where the time went and what went wrong."""
    try:
        it = json.loads(r["body"])
    except ValueError:
        return f"{r['received_at']} (unreadable row)"
    when = it.get("at") or datetime.fromtimestamp(r["received_at"], timezone.utc).isoformat()
    parts = [str(when), str(it.get("activation") or "-")]
    # How the press went (S9, build 6): a short press is a tap that listens until the
    # next tap, a long one a hold sent on release; handler lag is what made taps read as
    # holds on the Mac. Shown when the phone sent them.
    talk = [f"{k}={_short(it[k])}" for k in _TALK_FIELDS if k in it and it[k] is not None]
    if talk:
        parts.append(" ".join(talk))
    sent = it.get("sent") if isinstance(it.get("sent"), dict) else {}
    # A reason always wins: a turn that stopped on the phone (AI services off, superseded,
    # dropped, answered on the phone) never reads as sent, even if an older build kept words.
    if sent.get("reason") or not sent.get("text"):
        parts.append(f"NOT SENT ({sent.get('reason') or it.get('outcome') or '?'})")
        held = sent.get("held") or (sent.get("text") if sent.get("reason") else None)
        if held:
            parts.append(f'held="{held}"')
    else:
        parts.append(f'sent="{sent["text"]}"')
    for rec in it.get("recognizers") or []:
        if isinstance(rec, dict):
            parts.append(f"{rec.get('engine')}=\"{rec.get('final') or ''}\""
                         f"{' err=' + str(rec['error']) if rec.get('error') else ''}")
    brain = it.get("brain") if isinstance(it.get("brain"), dict) else {}
    if brain:
        parts.append(f"brain={brain.get('status') or '-'} did={brain.get('did') or '-'} "
                     f"card={brain.get('card') or '-'} rt={brain.get('round_trip_ms') or '-'} "
                     f"server={brain.get('server_ms') or '-'}"
                     + (f" err={brain['error']}" if brain.get("error") else ""))
        if brain.get("say"):
            parts.append(f'said="{brain["say"]}"')
    marks = it.get("marks") if isinstance(it.get("marks"), dict) else {}
    if marks:
        # The known marks in the order a turn passes them, then any mark a newer build
        # adds, as sent: an additive field is never dropped from the owner's view.
        order = ("mic_open", "first_buffer", "first_partial", "end", "drain_ms", "finalize_ms",
                 "final", "sent", "reply", "first_audio", "spoken", "done")
        keys = [k for k in order if k in marks] + [k for k in marks if k not in order]
        parts.append("ms " + " ".join(f"{k}={_short(marks[k])}" for k in keys))
    if it.get("errors"):
        parts.append(f"errors={it['errors']}")
    extra = [f"{k}={_short(v)}" for k, v in it.items()
             if k not in _SHOWN and k not in _TALK_FIELDS and v not in (None, "", [], {})]
    if extra:
        parts.append("+ " + " ".join(extra))
    return " | ".join(parts)


# S9 (2026-10-05): the phone's talk button and listener, per turn.
_TALK_FIELDS = ("press_ms", "handler_lag_ms", "cancel_by", "own_playback", "final_rule")
# Fields the line above already shows, or that are only the item's own framing.
_SHOWN = {"v", "kind", "at", "activation", "sent", "recognizers", "brain", "marks", "errors",
          "outcome"}


def _short(v) -> str:
    """One value for the readable line: scalars as they are, anything else as compact
    JSON, cut at 120 characters (the --json view keeps everything)."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        text = str(v)
    else:
        text = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 120 else text[:117] + "..."


if __name__ == "__main__":
    sys.exit(main())
