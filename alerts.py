"""
alerts.py - price alerts to your phone (ntfy) from data_cache/odds_snapshots.csv.

Runs right after 14_odds_snapshot.py (GitHub Action or Terminal). Compares the
newest snapshot with the previous one and with our first snapshot of each game,
and sends ONE ntfy message per run listing anything that trips a rule. A state
file (data_cache/alerts_state.json) makes sure each alert fires once.

Rules (price only - never "model favour"):
  1. BOOK OFF CONSENSUS  any book >= 1 point off the median across books
                         (spread or total). That is a price opportunity either way.
  2. MOVE SINCE OPEN     consensus moved >= 1.5 points since our first snapshot
                         of the game (re-fires at every further 1.5).
  3. KEY NUMBER          consensus spread crossed 3 or 7 since the previous snapshot.
  4. WATCHLIST           watchlist.csv (game,market,side,point): fires when any book
                         offers that side at that number or better.
  5. OPEN POSITION       positions.csv (game,market,side,point): fires when the
                         consensus has moved >= 1 point AGAINST the position.

Setup:  ntfy app on the phone, subscribe to a private topic, put the topic in the
        NTFY_TOPIC environment variable (GitHub secret) or in ntfy_topic.txt here.
Run:    python3 alerts.py --seed     (ONCE, first time: remember what is already true, send nothing)
        python3 alerts.py            (send new alerts)
        python3 alerts.py --dry-run  (print only, no send, no state change)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data_cache"
CSV = CACHE / "odds_snapshots.csv"
STATE = CACHE / "alerts_state.json"
WATCHLIST = HERE / "watchlist.csv"
POSITIONS = HERE / "positions.csv"
TOPIC_FILE = HERE / "ntfy_topic.txt"

BOOK_GAP = 1.0        # rule 1
MOVE_STEP = 1.5       # rule 2
KEY_NUMBERS = (3, 7)  # rule 3
POSITION_GAP = 1.0    # rule 5


def log(m=""):
    print(m, flush=True)


def fmt(x):
    return f"{x:+g}" if x < 0 or x > 0 else "0"


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def consensus_table(df):
    """One row per (snapshot, game, market): consensus point (home spread / Over total)."""
    sp = df[(df.market == "spreads") & (df.side == df.home)]
    to = df[(df.market == "totals") & (df.side == "Over")]
    both = pd.concat([sp, to])
    return (both.groupby(["snapshot_utc", "game", "market"])["point"]
                .median().rename("cons").reset_index())


def side_label(game, market, point_gap):
    """Who benefits from a book being `point_gap` above consensus."""
    away, home = game.split("@")
    if market == "spreads":
        return f"better for {home}" if point_gap > 0 else f"better for {away}"
    return "better for Under" if point_gap > 0 else "better for Over"


def crossed(prev, now, k):
    """Did |spread| cross key number k between prev and now (same sign)?"""
    if prev is None or pd.isna(prev) or pd.isna(now) or prev == now:
        return False
    if (prev < 0) != (now < 0) and prev != 0 and now != 0:
        return False
    a, b = abs(prev), abs(now)
    return (a < k <= b) or (b < k <= a) or (a <= k < b) or (b <= k < a)


def build_alerts(df, state, now_utc):
    alerts, new_state = [], dict(state)
    df = df.copy()
    df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
    df = df[df.kickoff_utc > now_utc]                      # upcoming games only
    if df.empty:
        return alerts, new_state
    snaps = sorted(df.snapshot_utc.unique())
    latest = snaps[-1]
    cons = consensus_table(df)
    cur = df[df.snapshot_utc == latest]
    c_latest = cons[cons.snapshot_utc == latest].set_index(["game", "market"])["cons"]
    c_first = cons.sort_values("snapshot_utc").groupby(["game", "market"])["cons"].first()
    prev_snap = cons[cons.snapshot_utc < latest].groupby(["game", "market"])["snapshot_utc"].max()
    c_prev = {}
    for (g, m), ts in prev_snap.items():
        c_prev[(g, m)] = cons[(cons.snapshot_utc == ts) & (cons.game == g) & (cons.market == m)]["cons"].iloc[0]

    def mark(key):
        if key in state:
            return False
        new_state[key] = latest
        return True

    # ---- rule 1: book off consensus ----------------------------------------
    sp = cur[(cur.market == "spreads") & (cur.side == cur.home)]
    to = cur[(cur.market == "totals") & (cur.side == "Over")]
    for sub, market in ((sp, "spreads"), (to, "totals")):
        for _, r in sub.dropna(subset=["point"]).iterrows():
            c = c_latest.get((r.game, market))
            if c is None or pd.isna(c):
                continue
            gap = r.point - c
            if abs(gap) >= BOOK_GAP:
                key = f"book|{r.game}|{market}|{r.book}|{r.point:g}"
                if mark(key):
                    what = "spread" if market == "spreads" else "total"
                    alerts.append(f"PRICE {r.game} {what}: {r.book} {r.point:g} vs consensus "
                                  f"{c:g} ({side_label(r.game, market, gap)})")

    # ---- rule 2: move since open --------------------------------------------
    for (g, m), c in c_latest.items():
        f = c_first.get((g, m))
        if f is None or pd.isna(f) or pd.isna(c):
            continue
        mv = c - f
        if abs(mv) >= MOVE_STEP:
            band = int(abs(mv) // MOVE_STEP)
            key = f"move|{g}|{m}|{band}|{'+' if mv > 0 else '-'}"
            if mark(key):
                what = "spread (home)" if m == "spreads" else "total"
                alerts.append(f"MOVE {g} {what}: open {f:g} -> now {c:g} ({fmt(mv)})")

    # ---- rule 3: key-number crossing (spreads) -----------------------------
    for (g, m), c in c_latest.items():
        if m != "spreads":
            continue
        p = c_prev.get((g, m))
        for k in KEY_NUMBERS:
            if crossed(p, c, k):
                key = f"key|{g}|{k}|{p:g}->{c:g}"
                if mark(key):
                    alerts.append(f"KEY {g} spread crossed {k}: {p:g} -> {c:g}")

    # ---- rule 4: watchlist ---------------------------------------------------
    if WATCHLIST.exists():
        wl = pd.read_csv(WATCHLIST, comment="#").dropna(subset=["game", "market", "side", "point"])
        for _, w in wl.iterrows():
            hits = cur[(cur.game == w.game) & (cur.market == w.market) & (cur.side == w.side)]
            if w.market == "totals":
                ok = hits[hits.point <= w.point] if w.side == "Over" else hits[hits.point >= w.point]
            else:
                ok = hits[hits.point >= w.point]           # a bigger number is always better for that side
            for _, h in ok.iterrows():
                key = f"watch|{w.game}|{w.market}|{w.side}|{h.book}|{h.point:g}"
                if mark(key):
                    alerts.append(f"WATCH {w.game} {w.side} {h.point:g} available at {h.book} "
                                  f"(wanted {w.point:g})")

    # ---- rule 5: open positions --------------------------------------------
    if POSITIONS.exists():
        ps = pd.read_csv(POSITIONS, comment="#").dropna(subset=["game", "market", "side", "point"])
        for _, p in ps.iterrows():
            c = c_latest.get((p.game, p.market))
            if c is None or pd.isna(c):
                continue
            away, home = p.game.split("@")
            if p.market == "totals":
                cons_side = c                              # Over-point convention
                against = (cons_side - p.point) if p.side == "Over" else (p.point - cons_side)
            else:
                cons_side = c if p.side == home else -c    # consensus from the bettor's side
                against = p.point - cons_side              # market now gives a worse number than we took
            if against >= POSITION_GAP:
                key = f"pos|{p.game}|{p.market}|{p.side}|{int(against // POSITION_GAP)}"
                if mark(key):
                    alerts.append(f"POSITION {p.game} {p.side} {p.point:g}: market now "
                                  f"{cons_side:g}, {against:g} pts against you")
    return alerts, new_state


def send(topic, title, body):
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                 headers={"Title": title, "Tags": "football"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print alerts, do not send or save state")
    ap.add_argument("--seed", action="store_true",
                    help="first run: mark everything current as already-alerted, send nothing")
    ap.add_argument("--csv", default=str(CSV))
    a = ap.parse_args()

    path = Path(a.csv)
    if not path.exists():
        sys.exit(f"No snapshot file at {path}")
    df = pd.read_csv(path)
    now_utc = datetime.now(timezone.utc)
    state = {} if a.dry_run else load_state()
    alerts, new_state = build_alerts(df, state, pd.Timestamp(now_utc))

    if a.seed:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(new_state, indent=0, sort_keys=True))
        log(f"Seeded {len(alerts)} current alerts into {STATE.name}; nothing sent. "
            "From now on only NEW alerts fire.")
        return 0
    if not alerts:
        log("No new alerts.")
        return 0
    body = "\n".join(alerts)
    title = f"NFL price alerts ({len(alerts)})"
    log(title); log(body)
    if a.dry_run:
        return 0

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and TOPIC_FILE.exists():
        topic = TOPIC_FILE.read_text().strip().split()[0]
    if not topic:
        log("NTFY_TOPIC not set - alerts printed only, state not saved.")
        return 0
    status = send(topic, title, body)
    log(f"ntfy -> HTTP {status}")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(new_state, indent=0, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
