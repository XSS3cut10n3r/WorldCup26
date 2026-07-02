#!/usr/bin/env python3
"""
simulate_pool.py — Monte Carlo for The Family Cup.

Runs the SAME strength-weighted tournament simulator that lives in index.html
(buildSimData / eloScore), but in Python and tens of thousands of times, then
reports how often each person (the owner of a set of countries) finishes top of
the pool standings.

It reads:
  * elo.json   — team ratings + model params (the "weights"). Missing params fall
                 back to the exact same defaults index.html uses.
  * data.json  — the sim block (who owns which country, the scoring table, the
                 current group results, the remaining fixtures, the Annex-C
                 bracket template, any knockout games already played) and the
                 current leaderboard (the points already banked).

Each simulated tournament plays out everything still to come from the current
state, adds the points each country earns to its owner, and the person with the
most total points wins that run. Probabilities are the share of runs each person
wins (ties split the win evenly).

    python3 simulate_pool.py                 # 1,000,000 runs, all CPU cores
    python3 simulate_pool.py --sims 20000
    python3 simulate_pool.py --seed 1 --sims 5000
    python3 simulate_pool.py --workers 1     # original single-process behaviour
    python3 simulate_pool.py --json sim_odds.json --knockout-json knockout_odds.json

This is the aggregate of the site's "Simulate" button: at the start of the
tournament it is a clean from-scratch projection off the Elos and weights; once
games are played it conditions on the real results, exactly like the page.

Alongside the pool-winner odds (--json), the same run can emit per-team odds of
reaching the Round of 32 (--knockout-json). Those come for free: every simulated
tournament already decides the 12 group winners, 12 runners-up, and 8 best
thirds — that set IS the 32 teams that made the knockouts — so a team's odds are
just the share of runs it lands in that set. A clinched team comes out at ~1.0,
an eliminated team at ~0.0, everyone else in between. The file mirrors the
sim_odds.json shape (per-row base/delta trend), and carries ONLY the model
output (name + odds); the page joins group / owner / clinched / eliminated /
FIFA rank from data.json.

Speed: the work is split across CPU cores (multiprocessing). Each worker runs an
independent slice of the sims with its own seeded RNG and the SAME model, and the
per-person win/points/title and per-team knockout tallies are summed at the end —
statistically identical to running every sim in one process, just much faster.
Runs faster still under PyPy with no code change.
"""

import argparse
import json
import math
import os
import random
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool


# ----------------------------------------------------------------------------
# Load inputs
# ----------------------------------------------------------------------------
def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_model(elo, data):
    """Return a dict of everything the per-run simulation needs, precomputing the
    fixed pieces (ratings, params, live-form tilts) once."""
    R = (elo or {}).get("ratings") or None
    P = (elo or {}).get("params") or {}
    sim = data["sim"]
    T = sim["teams"]
    sc = sim["scoring"]

    # --- params, with the SAME fallbacks as index.html -----------------------
    p = {
        "suprK": P.get("suprK", 0.42),
        "total": P.get("total", 2.6),
        "floor": P.get("floor", 0.18),
        # Penalty-shootout model: near-coin-flip, tiny nudges only (see
        # shoot_win). Elo plays no part.
        "pensFormK": P.get("pensFormK", 0.06),
        "pensResK": P.get("pensResK", 0.10),
        "pensWonK": P.get("pensWonK", 0.15),
        "pensCap": P.get("pensCap", 0.60),
        "atkCoef": P.get("atkCoef", 0.0),
        "defCoef": P.get("defCoef", 0.0),
        "rankCoef": P.get("rankCoef", 0.0),
        "rankCoefGroup": P.get("rankCoefGroup", None),
        "formCap": P.get("formCap", 0.4),
        "formOppCoef": P.get("formOppCoef", 0.4),
        "formAtkFloor": P.get("formAtkFloor", 1.0),
        "formDefFloor": P.get("formDefFloor", 0.0),
        "pensHangover": P.get("pensHangover", 0.0),
        "finalScoreBoost": P.get("finalScoreBoost", 1.0),
        "koTotalMul": P.get("koTotalMul", 0.91),
        "etTotal": P.get("etTotal", 0.75),
        "dcRho": P.get("dcRho", -0.30),
    }

    owner_of = {nm: (T[nm].get("owner")) for nm in T}
    canon_of = {nm: (T[nm].get("canon")) for nm in T}
    fifa_of = {nm: (T[nm].get("fifa")) for nm in T}
    conduct_of = {nm: (T[nm].get("conduct", 0) or 0) for nm in T}

    def rs(t):  # rank strength: higher (less negative) for better-ranked sides
        fr = fifa_of.get(t)
        return -math.log(fr or 40)

    # --- live form (opponent-weighted), computed once from played games ------
    played = []
    for L, games in (sim.get("groupResults") or {}).items():
        for m in games:
            played.append((m["h"], m["a"], m["hg"], m["ag"]))
    for k in (sim.get("knockoutPlayed") or []):
        if k.get("home") and k.get("away") and k.get("homeGoals") is not None \
                and k.get("awayGoals") is not None:
            played.append((k["home"], k["away"], k["homeGoals"], k["awayGoals"]))

    totG = sum(hg + ag for (_, _, hg, ag) in played)
    avgGpg = (totG / (2 * len(played))) if played else 1.3

    rs_vals = [rs(t) for t in T]
    rs_bar = (sum(rs_vals) / len(rs_vals)) if rs_vals else 0.0
    qCoef = p["formOppCoef"]
    oppW = lambda opp: math.exp(qCoef * (rs(opp) - rs_bar))

    fa = {}
    atkFloor, defFloor = p["formAtkFloor"], p["formDefFloor"]

    def bump(t, opp, gf, ga):
        wStrong = oppW(opp)
        wInv = 1.0 / wStrong
        f = fa.setdefault(t, {"an": 0.0, "dn": 0.0, "n": 0})
        devA = gf - avgGpg
        f["an"] += (wStrong if devA >= 0 else max(atkFloor, wInv)) * devA
        devD = ga - avgGpg
        f["dn"] += (max(defFloor, wInv) if devD > 0 else wStrong) * devD
        f["n"] += 1

    for (h, a, hg, ag) in played:
        bump(h, a, hg, ag)
        bump(a, h, ag, hg)

    def atk(t):
        f = fa.get(t)
        return (f["an"] / f["n"]) if (f and f["n"]) else 0.0

    def deff(t):
        f = fa.get(t)
        return (f["dn"] / f["n"]) if (f and f["n"]) else 0.0

    # Real results so far: points-per-game (3/1/0, a shootout counts as the 90-min
    # draw) and the set of teams that have ALREADY WON a shootout. Together with
    # form these are the only inputs to the penalty model (shoot_win).
    rec_pts, rec_n = {}, {}
    for (h, a, hg, ag) in played:
        for t, gf, ga in ((h, hg, ag), (a, ag, hg)):
            rec_n[t] = rec_n.get(t, 0) + 1
            rec_pts[t] = rec_pts.get(t, 0) + (3 if gf > ga else 1 if gf == ga else 0)

    def ppg(t):
        n = rec_n.get(t)
        return (rec_pts[t] / n) if n else 0.0

    pens_won = set()
    for k in (sim.get("knockoutPlayed") or []):
        if k.get("penalties") and k.get("winner") in ("HOME_TEAM", "AWAY_TEAM"):
            pens_won.add(k["home"] if k["winner"] == "HOME_TEAM" else k["away"])

    # Flat, deterministically-ordered list of the remaining GROUP fixtures, shared
    # by the runner (which tallies each game's outcome as it plays it) and the
    # upcoming-odds writer (which maps those tallies back to games). The order is
    # groupsDef order, then each group's remaining games — the exact order run()
    # plays them in, so a positional tally lines up index-for-index.
    groups_def = sim["groupsDef"]
    group_remaining = sim.get("groupRemaining") or {}
    flat_upcoming = [(letter, m["h"], m["a"])
                     for letter in groups_def
                     for m in (group_remaining.get(letter) or [])]

    return {
        "R": R, "p": p, "sim": sim, "sc": sc, "T": T,
        "owner_of": owner_of, "canon_of": canon_of,
        "fifa_of": fifa_of, "conduct_of": conduct_of,
        "rs": rs, "atk": atk, "deff": deff,
        "ppg": ppg, "pensWon": pens_won,
        "fa": fa, "avgGpg": avgGpg, "rs_bar": rs_bar,
        "flat_upcoming": flat_upcoming,
        "baseline": {row["name"]: row["points"] for row in data["leaderboard"]},
    }


# ----------------------------------------------------------------------------
# The scoring model (faithful port of eloScore / simGoals / eloShootWin)
# ----------------------------------------------------------------------------
SIM_ROUND_STAGE = {"r32": "LAST_32", "r16": "LAST_16", "qf": "QUARTER_FINALS",
                   "sf": "SEMI_FINALS", "final": "FINAL", "third": "THIRD_PLACE"}


def make_engine(model):
    R = model["R"]
    p = model["p"]
    rs = model["rs"]
    atk = model["atk"]
    deff = model["deff"]
    rnd = random.random
    exp = math.exp

    suprK, total0, floor = p["suprK"], p["total"], p["floor"]
    ATK, DEF = p["atkCoef"], p["defCoef"]
    rankCoef, rankCoefGroup = p["rankCoef"], p["rankCoefGroup"]
    cap, fatigue = p["formCap"], p["pensHangover"]
    kF, kR, kP = p["pensFormK"], p["pensResK"], p["pensWonK"]
    pcap = p["pensCap"]
    ppg = model["ppg"]
    base_pens = model["pensWon"]

    def sim_goals():
        r = rnd()
        return 0 if r < 0.20 else 1 if r < 0.48 else 2 if r < 0.73 else \
            3 if r < 0.89 else 4 if r < 0.97 else 5

    def poisson(lam):
        L = exp(-lam)
        k, prod = 0, 1.0
        while True:
            k += 1
            prod *= rnd()
            if prod <= L:
                return k - 1

    def elo_score(a, b, group=False, total_abs=None, total_mul=1.0,
                  hangA=False, hangB=False, rho=0.0,
                  atkA=None, defA=None, atkB=None, defB=None):
        if R is None or R.get(a) is None or R.get(b) is None:
            return sim_goals(), sim_goals()
        s = suprK * (R[a] - R[b])
        total = (total_abs if total_abs is not None else total0) * total_mul
        lamA = max(floor, total / 2 + s / 2)
        lamB = max(floor, total / 2 - s / 2)
        rc = rankCoefGroup if (group and rankCoefGroup is not None) else rankCoef
        aA = atk(a) if atkA is None else atkA
        dB = deff(b) if defB is None else defB
        aB = atk(b) if atkB is None else atkB
        dA = deff(a) if defA is None else defA
        eA = ATK * aA + DEF * dB
        eB = ATK * aB + DEF * dA
        eA = -cap if eA < -cap else (cap if eA > cap else eA)
        eB = -cap if eB < -cap else (cap if eB > cap else eB)
        lamA *= exp(eA + rc * (rs(a) - rs(b)))
        lamB *= exp(eB + rc * (rs(b) - rs(a)))
        if hangA:
            lamA *= exp(-fatigue)
        if hangB:
            lamB *= exp(-fatigue)
        ga, gb = poisson(lamA), poisson(lamB)
        if rho and ga <= 1 and gb <= 1:
            w = [1 - lamA * lamB * rho, lamB * (1 + lamA * rho),
                 lamA * (1 + lamB * rho), lamA * lamB * (1 - rho)]
            w = [x if x > 0 else 0 for x in w]
            r = rnd() * (w[0] + w[1] + w[2] + w[3])
            k = 0
            while k < 3:
                r -= w[k]
                if r >= 0:
                    k += 1
                else:
                    break
            ga, gb = ((0, 0), (0, 1), (1, 0), (1, 1))[k]
        return ga, gb

    def shoot_win(a, b, atkA=None, defA=None, atkB=None, defB=None,
                  pensA=None, pensB=None):
        """Penalty shootouts are close to a coin flip. Only tiny nudges apply:
        attacking/defending form (live values may be passed in), real
        points-per-game so far, and a boost for a side that has ALREADY WON a
        shootout this tournament (pensA/pensB; defaults to the real winners).
        Clamped to [1-pensCap, pensCap], 40-60% by default. Elo plays no part.
        Mirrored by eloShootWin in index.html — keep the two in sync."""
        aA = atk(a) if atkA is None else atkA
        dA = deff(a) if defA is None else defA
        aB = atk(b) if atkB is None else atkB
        dB = deff(b) if defB is None else defB
        pA = (a in base_pens) if pensA is None else pensA
        pB = (b in base_pens) if pensB is None else pensB
        d = (kF * ((aA - dA) - (aB - dB))
             + kR * (ppg(a) - ppg(b))
             + kP * ((1.0 if pA else 0.0) - (1.0 if pB else 0.0)))
        pr = 1.0 / (1.0 + exp(-d))
        lo = 1.0 - pcap
        return lo if pr < lo else (pcap if pr > pcap else pr)

    return sim_goals, elo_score, shoot_win


# ----------------------------------------------------------------------------
# Group ranking (faithful port of simRankGroup): head-to-head first.
# Each row: dict with team, points, gd, gf, conduct, fifa.
# ----------------------------------------------------------------------------
def rank_group(rows, results):
    def overall_key(r):
        return (-r["gd"], -r["gf"], -(r["conduct"] or 0),
                (999 if r["fifa"] is None else r["fifa"]), r["team"])

    def resolve(group):
        if len(group) == 1:
            return list(group)
        names = {r["team"] for r in group}
        h = {r["team"]: {"pts": 0, "gf": 0, "ga": 0} for r in group}
        for m in results:
            if m["h"] in names and m["a"] in names:
                h[m["h"]]["gf"] += m["hg"]; h[m["h"]]["ga"] += m["ag"]
                h[m["a"]]["gf"] += m["ag"]; h[m["a"]]["ga"] += m["hg"]
                if m["hg"] > m["ag"]:
                    h[m["h"]]["pts"] += 3
                elif m["hg"] < m["ag"]:
                    h[m["a"]]["pts"] += 3
                else:
                    h[m["h"]]["pts"] += 1; h[m["a"]]["pts"] += 1
        hk = lambda r: (-h[r["team"]]["pts"],
                        -(h[r["team"]]["gf"] - h[r["team"]]["ga"]),
                        -h[r["team"]]["gf"])
        ordered = sorted(group, key=hk)
        blocks = []
        for r in ordered:
            if blocks and hk(r) == hk(blocks[-1][0]):
                blocks[-1].append(r)
            else:
                blocks.append([r])
        if len(blocks) == 1:
            return sorted(group, key=overall_key)
        out = []
        for b in blocks:
            out += resolve(b)
        return out

    by_pts = sorted(rows, key=lambda r: -r["points"])
    ranked, i = [], 0
    while i < len(by_pts):
        j = i
        while j < len(by_pts) and by_pts[j]["points"] == by_pts[i]["points"]:
            j += 1
        ranked += resolve(by_pts[i:j])
        i = j
    return ranked


# ----------------------------------------------------------------------------
# One simulated tournament -> ({person: total points}, champion, {qualified teams})
# ----------------------------------------------------------------------------
def make_runner(model, form_feedback=False):
    sim = model["sim"]
    sc = model["sc"]
    p = model["p"]
    owner_of = model["owner_of"]
    fifa_of = model["fifa_of"]
    conduct_of = model["conduct_of"]
    baseline = model["baseline"]
    DC = p["dcRho"]
    groups_def = sim["groupsDef"]
    group_results = sim.get("groupResults") or {}
    group_remaining = sim.get("groupRemaining") or {}
    flat_upcoming = model["flat_upcoming"]
    # Shared [home-win, draw, away-win] tally per remaining group game, summed
    # across all of this worker's runs (no per-run allocation).
    upc = [[0, 0, 0] for _ in flat_upcoming]
    tmpl = sim["template"]
    knockout_played = sim.get("knockoutPlayed") or []
    third_alloc = tmpl.get("thirdAllocation") or {}
    third_slots = tmpl.get("thirdSlots") or []

    GW = sc.get("GROUP_STAGE_WIN", 0)
    GD = sc.get("GROUP_STAGE_DRAW", 0)

    sim_goals, elo_score, shoot_win = make_engine(model)

    # Bracket definitions, flattened and ordered by match number (once).
    defs = []
    for key, stage in SIM_ROUND_STAGE.items():
        for md in (tmpl.get(key) or []):
            defs.append((md, stage))
    defs.sort(key=lambda d: d[0]["match"])
    final_match = (tmpl.get("final") or [{}])[0].get("match")

    # If the template doesn't define the third-place play-off, synthesize it from
    # the two semi-final losers using the bracket engine's own loser-reference
    # codes ("L<semi match>"). Injected into the template so it is simulated and
    # emitted exactly like any other tie — points awarded (per the scoring table),
    # a projection written, and the /#knockout third-place card shown. Idempotent.
    if not tmpl.get("third"):
        _sf = list(tmpl.get("sf") or [])
        if len(_sf) == 2 and final_match:
            _third = {"match": final_match - 1,
                      "home": "L" + str(_sf[0]["match"]),
                      "away": "L" + str(_sf[1]["match"])}
            tmpl["third"] = [_third]
            defs.append((_third, "THIRD_PLACE"))
            defs.sort(key=lambda d: d[0]["match"])

    FINAL_BOOST = p["finalScoreBoost"]
    KO_MUL = p["koTotalMul"]
    ET_TOTAL = p["etTotal"]

    # Optional in-knockout form feedback (knockout page only): each KO result
    # updates both teams' opponent-weighted attack/defence form, so later rounds
    # see the in-form sides. Same bump the model applies to real games.
    atkf = model["atk"]; deff_f = model["deff"]
    base_fa = model["fa"]; avgGpg = model["avgGpg"]; rs_bar = model["rs_bar"]
    rsf = model["rs"]; qCoef = p["formOppCoef"]
    atkFloor = p["formAtkFloor"]; defFloor = p["formDefFloor"]
    koFormGain = p.get("koFormGain", 4.0)   # amplify in-tournament form so wins snowball
    fa_live = {}
    def _cur_atk(t):
        f = fa_live.get(t)
        return f["an"] / f["n"] if (f and f["n"]) else atkf(t)
    def _cur_def(t):
        f = fa_live.get(t)
        return f["dn"] / f["n"] if (f and f["n"]) else deff_f(t)
    def _bump_live(t, opp, gf, ga):
        f = fa_live.get(t)
        if f is None:
            bb = base_fa.get(t)
            f = {"an": bb["an"], "dn": bb["dn"], "n": bb["n"]} if bb else {"an": 0.0, "dn": 0.0, "n": 0}
            fa_live[t] = f
        w = math.exp(qCoef * (rsf(opp) - rs_bar)); wi = 1.0 / w
        dA = gf - avgGpg
        f["an"] += koFormGain * (w if dA >= 0 else (atkFloor if atkFloor > wi else wi)) * dA
        dD = ga - avgGpg
        f["dn"] += koFormGain * ((defFloor if defFloor > wi else wi) if dD > 0 else w) * dD
        f["n"] += 1

    def run(decide=None):
        if form_feedback:
            fa_live.clear()
        person = dict(baseline)

        # 1) Group tables: real results + simulated remaining fixtures.
        winners, runners, thirds = {}, {}, []
        gi = 0  # index into flat_upcoming, advanced per remaining group game played
        for letter in groups_def:
            names = groups_def[letter]
            rec = {t: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0}
                   for t in names}
            all_res = []

            def apply(hn, an, hg, ag):
                all_res.append({"h": hn, "a": an, "hg": hg, "ag": ag})
                for (t, gf, ga) in ((hn, hg, ag), (an, ag, hg)):
                    r = rec[t]
                    r["p"] += 1; r["gf"] += gf; r["ga"] += ga
                    if gf > ga:
                        r["w"] += 1; r["pts"] += 3
                    elif gf == ga:
                        r["d"] += 1; r["pts"] += 1
                    else:
                        r["l"] += 1

            for m in (group_results.get(letter) or []):
                apply(m["h"], m["a"], m["hg"], m["ag"])
            for m in (group_remaining.get(letter) or []):
                hg, ag = elo_score(m["h"], m["a"], group=True)
                apply(m["h"], m["a"], hg, ag)
                # award the simulated group game (points not yet in baseline) and
                # tally the outcome for the per-game upcoming odds (0=home, 1=draw,
                # 2=away), in lock-step with flat_upcoming via gi.
                if hg > ag:
                    upc[gi][0] += 1
                    o = owner_of.get(m["h"])
                    if o is not None:
                        person[o] = person.get(o, 0) + GW
                elif hg < ag:
                    upc[gi][2] += 1
                    o = owner_of.get(m["a"])
                    if o is not None:
                        person[o] = person.get(o, 0) + GW
                else:
                    upc[gi][1] += 1
                    for nm in (m["h"], m["a"]):
                        o = owner_of.get(nm)
                        if o is not None:
                            person[o] = person.get(o, 0) + GD
                gi += 1

            rows = [{"team": t, "points": rec[t]["pts"],
                     "gd": rec[t]["gf"] - rec[t]["ga"], "gf": rec[t]["gf"],
                     "conduct": conduct_of.get(t, 0), "fifa": fifa_of.get(t)}
                    for t in names]
            ranked = rank_group(rows, all_res)
            winners[letter] = ranked[0]
            runners[letter] = ranked[1]
            third = dict(ranked[2]); third["group"] = letter
            thirds.append(third)

        # 2) Best third-place race: top eight qualify.
        thirds.sort(key=lambda t: (-t["points"], -t["gd"], -t["gf"],
                                   -(t["conduct"] or 0),
                                   (999 if t["fifa"] is None else t["fifa"]),
                                   t["team"]))
        qual = thirds[:8]

        # The 32 teams through to the Round of 32: the 12 group winners, the 12
        # runners-up, and the 8 best thirds. (Captured here for the knockout-odds
        # tally; this set is fully determined by the group stage, independent of
        # the knockout simulation below.)
        qualified = set()
        for L in winners:
            qualified.add(winners[L]["team"])
            qualified.add(runners[L]["team"])
        for t in qual:
            qualified.add(t["team"])

        # 3) Allocate qualifying thirds to bracket slots (Annex-C table, else order).
        third_by_slot = {}
        combo = "".join(sorted(t["group"] for t in qual))
        assignment = third_alloc.get(combo)
        if assignment and third_slots:
            by_g = {t["group"]: t for t in qual}
            for i, slot in enumerate(third_slots):
                third_by_slot[slot] = by_g.get(assignment[i])
        else:
            for i, slot in enumerate(third_slots):
                third_by_slot[slot] = qual[i] if i < len(qual) else None

        # 4) Knockout bracket, simulated forward (real results kept where played).
        results = {}
        won_on_pens = {}                       # last-match pens win -> hangover
        ever_pens = set(model["pensWon"])      # any pens win so far -> pens boost
        real_ko = set()   # match numbers resolved from already-played real games

        def side_from_code(code, md):
            c0 = code[0]
            if c0 == "1" or c0 == "2":
                w = winners[code[1]] if c0 == "1" else runners[code[1]]
                return {"name": w["team"], "fifa": w["fifa"]} if w else None
            if code[:2] == "T:":
                w = third_by_slot.get(md["home"][1])
                return {"name": w["team"], "fifa": w["fifa"]} if w else None
            ref = int(code[1:])
            res = results.get(ref)
            if res:
                s = res["winner"] if c0 == "W" else res["loser"]
                return {"name": s["name"], "fifa": s["fifa"]}
            return None

        def find_real(hn, an, stage):
            for k in knockout_played:
                if k.get("stageCode") == stage and (
                        (k.get("home") == hn and k.get("away") == an) or
                        (k.get("home") == an and k.get("away") == hn)):
                    return k
            return None

        for md, stage in defs:
            home = side_from_code(md["home"], md)
            away = side_from_code(md["away"], md)
            if not (home and away):
                continue
            real = find_real(home["name"], away["name"], stage)
            if real and real.get("winner"):
                real_ko.add(md["match"])
                same = real.get("home") == home["name"]
                hg = real["homeGoals"] if same else real["awayGoals"]
                ag = real["awayGoals"] if same else real["homeGoals"]
                if real["winner"] == "DRAW":
                    win_side = None
                elif same:
                    win_side = real["winner"]
                else:
                    win_side = ("AWAY_TEAM" if real["winner"] == "HOME_TEAM"
                                else "HOME_TEAM")
                pens = real.get("penalties", False)
            elif decide is not None:
                hg, ag, win_side, pens = decide(home["name"], away["name"],
                                                stage, md["match"])
            else:
                fin_mul = FINAL_BOOST if stage == "FINAL" else 1.0
                hn = home["name"]; an_ = away["name"]
                hangA = bool(won_on_pens.get(hn))
                hangB = bool(won_on_pens.get(an_))
                fkw = (dict(atkA=_cur_atk(hn), defA=_cur_def(hn),
                            atkB=_cur_atk(an_), defB=_cur_def(an_))
                       if form_feedback else {})
                hg, ag = elo_score(hn, an_, hangA=hangA, hangB=hangB,
                                   total_mul=KO_MUL * fin_mul, rho=DC, **fkw)
                if hg == ag:
                    e1, e2 = elo_score(hn, an_, hangA=hangA, hangB=hangB,
                                       total_abs=ET_TOTAL, total_mul=fin_mul,
                                       rho=DC, **fkw)
                    hg += e1; ag += e2
                pens = False
                if hg == ag:
                    pens = True
                    home_wins = random.random() < shoot_win(
                        hn, an_, pensA=(hn in ever_pens), pensB=(an_ in ever_pens),
                        **fkw)
                    win_side = "HOME_TEAM" if home_wins else "AWAY_TEAM"
                else:
                    win_side = "HOME_TEAM" if hg > ag else "AWAY_TEAM"
                if form_feedback:
                    _bump_live(hn, an_, hg, ag)
                    _bump_live(an_, hn, ag, hg)
                # award the simulated knockout winner this round's points
                winner_name = hn if win_side == "HOME_TEAM" else an_
                pts = sc.get(stage, 0)
                if pts:
                    o = owner_of.get(winner_name)
                    if o is not None:
                        person[o] = person.get(o, 0) + pts

            if win_side == "HOME_TEAM":
                win, lose = home, away
            elif win_side == "AWAY_TEAM":
                win, lose = away, home
            else:
                win, lose = home, away  # (a real draw with no decisive winner)
            won_on_pens[win["name"]] = pens
            if pens:
                ever_pens.add(win["name"])
            results[md["match"]] = {
                "winner": {"name": win["name"], "fifa": win["fifa"]},
                "loser": {"name": lose["name"], "fifa": lose["fifa"]}}

        champ = None
        champ_opps = None
        if final_match is not None:
            res = results.get(final_match)
            if res:
                champ = res["winner"]["name"]
                # The champion's road to the title FROM HERE: the FIFA rank of each
                # side still AHEAD of it that it's projected to beat. Games already
                # played in reality are excluded (real_ko), so "Easiest Path to the
                # Title" reflects the remaining draw, not opponents already disposed
                # of. Empty once a team has no knockout games left to play.
                champ_opps = [results[m]["loser"]["fifa"] for m in results
                              if results[m]["winner"]["name"] == champ
                              and m not in real_ko]
        return person, champ, qualified, champ_opps

    return run, upc


# ----------------------------------------------------------------------------
# Worker: run a slice of the sims in its own process with its own seeded RNG.
# Rebuilds the SAME model from the same files, so every worker is identical
# except for its independent random stream. Returns plain dicts to sum up.
# ----------------------------------------------------------------------------
def _run_chunk(task):
    n, seed, data_path, elo_path = task
    random.seed(seed)
    model = build_model(load(elo_path), load(data_path))
    # Round-by-round with in-knockout form feedback (a result updates the winner's
    # form for the next round). This single pass now drives EVERYTHING — pool-win
    # odds, per-team title odds, R32-reach odds, the easiest-path bell and the
    # upcoming-game odds — so the whole site shares one coherent, more accurate
    # simulation instead of a second no-feedback pass.
    run, upc = make_runner(model, form_feedback=True)

    wins = defaultdict(float)
    pts_sum = defaultdict(float)
    champ = defaultdict(float)
    ko = defaultdict(float)        # per-team count of "made the Round of 32"
    ep_sum = defaultdict(float)    # champion -> sum over its titles of mean(-log opp rank)
    ep_rank = defaultdict(float)   # champion -> sum over its titles of mean(opp FIFA rank)
    for _ in range(n):
        person, champion, qualified, champ_opps = run()
        if champion is not None:
            champ[champion] += 1
            if champ_opps:
                k = len(champ_opps)
                ep_sum[champion]  += sum(-math.log(f or 40) for f in champ_opps) / k
                ep_rank[champion] += sum((f or 40) for f in champ_opps) / k
        for nm in qualified:
            ko[nm] += 1
        best = max(person.values())
        leaders = [nm for nm, v in person.items() if abs(v - best) < 1e-9]
        share = 1.0 / len(leaders)
        for nm in leaders:
            wins[nm] += share
        for nm, v in person.items():
            pts_sum[nm] += v
    return dict(wins), dict(pts_sum), dict(champ), dict(ko), upc, dict(ep_sum), dict(ep_rank)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Monte Carlo pool-winner odds.")
    ap.add_argument("--sims", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None,
                    help="parallel worker processes (default: all CPU cores). "
                         "Use 1 for the original single-process behaviour.")
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--elo", default="elo.json")
    ap.add_argument("--json", default=None,
                    help="also write a sim_odds.json (same shape as odds.json) "
                         "with per-person pool-win odds and per-team title odds.")
    ap.add_argument("--knockout-json", default=None,
                    help="also write a knockout_odds.json with each team's odds "
                         "of reaching the Round of 32 (top two, or one of the 8 "
                         "best thirds). Same base/delta trend structure as "
                         "sim_odds.json; carries only name + odds, so the page "
                         "joins group/owner/clinched/eliminated from data.json.")
    ap.add_argument("--upcoming-json", default=None,
                    help="also write an upcoming_odds.json with win/draw/loss odds "
                         "for every remaining GROUP game, tallied from the same "
                         "simulated group stage (so they agree exactly with the "
                         "pool/knockout odds). Same base/delta trend per outcome; "
                         "keyed on the home/away pair for the page to join.")
    args = ap.parse_args()

    data = load(args.data)
    elo = load(args.elo)
    # Built once in the parent for the warning, the people list, and the JSON
    # section below. Workers rebuild their own identical copy.
    model = build_model(elo, data)

    if model["R"] is None:
        print("WARNING: elo.json has no ratings; scorelines fall back to uniform "
              "random (the same graceful fallback the page uses).", file=sys.stderr)

    people = list(model["baseline"].keys())
    N = args.sims

    # How many processes, and how to split N so the slices sum to exactly N.
    workers = args.workers if args.workers else (os.cpu_count() or 1)
    workers = max(1, min(workers, N))
    base, rem = divmod(N, workers)
    counts = [base + (1 if i < rem else 0) for i in range(workers)]

    # Deterministic, well-separated per-worker seeds: reproducible when --seed
    # is given, independent across workers either way.
    seeder = random.Random(args.seed)
    tasks = [(counts[i], seeder.randrange(2 ** 31 - 1), args.data, args.elo)
             for i in range(workers)]

    print(f"Simulating {N:,} tournaments across {workers} worker(s) …",
          file=sys.stderr)

    wins = defaultdict(float)
    pts_sum = defaultdict(float)
    champ = defaultdict(float)
    ko = defaultdict(float)
    ep_sum_total = defaultdict(float)   # champion -> summed mean opponent strength over titles
    ep_rank_total = defaultdict(float)  # champion -> summed mean opponent FIFA rank over titles
    # Per-game [home-win, draw, away-win] counts, summed across workers by the
    # shared flat_upcoming index order.
    upc_total = [[0, 0, 0] for _ in model["flat_upcoming"]]

    def merge(result):
        w, p, c, k, u, es, er = result
        for nm, v in w.items():
            wins[nm] += v
        for nm, v in p.items():
            pts_sum[nm] += v
        for nm, v in c.items():
            champ[nm] += v
        for nm, v in k.items():
            ko[nm] += v
        for nm, v in es.items():
            ep_sum_total[nm] += v
        for nm, v in er.items():
            ep_rank_total[nm] += v
        for i, x in enumerate(u):
            t = upc_total[i]
            t[0] += x[0]; t[1] += x[1]; t[2] += x[2]

    done = 0
    if workers == 1:
        merge(_run_chunk(tasks[0]))
    else:
        with Pool(processes=workers) as pool:
            for i, result in enumerate(pool.imap_unordered(_run_chunk, tasks), 1):
                merge(result)
                done += 1
                print(f"  …worker {done}/{workers} finished", file=sys.stderr)

    rows = sorted(people, key=lambda nm: -wins[nm])
    width = max(len(nm) for nm in people)
    print(f"\nPool-winner odds over {N:,} simulated tournaments")
    print(f"(model + weights from {args.elo}, state from {args.data})\n")
    print(f"{'Name'.ljust(width)}   Win%     Avg pts   Wins")
    print(f"{'-' * width}   ------   -------   ------")
    for nm in rows:
        pct = 100.0 * wins[nm] / N
        avg = pts_sum[nm] / N
        print(f"{nm.ljust(width)}   {pct:5.1f}%   {avg:7.1f}   {wins[nm]:6.1f}")
    print()

    if args.json:
        # Persistent trend: each value keeps a `base` (its value as of the last
        # actual change); delta = odds - base. A no-op re-run leaves base alone,
        # so the arrow shows the last real movement and never collapses to "no
        # change". base only moves when the value moves.
        prev = None
        prev_p, prev_t = {}, {}
        try:
            with open(args.json, encoding="utf-8") as f:
                prev = json.load(f)
            for s in prev.get("standings", []):
                prev_p[s["name"]] = s
                for t in s.get("teams", []):
                    prev_t[t["name"]] = t
        except Exception:
            prev = None

        teams_of = {p["name"]: [t["name"] for t in p["teams"]]
                    for p in data["leaderboard"]}
        d5 = lambda x: round(x, 5)
        EPS = 5e-7

        def trend(new, prv):
            if not prv or prv.get("odds") is None:
                return new, None
            o = prv["odds"]
            b = prv.get("base")
            if b is None:
                d = prv.get("delta")
                b = (o - d) if d is not None else o
            base = o if abs(new - o) > EPS else b
            d = d5(new - base)
            return base, (d if d != 0 else None)

        standings = []
        for nm in people:
            odds = d5(wins[nm] / N)
            tlist = []
            for tn in teams_of.get(nm, []):
                todds = d5(champ.get(tn, 0.0) / N)
                tb, td = trend(todds, prev_t.get(tn))
                tlist.append({"name": tn, "odds": todds, "base": d5(tb), "delta": td})
            tlist.sort(key=lambda t: -t["odds"])
            pb, pd = trend(odds, prev_p.get(nm))
            standings.append({"name": nm, "odds": d5(odds), "base": d5(pb),
                              "delta": pd, "teams": tlist})
        standings.sort(key=lambda s: (-s["odds"], s["name"].lower()))
        rk, prev_o = 0, None
        for i, s in enumerate(standings):
            if prev_o is None or abs(s["odds"] - prev_o) > 1e-9:
                rk, prev_o = i + 1, s["odds"]
            s["rank"] = rk

        if prev is not None and prev.get("standings") == standings:
            print(f"No simulation changes since last run; {args.json} left untouched.")
        else:
            out = {"generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "sims": N, "standings": standings}
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            print(f"Wrote {args.json} ({N:,} sims).")

    if args.knockout_json:
        # Per-team odds of reaching the Round of 32. Same persistent-trend scheme
        # as the pool-winner file: `base` is the value as of the last real change,
        # `delta = odds - base`, and a no-op re-run leaves the file untouched (so
        # the `generated` stamp doesn't churn and the pipeline's no-change check
        # still fires). Output carries only name + odds + trend; the page joins
        # group / owner / clinched / eliminated / FIFA rank from data.json.
        prev = None
        prev_t = {}
        try:
            with open(args.knockout_json, encoding="utf-8") as f:
                prev = json.load(f)
            for t in prev.get("teams", []):
                prev_t[t["name"]] = t
        except Exception:
            prev = None

        d5 = lambda x: round(x, 5)
        EPS = 5e-7

        def ktrend(new, prv):
            if not prv or prv.get("odds") is None:
                return new, None
            o = prv["odds"]
            b = prv.get("base")
            if b is None:
                d = prv.get("delta")
                b = (o - d) if d is not None else o
            base = o if abs(new - o) > EPS else b
            d = d5(new - base)
            return base, (d if d != 0 else None)

        # Every team in the tournament gets a row (so eliminated sides show 0.0),
        # keyed on the canonical names the sim block already uses.
        teams = []
        for nm in model["T"]:
            odds = d5(ko.get(nm, 0.0) / N)
            kb, kd = ktrend(odds, prev_t.get(nm))
            teams.append({"name": nm, "odds": odds, "base": d5(kb), "delta": kd})
        teams.sort(key=lambda t: (-t["odds"], t["name"].lower()))

        # Easiest-Path-to-the-Title: for every team that won >= 1 simulated title,
        # the mean strength (-log FIFA rank) of the knockout sides STILL AHEAD of it
        # that it's projected to beat (games already played are excluded), averaged
        # over its title-winning sims, with a readable mean opponent rank and the raw
        # title tally. From the same single form-feedback pass as the odds above, so
        # the champion and the bell agree exactly. The #knockout page plots these on
        # a bell curve and lists them. Deterministic under a fixed --seed, so the
        # no-op skip still holds when nothing upstream changed.
        ep_rows = []
        for nm in model["T"]:
            c = champ.get(nm, 0.0)
            if c <= 0:
                continue
            ep_rows.append({
                "name": nm,
                "wins": int(round(c)),
                "winPct": d5(c / N),
                "avgStrength": round(ep_sum_total.get(nm, 0.0) / c, 5),
                "avgOppRank": round(ep_rank_total.get(nm, 0.0) / c, 2),
            })
        ep_rows.sort(key=lambda r: (-r["wins"], r["name"].lower()))

        # Most-Likely Bracket — coherent, ROUND BY ROUND. R32 are the real fixed
        # matchups. Each game is decided by a fast head-to-head with the SAME model
        # weights; the winner advances and the next round's matchup is formed from
        # those winners (a result genuinely feeds the next round, so the bracket
        # chains cleanly). We keep the home win probability (away = 1 - it) and the
        # most likely score with ET/pens noted. Deterministic under a fixed --seed.
        pp = model["p"]
        KO_MUL = pp["koTotalMul"]; ET_TOTAL = pp["etTotal"]
        FINAL_BOOST = pp["finalScoreBoost"]; DC = pp["dcRho"]
        _, elo_b, shoot_b = make_engine(model)

        # Same in-round form feedback as the easiest-path pass, but along the single
        # coherent bracket chain: each decided result updates both teams' form, so
        # the next round's head-to-head sees the in-form sides.
        base_fa = model["fa"]; avgGpg = model["avgGpg"]; rs_bar = model["rs_bar"]
        rsf = model["rs"]; qCoef = pp["formOppCoef"]
        atkFloor = pp["formAtkFloor"]; defFloor = pp["formDefFloor"]
        koFormGain = pp.get("koFormGain", 3.0)
        b_atk0 = model["atk"]; b_def0 = model["deff"]
        fa_b = {}
        def cur_a(t):
            f = fa_b.get(t)
            return f["an"] / f["n"] if (f and f["n"]) else b_atk0(t)
        def cur_d(t):
            f = fa_b.get(t)
            return f["dn"] / f["n"] if (f and f["n"]) else b_def0(t)
        def bump_b(t, opp, gf, ga):
            f = fa_b.get(t)
            if f is None:
                bb = base_fa.get(t)
                f = {"an": bb["an"], "dn": bb["dn"], "n": bb["n"]} if bb else {"an": 0.0, "dn": 0.0, "n": 0}
                fa_b[t] = f
            w = math.exp(qCoef * (rsf(opp) - rs_bar)); wi = 1.0 / w
            dA = gf - avgGpg
            f["an"] += koFormGain * (w if dA >= 0 else (atkFloor if atkFloor > wi else wi)) * dA
            dD = ga - avgGpg
            f["dn"] += koFormGain * ((defFloor if defFloor > wi else wi) if dD > 0 else w) * dD
            f["n"] += 1

        # Modal-bracket pens history: real shootout winners, plus each round's
        # modal winner when the modal result was itself a shootout.
        ml_pens = set(model["pensWon"])

        def play1(h, a, is_final, fv, pH, pA):
            fmul = FINAL_BOOST if is_final else 1.0
            hg, ag = elo_b(h, a, total_mul=KO_MUL * fmul, rho=DC,
                           atkA=fv[0], defA=fv[1], atkB=fv[2], defB=fv[3])
            if hg != ag:
                return hg, ag, 0, (0 if hg > ag else 1)
            e1, e2 = elo_b(h, a, total_abs=ET_TOTAL, total_mul=fmul, rho=DC,
                           atkA=fv[0], defA=fv[1], atkB=fv[2], defB=fv[3])
            hg += e1; ag += e2
            if hg != ag:
                return hg, ag, 1, (0 if hg > ag else 1)
            hw = random.random() < shoot_b(h, a, atkA=fv[0], defA=fv[1],
                                           atkB=fv[2], defB=fv[3],
                                           pensA=pH, pensB=pA)
            return hg, ag, 2, (0 if hw else 1)

        K = 40000
        bdata = {}

        def decide(h, a, stage, match):
            is_final = (stage == "FINAL")
            fv = (cur_a(h), cur_d(h), cur_a(a), cur_d(a))
            pH, pA = (h in ml_pens), (a in ml_pens)
            wh = 0; rtc = [0, 0, 0]
            hist = defaultdict(int)
            for _ in range(K):
                hg, ag, rt, ws = play1(h, a, is_final, fv, pH, pA)
                if ws == 0:
                    wh += 1
                rtc[rt] += 1
                hist[(hg, ag, rt, ws)] += 1
            ph = wh / K
            pick_home = ph >= 0.5
            # Result type = the most common of reg / extra time / pens, so close ties
            # show a shootout. Score = the most likely scoreline OF THAT TYPE with the
            # favourite winning (pens: the modal level score). Keeps realistic variety
            # — 1-0, 2-0, 2-1, 1-1 (pens) — driven by the matchup and live form.
            ti = rtc.index(max(rtc))
            best, bc = None, -1
            for (hg, ag, rt, ws), cc in hist.items():
                if rt != ti:
                    continue
                if ti != 2 and (ws == 0) != pick_home:
                    continue
                if cc > bc:
                    bc, best = cc, (hg, ag)
            if best is None:
                for (hg, ag, rt, ws), cc in hist.items():
                    if (ws == 0) != pick_home:
                        continue
                    if cc > bc:
                        bc, best = cc, (hg, ag)
            gh, ga = best if best else (1, 0)
            bump_b(h, a, gh, ga); bump_b(a, h, ga, gh)
            if ti == 2:
                ml_pens.add(h if pick_home else a)
            bdata[match] = {
                "home": h, "away": a, "pHome": d5(ph),
                "score": {"h": gh, "a": ga,
                          "type": ("reg" if ti == 0 else "et" if ti == 1 else "pens")},
            }
            return gh, ga, ("HOME_TEAM" if pick_home else "AWAY_TEAM"), (ti == 2)

        random.seed((args.seed if args.seed is not None else 1) + 777)
        chalk_run, _cu = make_runner(model)
        chalk_run(decide=decide)

        tmpl = model["sim"]["template"]
        bracket_rows = []
        for key in ("r32", "r16", "qf", "sf", "final", "third"):
            stage = SIM_ROUND_STAGE[key]
            for md in (tmpl.get(key) or []):
                b = bdata.get(md["match"])
                if not b:
                    continue
                bracket_rows.append({"match": md["match"], "stage": stage,
                                     "home": b["home"], "away": b["away"],
                                     "pHome": b["pHome"], "score": b["score"]})
        bracket_rows.sort(key=lambda r: r["match"])
        champion = None
        fin = next((r for r in bracket_rows if r["stage"] == "FINAL"), None)
        if fin:
            cn = fin["home"] if fin["pHome"] >= 0.5 else fin["away"]
            champion = {"name": cn, "titlePct": d5(champ.get(cn, 0.0) / N)}


        if (prev is not None and prev.get("teams") == teams and prev.get("easiestPath") == ep_rows
                and prev.get("bracket") == bracket_rows and prev.get("champion") == champion):
            print(f"No knockout-odds changes since last run; {args.knockout_json} left untouched.")
        else:
            out = {"generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "sims": N, "teams": teams, "easiestPath": ep_rows,
                   "bracket": bracket_rows, "champion": champion}
            with open(args.knockout_json, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            print(f"Wrote {args.knockout_json} ({N:,} sims).")

    if args.upcoming_json:
        # Per-game win/draw/loss odds for every remaining GROUP game, read off the
        # same simulated group stage as the pool/knockout odds (so the three files
        # agree exactly). Each of the three outcomes carries its own persistent
        # trend: `base` is its value as of the last real change, `delta = odds -
        # base`, and a no-op re-run leaves the file untouched. Keyed on the
        # home/away pair (unique in a group round-robin); the page joins to its
        # upcoming cards by that pair.
        prev = None
        prev_g = {}
        try:
            with open(args.upcoming_json, encoding="utf-8") as f:
                prev = json.load(f)
            for g in prev.get("games", []):
                prev_g[(g["home"], g["away"])] = g
        except Exception:
            prev = None

        d5 = lambda x: round(x, 5)
        EPS = 5e-7

        def utrend(new, po, pb, pd):
            # po/pb/pd = previous odds / base / delta for this one outcome.
            if po is None:
                return new, None
            b = pb if pb is not None else ((po - pd) if pd is not None else po)
            base = po if abs(new - po) > EPS else b
            d = d5(new - base)
            return base, (d if d != 0 else None)

        games = []
        for i, (letter, h, a) in enumerate(model["flat_upcoming"]):
            c = upc_total[i]
            tot = c[0] + c[1] + c[2]
            if tot == 0:
                continue
            ph, pdr, pa = d5(c[0] / tot), d5(c[1] / tot), d5(c[2] / tot)
            pg = prev_g.get((h, a)) or {}
            po, pb, pdl = pg.get("odds") or {}, pg.get("base") or {}, pg.get("delta") or {}
            bh, dh = utrend(ph, po.get("home"), pb.get("home"), pdl.get("home"))
            bd, dd = utrend(pdr, po.get("draw"), pb.get("draw"), pdl.get("draw"))
            ba, da = utrend(pa, po.get("away"), pb.get("away"), pdl.get("away"))
            games.append({
                "group": letter, "home": h, "away": a,
                "odds": {"home": ph, "draw": pdr, "away": pa},
                "base": {"home": d5(bh), "draw": d5(bd), "away": d5(ba)},
                "delta": {"home": dh, "draw": dd, "away": da},
            })
        games.sort(key=lambda g: (g["group"], g["home"].lower(), g["away"].lower()))

        # Per-game win odds for upcoming KNOCKOUT fixtures. Knockouts can't draw, so
        # instead of home/draw/away each team gets a regulation / extra-time / penalty
        # win share (summing to 1 across both teams). Computed as a direct head-to-head
        # Monte-Carlo using the same KO match model as the bracket sim, and keyed (like
        # the group games) on the home/away pair so the page joins them to its cards.
        # Knockout fixtures come from the live feed, which spells some nations
        # differently from the canonical sim names ("Congo DR" vs "DR Congo",
        # "Ivory Coast" vs "Côte d'Ivoire", "Bosnia-Herzegovina" vs "Bosnia and
        # Herzegovina"). Map them to the model's names (same aliases the page uses)
        # so the matchup is rated and the row joins to the page's cards. Group games
        # avoid this because they're built straight from the canonical sim names.
        import unicodedata
        KO_ALIAS = {"Congo DR": "DR Congo", "Bosnia-Herzegovina": "Bosnia and Herzegovina",
                    "Ivory Coast": "Côte d'Ivoire", "Turkey": "Türkiye",
                    "Cape Verde Islands": "Cabo Verde"}
        _team_names = set(model["fifa_of"])

        def _nk(s):
            s = unicodedata.normalize("NFKD", s or "")
            s = "".join(c for c in s if not unicodedata.combining(c))
            for ch in "\u2018\u2019\u02bc`\u00b4":
                s = s.replace(ch, "'")
            return " ".join(s.lower().split())
        _norm_map = {_nk(n): n for n in _team_names}
        _alias_norm = {_nk(k): v for k, v in KO_ALIAS.items()}

        def canon_feed(name):
            if name in _team_names:
                return name
            a = KO_ALIAS.get(name) or _alias_norm.get(_nk(name))
            if a and a in _team_names:
                return a
            m = _norm_map.get(_nk(name))
            if m:
                return m
            if a:
                m = _norm_map.get(_nk(a))
                if m:
                    return m
            return name  # unknown to the model; ko_split will skip if Elo is present

        ko_fixtures, seen_ko = [], set()
        for mm in (data.get("upcoming") or []):
            code = (mm.get("stageCode") or "").upper()
            if not code or code == "GROUP_STAGE":
                continue
            hn = canon_feed((mm.get("home") or {}).get("name"))
            an = canon_feed((mm.get("away") or {}).get("name"))
            if not hn or not an or (hn, an) in seen_ko:
                continue
            seen_ko.add((hn, an))
            ko_fixtures.append((code, hn, an))

        # The feed's `upcoming` list only carries the next handful of fixtures,
        # so a knockout game can have both of its teams decided yet be missing
        # from it. The page now merges those straight from the bracket into its
        # Coming Up section; mirror that rule here so every one of them also
        # gets its odds bar: any bracket match that hasn't kicked off and whose
        # two sides are both resolved (and not what-if projections) is rated.
        for rnd in (data.get("bracket") or []):
            code = (rnd.get("stageCode") or "").upper()
            if not code or code == "GROUP_STAGE":
                continue
            for mm in (rnd.get("matches") or []):
                st = (mm.get("status") or "").upper()
                if st in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED"):
                    continue
                hs, aw = mm.get("home") or {}, mm.get("away") or {}
                if not hs.get("resolved") or not aw.get("resolved"):
                    continue
                if hs.get("projected") or aw.get("projected"):
                    continue
                hn = canon_feed(hs.get("name"))
                an = canon_feed(aw.get("name"))
                if not hn or not an or (hn, an) in seen_ko:
                    continue
                seen_ko.add((hn, an))
                ko_fixtures.append((code, hn, an))

        ko_games = []
        if ko_fixtures:
            _sg, ko_elo, ko_shoot = make_engine(model)
            R = model["R"]
            P = model["p"]
            DC, KO_MUL, ET_TOTAL = P["dcRho"], P["koTotalMul"], P["etTotal"]
            KO_SIMS = min(N, 200000) or 200000
            # Deterministic and independent of the group draws: reseed the global RNG
            # (the one ko_elo/ko_shoot draw from) so a no-op re-run is byte-stable.
            random.seed((args.seed if args.seed is not None else 0) ^ 0x4B4F)

            def ko_split(h, a):
                if R is not None and (h not in R or a not in R):
                    return None
                # counts: home reg/ET/pen, away reg/ET/pen
                hr = he = hp = ar = ae = ap = 0
                for _ in range(KO_SIMS):
                    hg, ag = ko_elo(h, a, total_mul=KO_MUL, rho=DC)
                    if hg != ag:                       # decided in regulation
                        if hg > ag: hr += 1
                        else:       ar += 1
                        continue
                    e1, e2 = ko_elo(h, a, total_abs=ET_TOTAL, rho=DC)
                    if e1 != e2:                       # decided in extra time
                        if e1 > e2: he += 1
                        else:       ae += 1
                        continue
                    if random.random() < ko_shoot(h, a):   # decided on penalties
                        hp += 1
                    else:
                        ap += 1
                n = float(KO_SIMS)
                return {"homeReg": hr / n, "homeET": he / n, "homePen": hp / n,
                        "awayReg": ar / n, "awayET": ae / n, "awayPen": ap / n}

            KEYS = ("homeReg", "homeET", "homePen", "awayReg", "awayET", "awayPen")
            for code, hn, an in ko_fixtures:
                raw = ko_split(hn, an)
                if raw is None:
                    continue
                odds = {k: d5(raw[k]) for k in KEYS}
                pg = prev_g.get((hn, an)) or {}
                po, pb, pdl = pg.get("odds") or {}, pg.get("base") or {}, pg.get("delta") or {}
                base, delta = {}, {}
                for k in KEYS:
                    b, dlt = utrend(odds[k], po.get(k), pb.get(k), pdl.get(k))
                    base[k], delta[k] = d5(b), dlt
                ko_games.append({
                    "stageCode": code, "home": hn, "away": an, "knockout": True,
                    "odds": odds, "base": base, "delta": delta,
                })
            ko_games.sort(key=lambda g: (g["stageCode"], g["home"].lower(), g["away"].lower()))
        games += ko_games

        if prev is not None and prev.get("games") == games:
            print(f"No upcoming-odds changes since last run; {args.upcoming_json} left untouched.")
        else:
            out = {"generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "sims": N, "games": games}
            with open(args.upcoming_json, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            print(f"Wrote {args.upcoming_json} ({N:,} sims, {len(games)} games).")


if __name__ == "__main__":
    main()
