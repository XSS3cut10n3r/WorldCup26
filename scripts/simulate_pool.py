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

    python3 simulate_pool.py                 # 100,000 runs
    python3 simulate_pool.py --sims 20000
    python3 simulate_pool.py --seed 1 --sims 5000

This is the aggregate of the site's "Simulate" button: at the start of the
tournament it is a clean from-scratch projection off the Elos and weights; once
games are played it conditions on the real results, exactly like the page.
"""

import argparse
import json
import math
import random
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone


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
        "pensK": P.get("pensK", 0.9),
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
        "koTotalMul": P.get("koTotalMul", 0.85),
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

    return {
        "R": R, "p": p, "sim": sim, "sc": sc, "T": T,
        "owner_of": owner_of, "canon_of": canon_of,
        "fifa_of": fifa_of, "conduct_of": conduct_of,
        "rs": rs, "atk": atk, "deff": deff,
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
    cap, fatigue, pensK = p["formCap"], p["pensHangover"], p["pensK"]

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
                  hangA=False, hangB=False, rho=0.0):
        if R is None or R.get(a) is None or R.get(b) is None:
            return sim_goals(), sim_goals()
        s = suprK * (R[a] - R[b])
        total = (total_abs if total_abs is not None else total0) * total_mul
        lamA = max(floor, total / 2 + s / 2)
        lamB = max(floor, total / 2 - s / 2)
        rc = rankCoefGroup if (group and rankCoefGroup is not None) else rankCoef
        eA = ATK * atk(a) + DEF * deff(b)
        eB = ATK * atk(b) + DEF * deff(a)
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

    def shoot_win(a, b):
        if R is None or R.get(a) is None or R.get(b) is None:
            return 0.5
        return 1.0 / (1.0 + exp(-pensK * (R[a] - R[b])))

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
# One simulated tournament -> {person: total points}
# ----------------------------------------------------------------------------
def make_runner(model):
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

    FINAL_BOOST = p["finalScoreBoost"]
    KO_MUL = p["koTotalMul"]
    ET_TOTAL = p["etTotal"]

    def run():
        person = dict(baseline)

        # 1) Group tables: real results + simulated remaining fixtures.
        winners, runners, thirds = {}, {}, []
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
                # award the simulated group game (points not yet in baseline)
                if hg > ag:
                    o = owner_of.get(m["h"])
                    if o is not None:
                        person[o] = person.get(o, 0) + GW
                elif hg < ag:
                    o = owner_of.get(m["a"])
                    if o is not None:
                        person[o] = person.get(o, 0) + GW
                else:
                    for nm in (m["h"], m["a"]):
                        o = owner_of.get(nm)
                        if o is not None:
                            person[o] = person.get(o, 0) + GD

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
        won_on_pens = {}

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
            else:
                fin_mul = FINAL_BOOST if stage == "FINAL" else 1.0
                hangA = bool(won_on_pens.get(home["name"]))
                hangB = bool(won_on_pens.get(away["name"]))
                hg, ag = elo_score(home["name"], away["name"], hangA=hangA,
                                   hangB=hangB, total_mul=KO_MUL * fin_mul, rho=DC)
                if hg == ag:
                    e1, e2 = elo_score(home["name"], away["name"], hangA=hangA,
                                       hangB=hangB, total_abs=ET_TOTAL,
                                       total_mul=fin_mul, rho=DC)
                    hg += e1; ag += e2
                pens = False
                if hg == ag:
                    pens = True
                    home_wins = random.random() < shoot_win(home["name"], away["name"])
                    win_side = "HOME_TEAM" if home_wins else "AWAY_TEAM"
                else:
                    win_side = "HOME_TEAM" if hg > ag else "AWAY_TEAM"
                # award the simulated knockout winner this round's points
                winner_name = home["name"] if win_side == "HOME_TEAM" else away["name"]
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
            results[md["match"]] = {
                "winner": {"name": win["name"], "fifa": win["fifa"]},
                "loser": {"name": lose["name"], "fifa": lose["fifa"]}}

        champ = None
        if final_match is not None:
            res = results.get(final_match)
            if res:
                champ = res["winner"]["name"]
        return person, champ

    return run


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Monte Carlo pool-winner odds.")
    ap.add_argument("--sims", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--elo", default="elo.json")
    ap.add_argument("--json", default=None,
                    help="also write a sim_odds.json (same shape as odds.json) "
                         "with per-person pool-win odds and per-team title odds.")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    data = load(args.data)
    elo = load(args.elo)
    model = build_model(elo, data)
    run = make_runner(model)

    if model["R"] is None:
        print("WARNING: elo.json has no ratings; scorelines fall back to uniform "
              "random (the same graceful fallback the page uses).", file=sys.stderr)

    people = list(model["baseline"].keys())
    wins = defaultdict(float)
    pts_sum = defaultdict(float)
    champ = defaultdict(float)
    N = args.sims
    step = max(1, N // 10)

    for i in range(N):
        person, champion = run()
        if champion is not None:
            champ[champion] += 1
        best = max(person.values())
        leaders = [nm for nm, v in person.items() if abs(v - best) < 1e-9]
        share = 1.0 / len(leaders)
        for nm in leaders:
            wins[nm] += share
        for nm, v in person.items():
            pts_sum[nm] += v
        if (i + 1) % step == 0:
            print(f"  ...{i + 1:,}/{N:,}", file=sys.stderr)

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
        # Deltas vs the previously published run, if present.
        prev_person, prev_team = {}, {}
        try:
            with open(args.json, encoding="utf-8") as f:
                prev = json.load(f)
            for s in prev.get("standings", []):
                prev_person[s["name"]] = s.get("odds")
                for t in s.get("teams", []):
                    prev_team[t["name"]] = t.get("odds")
        except Exception:
            pass

        teams_of = {p["name"]: [t["name"] for t in p["teams"]]
                    for p in data["leaderboard"]}
        d5 = lambda x: round(x, 5)

        def delta(now, prev):
            return None if prev is None else d5(now - prev)

        standings = []
        for nm in people:
            odds = wins[nm] / N
            tlist = []
            for tn in teams_of.get(nm, []):
                todds = champ.get(tn, 0.0) / N
                tlist.append({"name": tn, "odds": d5(todds),
                              "delta": delta(todds, prev_team.get(tn))})
            tlist.sort(key=lambda t: -t["odds"])
            standings.append({"name": nm, "odds": d5(odds),
                              "delta": delta(odds, prev_person.get(nm)),
                              "teams": tlist})
        standings.sort(key=lambda s: (-s["odds"], s["name"].lower()))
        rk, prev_o = 0, None
        for i, s in enumerate(standings):
            if prev_o is None or abs(s["odds"] - prev_o) > 1e-9:
                rk, prev_o = i + 1, s["odds"]
            s["rank"] = rk

        out = {"generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "sims": N, "standings": standings}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"Wrote {args.json} ({N:,} sims).")


if __name__ == "__main__":
    main()
