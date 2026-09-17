"""
14_odds_snapshot.py - log today's NFL odds from every US book (The Odds API).

Each run appends one snapshot to data_cache/odds_snapshots.csv and prints, per
game: the FanDuel line, the best available number on each side, and how far
the consensus has moved since our first snapshot of that game. Run it daily
(the scheduled task does) - the history is what makes CLV and open-vs-close
measurable.

Key: paste it into odds_api_key.txt in this folder (one line, nothing else),
or set the ODDS_API_KEY environment variable (GitHub Actions does this).
Cost: 3 credits per run (spreads, totals, moneylines x 1 region). Free tier
is 500 / month.

Run:  python3 14_odds_snapshot.py
      python3 14_odds_snapshot.py --quiet   (no table, just log)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data_cache"
CACHE.mkdir(exist_ok=True)
KEY_FILE = HERE / "odds_api_key.txt"
OUT = CACHE / "odds_snapshots.csv"
# On the Mac the 7 AM background job runs from ~/NFL_Odds (macOS blocks background
# jobs from touching Desktop). Every run merges both stores so they stay identical.
ALT = Path.home() / "NFL_Odds" / "data_cache" / "odds_snapshots.csv"
STORES = [OUT] + ([ALT] if ALT.resolve() != OUT.resolve() and ALT.exists() else [])
KEY_COLS = ["snapshot_utc", "game", "book", "market", "side"]
BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
PREFERRED_BOOK = "fanduel"

# The Odds API uses full names; our files use nflverse abbreviations.
NAME2ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Los Angeles Rams": "LA", "Los Angeles Chargers": "LAC",
    "Las Vegas Raiders": "LV", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "Seattle Seahawks": "SEA", "San Francisco 49ers": "SF", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def log(m=""):
    print(m, flush=True)


def abbr(name):
    return NAME2ABBR.get(name, name)


def fetch(key):
    q = urllib.parse.urlencode({"apiKey": key, "regions": "us", "markets": "spreads,totals,h2h",
                                "oddsFormat": "american"})
    req = urllib.request.Request(BASE + "?" + q, headers={"User-Agent": "nfl-model"})
    with urllib.request.urlopen(req, timeout=60) as r:
        remaining = r.headers.get("x-requests-remaining")
        used = r.headers.get("x-requests-used")
        return json.loads(r.read().decode()), remaining, used


def flatten(games, snap_ts):
    rows = []
    for g in games:
        home, away = abbr(g["home_team"]), abbr(g["away_team"])
        kick = g["commence_time"]
        for bk in g.get("bookmakers", []):
            book = bk["key"]
            for mk in bk.get("markets", []):
                for oc in mk.get("outcomes", []):
                    rows.append(dict(
                        snapshot_utc=snap_ts, kickoff_utc=kick, home=home, away=away,
                        game=f"{away}@{home}", book=book, market=mk["key"],
                        side=abbr(oc["name"]), point=oc.get("point"), price=oc.get("price")))
    return pd.DataFrame(rows)


def summarise(df, hist):
    """One line per game: FanDuel number, best number each side, move since first snapshot."""
    out = []
    for game, g in df.groupby("game", sort=False):
        home, away = g["home"].iloc[0], g["away"].iloc[0]
        kick = pd.Timestamp(g["kickoff_utc"].iloc[0]).tz_convert("US/Pacific").strftime("%a %-m/%-d %-I:%M%p")
        sp = g[g.market == "spreads"]; to = g[g.market == "totals"]
        fd_sp = sp[(sp.book == PREFERRED_BOOK) & (sp.side == home)]
        fd_to = to[(to.book == PREFERRED_BOOK) & (to.side == "Over")]
        cons_sp = sp[sp.side == home]["point"].median()
        cons_to = to[to.side == "Over"]["point"].median()
        best_home = sp[sp.side == home].sort_values("point", ascending=False).head(1)
        best_away = sp[sp.side == away].sort_values("point", ascending=False).head(1)
        best_over = to[to.side == "Over"].sort_values("point").head(1)
        best_under = to[to.side == "Under"].sort_values("point", ascending=False).head(1)
        h = hist[(hist.game == game) & (hist.market == "spreads") & (hist.side == home)]
        h_to = hist[(hist.game == game) & (hist.market == "totals") & (hist.side == "Over")]
        first_sp = h.sort_values("snapshot_utc").groupby("snapshot_utc")["point"].median()
        first_to = h_to.sort_values("snapshot_utc").groupby("snapshot_utc")["point"].median()
        mv_sp = (cons_sp - first_sp.iloc[0]) if len(first_sp) else 0.0
        mv_to = (cons_to - first_to.iloc[0]) if len(first_to) else 0.0
        n_snaps = first_sp.index.nunique()

        def fmt_pt(x):
            return "" if x is None or pd.isna(x) else f"{x:+g}"

        out.append(dict(
            game=game, kick=kick, books=g["book"].nunique(),
            fd_spread=(fmt_pt(fd_sp["point"].iloc[0]) if len(fd_sp) else "-"),
            cons_spread=fmt_pt(cons_sp),
            best_home=(f"{home} {fmt_pt(best_home['point'].iloc[0])} ({best_home['book'].iloc[0]})" if len(best_home) else "-"),
            best_away=(f"{away} {fmt_pt(best_away['point'].iloc[0])} ({best_away['book'].iloc[0]})" if len(best_away) else "-"),
            fd_total=(f"{fd_to['point'].iloc[0]:g}" if len(fd_to) else "-"),
            cons_total=(f"{cons_to:g}" if pd.notna(cons_to) else "-"),
            best_over=(f"{best_over['point'].iloc[0]:g} ({best_over['book'].iloc[0]})" if len(best_over) else "-"),
            best_under=(f"{best_under['point'].iloc[0]:g} ({best_under['book'].iloc[0]})" if len(best_under) else "-"),
            move_spread=f"{mv_sp:+.1f}" if n_snaps > 1 else "first",
            move_total=f"{mv_to:+.1f}" if n_snaps > 1 else "first",
        ))
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("ODDS_API_KEY", "").strip()      # GitHub Actions passes it this way
    if not key:
        if not KEY_FILE.exists():
            sys.exit(f"No key. Create {KEY_FILE.name} in this folder with your Odds API key on one line, "
                     "or set ODDS_API_KEY.")
        key = KEY_FILE.read_text().strip().split()[0]
    if len(key) < 20:
        sys.exit("The key does not look right (too short).")

    games, remaining, used = fetch(key)
    snap_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    df = flatten(games, snap_ts)
    if df.empty:
        sys.exit("The API returned no games. Off-season, or the key is wrong.")

    parts = [pd.read_csv(p) for p in STORES if p.exists()]
    hist = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=df.columns)
    allrows = (pd.concat([hist, df], ignore_index=True)
                 .drop_duplicates(subset=KEY_COLS, keep="last")
                 .sort_values(["snapshot_utc", "game", "book", "market"]).reset_index(drop=True))
    for p in STORES:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            allrows.to_csv(p, index=False)
        except OSError:
            pass          # the other store is not reachable from here - fine

    log(f"Snapshot {snap_ts}: {df['game'].nunique()} games, {df['book'].nunique()} books, "
        f"{len(df):,} rows -> {OUT.name} (now {allrows['snapshot_utc'].nunique()} snapshots, {len(allrows):,} rows)")
    log(f"Odds API credits used {used}, remaining {remaining}")
    if a.quiet:
        return 0
    s = summarise(df, allrows)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20)
    log("")
    log(s.to_string(index=False))
    log("")
    log("best_home/best_away = the most favourable spread for that side across books.")
    log("move_* = consensus change since our FIRST snapshot of the game (+ = toward home / over).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
