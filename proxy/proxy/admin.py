"""Owner-side token management. Runs against the same SQLite file as the server.

    python -m proxy.admin mint --plan free [--label "zohar laptop"]
    python -m proxy.admin mint --plan pro --write-credentials PATH --proxy-url URL
    python -m proxy.admin list
    python -m proxy.admin revoke <hash prefix>        # from `list`
    python -m proxy.admin revoke dev_<id>             # the device id a user can quote
    python -m proxy.admin revoke --token-stdin        # when you hold the token itself
    python -m proxy.admin plan <hash prefix | dev_id> free|pro

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

from .config import Config
from .store import PLANS, Store, hash_token


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
    a = ap.parse_args(argv)

    db = Path(a.db).expanduser() if a.db else Config.from_env().db_path
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
        print(f"{'hash':14} {'device':21} {'plan':5} {'status':8} {'jev':>6} {'gemini':>6}"
              f"  label")
        for t in rows:
            status = "revoked" if t["revoked_at"] else "active"
            print(f"{t['token_hash'][:12]:14} {t['device_id']:21} {t['plan']:5} {status:8} "
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
