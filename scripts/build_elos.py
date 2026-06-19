#!/usr/bin/env python3
"""
build_elos.py — fit per-team Elo-style ratings so that, when the *whole* World
Cup is simulated forward thousands of times, each team lifts the trophy at
roughly its current market (Kalshi) rate. Writes elo.json.

    python3 build_elos.py                       # reads data.json + odds.json
    python3 build_elos.py data.json odds.json   # explicit paths
    BUILD_FINAL_SIMS=100000 python3 build_elos.py   # heavier final pass

How it works
------------
1.  Each team starts with a rating derived from its FIFA rank (the prior).
2.  We Monte-Carlo the remaining tournament — the *same* groups + Annex-C
    bracket the website simulates (read straight from data.json's `sim` block,
    so the builder and the site always agree on the format) — and read off how
    often each team wins it all.
3.  We nudge the priced teams' ratings up/down and repeat until the simulated
    champions match the market. Teams the market doesn't price keep their pure
    rank rating and just absorb the leftover probability.

The ratings produced here are the *base intrinsic strength*. The live GF/GA/
FIFA-rank tilts that make a hot or well-drilled team beat its market line are
applied in the browser (buildSimData) on top of these — they are deliberately
NOT in the calibration, which is why those tilts cause the small, intended
divergence from Kalshi. The tilt coefficients ride along in elo.json["params"]
so the front end and this script share one source of tuning.

Pure stdlib. For a one-off local build this is fast enough (~1-3 min for a
100k final pass); swap the Poisson draw for numpy if you want it quicker.
"""

import json
import math
import os
import sys
import random

# ----- tunable model parameters (these get written into elo.json) -----------
PARAMS = {
    "suprK":   0.42,   # rating gap -> goal supremacy (bigger = more decisive)
    "total":   2.65,   # baseline total goals in a match
    "floor":   0.18,   # minimum expected goals for a side
    "pensK":   0.90,   # rating gap -> shootout win probability steepness
    # live tilt coefficients (applied in the browser, not in calibration):
    "atkCoef":  0.05,  # +xG for a side scoring above the field average (GF)
    "defCoef":  0.05,  # opponent's xG shifts with how leaky a side is (GA)
    "formOppCoef": 0.4,  # how hard GF/GA form is weighted by opponent quality
                         #   (0 = ignore opponent; higher = beating strong teams counts much more)
    "formCap": 0.4,      # ceiling on the combined GF/GA form tilt (exp(0.4) ~ +/-49% xG max)
    "formAtkFloor": 1.0, # failing to score vs a giant is only partly forgiven (1 = normal
                         #   penalty, no giant bonus; lower forgives more, higher punishes more)
    "formDefFloor": 0.0, # leaking to a giant is fully forgiven (0); raise to forgive less
    "rankCoef": 0.04,  # small extra nudge from the FIFA-rank difference (knockouts)
    "rankCoefGroup": 0.15,  # FIFA rank counts for much more in the group stage
    "pensHangover": 0.06,   # xG haircut next match for a team that just won on penalties
    "finalScoreBoost": 1.20,  # baseline goals are 20% higher in the final
    "koTotalMul": 0.85,  # knockout regulation scoring vs the group stage. 0.85 keeps it
                         #   realistic (~2.3 goals/game, a touch below the tournament average).
                         #   On its own this gives only ~4 shootouts because independent Poisson
                         #   under-draws; the dcRho correction below makes up the difference.
    "etTotal": 0.75,  # expected goals in extra time (~1/3 of a ~2.3-goal regulation); higher
                      #   -> more games settled in ET -> fewer shootouts.
    "dcRho": -0.30,   # Dixon-Coles low-score draw correction. Inflates 0-0 and 1-1 (real
                      #   football draws more than two independent Poissons) WITHOUT changing
                      #   total goals or who is favoured. ~ -0.13 is textbook; -0.30 lifts the
                      #   knockout shootout rate to the historical ~19%/match (~6/tournament).
                      #   Front-end only, like the other tilts; the calibrator ignores it.
    "nudgeK":   0.55,  # update_odds.py: rating move per unit log-odds drift
}

# ----- calibration controls --------------------------------------------------
CALIB_SIMS  = int(os.environ.get("BUILD_CALIB_SIMS", "5000"))   # per iteration
CALIB_ITERS = int(os.environ.get("BUILD_CALIB_ITERS", "40"))
FINAL_SIMS  = int(os.environ.get("BUILD_FINAL_SIMS", "100000"))
STEP        = float(os.environ.get("BUILD_STEP", "0.22"))      # nudge size (log-space)
STEP_CAP    = 0.30                                             # max |Δrating| per iter
EPS         = 1e-6


# ===== goal / match model ====================================================
def _pois(lam, rng):
    """Knuth Poisson sampler (lam is small here, so this is cheap)."""
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def _lambdas(ra, rb, P):
    s = P["suprK"] * (ra - rb)
    return (max(P["floor"], P["total"] / 2 + s / 2),
            max(P["floor"], P["total"] / 2 - s / 2))


def _play_goals(a, b, R, P, rng):
    la, lb = _lambdas(R[a], R[b], P)
    return _pois(la, rng), _pois(lb, rng)


def _play_winner(a, b, R, P, rng):
    la, lb = _lambdas(R[a], R[b], P)
    ga, gb = _pois(la, rng), _pois(lb, rng)
    if ga > gb:
        return a, b
    if gb > ga:
        return b, a
    p = 1.0 / (1.0 + math.exp(-P["pensK"] * (R[a] - R[b])))
    return (a, b) if rng.random() < p else (b, a)


# ===== 2026 group ranking (recursive head-to-head, then overall) =============
def rank_group(rows, results):
    def overall_key(r):
        return (-r["gd"], -r["gf"], -(r["conduct"] or 0),
                (999 if r["fifa"] is None else r["fifa"]), r["team"])

    def resolve(group):
        if len(group) == 1:
            return list(group)
        names = {r["team"] for r in group}
        h = {r["team"]: [0, 0, 0] for r in group}   # pts, gf, ga
        for m in results:
            if m["h"] in names and m["a"] in names:
                h[m["h"]][1] += m["hg"]; h[m["h"]][2] += m["ag"]
                h[m["a"]][1] += m["ag"]; h[m["a"]][2] += m["hg"]
                if m["hg"] > m["ag"]:
                    h[m["h"]][0] += 3
                elif m["hg"] < m["ag"]:
                    h[m["a"]][0] += 3
                else:
                    h[m["h"]][0] += 1; h[m["a"]][0] += 1

        def hk(r):
            t = r["team"]
            return (-h[t][0], -(h[t][1] - h[t][2]), -h[t][1])

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

    by = sorted(rows, key=lambda r: -r["points"])
    ranked, i = [], 0
    while i < len(by):
        j = i
        while j < len(by) and by[j]["points"] == by[i]["points"]:
            j += 1
        ranked += resolve(by[i:j])
        i = j
    return ranked


# ===== one full simulated tournament -> champion =============================
def _side_from_code(code, md, winners, runners, third_by_slot, results):
    c0 = code[0]
    if c0 in ("1", "2"):
        return (winners if c0 == "1" else runners).get(code[1])
    if code[:2] == "T:":
        return third_by_slot.get(md["home"][1])
    res = results.get(int(code[1:]))
    if not res:
        return None
    return res["winner"] if c0 == "W" else res["loser"]


def simulate_once(ctx, R, P, rng):
    meta = ctx["meta"]
    winners, runners, thirds = {}, {}, []
    for L in ctx["letters"]:
        names = ctx["groupsDef"][L]
        rec = {t: [0, 0, 0] for t in names}          # pts, gf, ga
        allres = list(ctx["results"][L])
        for (h, a) in ctx["remaining"][L]:
            hg, ag = _play_goals(h, a, R, P, rng)
            allres.append({"h": h, "a": a, "hg": hg, "ag": ag})
        for m in allres:
            rec[m["h"]][1] += m["hg"]; rec[m["h"]][2] += m["ag"]
            rec[m["a"]][1] += m["ag"]; rec[m["a"]][2] += m["hg"]
            if m["hg"] > m["ag"]:
                rec[m["h"]][0] += 3
            elif m["hg"] < m["ag"]:
                rec[m["a"]][0] += 3
            else:
                rec[m["h"]][0] += 1; rec[m["a"]][0] += 1
        rows = [{"team": t, "points": rec[t][0], "gd": rec[t][1] - rec[t][2],
                 "gf": rec[t][1], "conduct": meta[t]["conduct"], "fifa": meta[t]["fifa"]}
                for t in names]
        ranked = rank_group(rows, allres)
        winners[L] = ranked[0]["team"]
        runners[L] = ranked[1]["team"]
        t3 = ranked[2]
        thirds.append({**t3, "group": L})

    thirds.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"],
                               -(r["conduct"] or 0),
                               (999 if r["fifa"] is None else r["fifa"]), r["team"]))
    qual = thirds[:8]
    tmpl = ctx["template"]
    combo = "".join(sorted(t["group"] for t in qual))
    assignment = (tmpl.get("thirdAllocation") or {}).get(combo)
    third_by_slot = {}
    slots = tmpl.get("thirdSlots") or []
    if assignment:
        byG = {t["group"]: t["team"] for t in qual}
        for slot, g in zip(slots, assignment):
            third_by_slot[slot] = byG.get(g)
    else:
        for i, slot in enumerate(slots):
            if i < len(qual):
                third_by_slot[slot] = qual[i]["team"]

    results = {}
    for md, code in ctx["defs"]:
        home = _side_from_code(md["home"], md, winners, runners, third_by_slot, results)
        away = _side_from_code(md["away"], md, winners, runners, third_by_slot, results)
        if home and away:
            w, l = _play_winner(home, away, R, P, rng)
            results[md["match"]] = {"winner": w, "loser": l}
    fin = results.get(ctx["final_match"])
    return fin["winner"] if fin else None


# ===== build the static simulation context from data.json ====================
def build_ctx(data):
    sim = data["sim"]
    groupsDef = sim["groupsDef"]
    letters = list(groupsDef.keys())
    meta = {}
    for disp, info in sim["teams"].items():
        meta[disp] = {"canon": info.get("canon") or disp,
                      "fifa": info.get("fifa"), "conduct": info.get("conduct", 0),
                      "owner": info.get("owner"), "group": info.get("group")}
    results = {L: [dict(m) for m in sim["groupResults"].get(L, [])] for L in letters}
    remaining = {L: [(m["h"], m["a"]) for m in sim["groupRemaining"].get(L, [])]
                 for L in letters}
    tmpl = sim["template"]
    round_keys = ["r32", "r16", "qf", "sf", "final", "third"]
    defs = []
    for key in round_keys:
        for md in tmpl.get(key, []):
            defs.append((md, key))
    defs.sort(key=lambda d: d[0]["match"])
    final_match = (tmpl.get("final") or [{}])[0].get("match")
    return {"letters": letters, "groupsDef": groupsDef, "meta": meta,
            "results": results, "remaining": remaining, "template": tmpl,
            "defs": defs, "final_match": final_match}


def load_targets(odds, ctx):
    """Per-team championship probability from odds.json, keyed by display name
    (joined via each team's canonical/leaderboard name)."""
    by_canon = {}
    for st in odds.get("standings", []):
        for t in st.get("teams", []):
            by_canon[t["name"]] = t.get("odds") or 0.0
    target = {}
    for disp, m in ctx["meta"].items():
        target[disp] = by_canon.get(m["canon"], 0.0)
    return target


# ===== Monte Carlo + calibration =============================================
def champion_freq(ctx, R, P, n, rng):
    counts = {t: 0 for t in ctx["meta"]}
    for _ in range(n):
        c = simulate_once(ctx, R, P, rng)
        if c is not None:
            counts[c] += 1
    return {t: counts[t] / n for t in counts}


def calibrate(ctx, target, P, rng):
    teams = list(ctx["meta"].keys())
    # initial ratings from FIFA rank (the prior)
    R = {}
    for t in teams:
        rk = ctx["meta"][t]["fifa"] or 80
        R[t] = -math.log(max(1, rk))            # rank 1 highest, long tail lower
    priced = [t for t in teams if target.get(t, 0) > 1e-4]
    print("calibrating %d priced teams (up to %d iters x %d sims, early-stop on plateau) ..."
          % (len(priced), CALIB_ITERS, CALIB_SIMS))
    stuck = {t: 0 for t in priced}
    pinned = set()      # teams the current results make unable to reach their odds
    best, stale = 1e9, 0
    for it in range(CALIB_ITERS):
        freq = champion_freq(ctx, R, P, CALIB_SIMS, rng)
        # A team is "structurally blocked" only if it's ALREADY rated near the
        # top of the field yet STILL can't approach its target — that rules out
        # strong teams that are merely slow to climb in early iterations.
        rsort = sorted(R.values(), reverse=True)
        hi_thresh = rsort[max(0, int(0.25 * len(rsort)) - 1)]   # ~top quartile
        for t in priced:
            if t in pinned:
                continue
            severe = freq[t] < 0.25 * target[t]
            if not severe:
                stuck[t] = 0
            elif R[t] >= hi_thresh:        # top-rated and still hopeless
                stuck[t] += 1
                if stuck[t] >= 4:
                    pinned.add(t)
            # severe but not yet top-rated: still climbing, leave the counter be
        reachable = [t for t in priced if t not in pinned]
        for t in pinned:                   # park zombies on a comparable live team
            if reachable:
                nn = min(reachable, key=lambda u: abs(target[u] - target[t]))
                R[t] = R[nn]
        decay = STEP / (1.0 + 0.04 * it)
        err = err_track = 0.0
        for t in reachable:
            f = max(freq[t], EPS)
            tg = max(target[t], EPS)
            d = max(-STEP_CAP, min(STEP_CAP, decay * (math.log(tg) - math.log(f))))
            R[t] = max(-8.0, min(8.0, R[t] + d))
            miss = abs(target[t] - freq[t])
            err = max(err, miss)
            if freq[t] >= 0.25 * target[t]:        # convergence judged on trackers
                err_track = max(err_track, miss)
        m = sum(R.values()) / len(R)
        for t in teams:
            R[t] -= m
        if (it + 1) % 5 == 0 or it == CALIB_ITERS - 1:
            print("  iter %2d  max |market-model| = %.4f  (%d reachable)"
                  % (it + 1, err, len(reachable)))
        if err_track < best - 0.0015:
            best, stale = err_track, 0
        else:
            stale += 1
            if stale >= 6:
                print("  converged (no improvement for 6 iters) — stopping at iter %d" % (it + 1))
                break
    return R, priced, pinned


def main():
    data_path = sys.argv[1] if len(sys.argv) > 1 else "data.json"
    odds_path = sys.argv[2] if len(sys.argv) > 2 else "odds.json"
    data = json.load(open(data_path, encoding="utf-8"))
    odds = json.load(open(odds_path, encoding="utf-8"))
    if "sim" not in data:
        sys.exit("data.json has no `sim` block — run the real generator (or the "
                 "demo generator) so the bracket template is present.")

    rng = random.Random(20260619)
    ctx = build_ctx(data)
    target = load_targets(odds, ctx)
    R, priced, pinned = calibrate(ctx, target, PARAMS, rng)

    print("final %d-sim validation pass ..." % FINAL_SIMS)
    freq = champion_freq(ctx, R, PARAMS, FINAL_SIMS, rng)

    elo = {
        "generated": data.get("generated"),
        "source": "build_elos.py — FIFA-rank prior, calibrated to %s" % odds_path,
        "sims": FINAL_SIMS,
        "params": PARAMS,
        "ratings": {t: round(R[t], 4) for t in R},          # live (front end reads this)
        "calibrated": {t: round(R[t], 4) for t in R},        # anchor (update_odds nudges from this)
        "baselineProb": {t: round(target[t], 6) for t in target},
        "canon": {t: ctx["meta"][t]["canon"] for t in ctx["meta"]},
        "modelChamp": {t: round(freq[t], 6) for t in freq},
    }
    json.dump(elo, open("elo.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    fit = [t for t in priced if t not in pinned]
    worst = max(fit, key=lambda t: abs(target[t] - freq[t])) if fit else None
    print("wrote elo.json — %d ratings." % len(R))
    if worst:
        print("worst reachable-team miss: %s  market %.3f  model %.3f"
              % (worst, target[worst], freq[worst]))
    if pinned:
        print("structurally eliminated by current results (champ ~0 despite a market line; "
              "rating pinned to a comparable side): " + ", ".join(sorted(pinned)))
    top = sorted(freq, key=lambda t: -freq[t])[:6]
    print("model title odds (top 6): "
          + ", ".join("%s %.1f%%" % (t, 100 * freq[t]) for t in top))


if __name__ == "__main__":
    main()
