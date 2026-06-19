#!/usr/bin/env python3
"""
elo_nudge.py — cheap odds-drift adjustment for elo.json.

build_elos.py is the heavy, occasional calibration (a 100k-sim Monte Carlo).
Between rebuilds, when the Kalshi odds wobble, you don't need to re-simulate
anything: each team's rating just slides a little off its calibrated anchor in
proportion to how far its current championship odds have drifted from the
baseline it was calibrated against.

    rating = calibratedRating + nudgeK * (logit(currentOdds) - logit(baselineOdds))

It always recomputes from the stored `calibrated` anchor, so repeated calls
never accumulate drift — re-run build_elos.py after each matchday (results
change the bracket and form) to re-anchor.

Usage from update_odds.py — after you've built your per-team odds, call:

    import elo_nudge
    elo_nudge.apply_odds_drift("elo.json", odds_by_team)

where `odds_by_team` maps each team's *canonical / leaderboard* name (the same
name you write into odds.json's standings[].teams[].name) to its current
championship probability (0..1). If you'd rather, pass the path to odds.json
itself and it'll read the odds out of the file:

    elo_nudge.apply_odds_drift("elo.json", "odds.json")
"""

import json
import math

EPS = 1e-4


def _logit(p):
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def _odds_by_canon(source):
    """Accepts a dict {canonName: prob} or a path to an odds.json file."""
    if isinstance(source, dict):
        return dict(source)
    odds = json.load(open(source, encoding="utf-8"))
    out = {}
    for st in odds.get("standings", []):
        for t in st.get("teams", []):
            out[t["name"]] = t.get("odds") or 0.0
    return out


def apply_odds_drift(elo_path, odds_source, write=True):
    elo = json.load(open(elo_path, encoding="utf-8"))
    anchor = elo.get("calibrated") or elo["ratings"]
    baseline = elo.get("baselineProb", {})
    canon = elo.get("canon", {})
    nudge_k = (elo.get("params") or {}).get("nudgeK", 0.55)
    cur = _odds_by_canon(odds_source)

    ratings = dict(elo["ratings"])
    moved = 0
    for disp, base in baseline.items():
        if base is None or base <= EPS:
            continue                              # unpriced team: leave its rank rating
        c = canon.get(disp, disp)
        now = cur.get(c)
        if now is None or now <= 0:
            continue
        ratings[disp] = round(anchor[disp] + nudge_k * (_logit(now) - _logit(base)), 4)
        moved += 1

    elo["ratings"] = ratings
    if write:
        json.dump(elo, open(elo_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    return moved


if __name__ == "__main__":   # quick CLI: python3 elo_nudge.py elo.json odds.json
    import sys
    e = sys.argv[1] if len(sys.argv) > 1 else "elo.json"
    o = sys.argv[2] if len(sys.argv) > 2 else "odds.json"
    n = apply_odds_drift(e, o)
    print("nudged %d priced teams in %s from %s" % (n, e, o))
