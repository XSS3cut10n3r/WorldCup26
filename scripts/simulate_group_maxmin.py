#!/usr/bin/env python3
"""
simulate_group_maxmin.py — EXACT third-place qualification bounds (with GD/GF).

The Round of 32 takes the 12 group winners, the 12 runners-up, and the 8 BEST
third-placed teams, ranked on: Points, Goal Difference, Goals For, Conduct, then
FIFA ranking. The 8th best third is the last team in; the 9th is the first out.

Late in the group stage the points are jammed together (every contender on the
same total), so the seat is decided on GOAL DIFFERENCE and GOALS FOR. This tool
therefore reports the bounds as full lines (Pts, GD, GF), not just points, and
computes everything EXACTLY by enumerating the actual remaining games:

  * groups already finished contribute a FIXED third-place line;
  * each group still to play contributes a third whose identity and GD/GF depend
    on the results, so its remaining games are enumerated over every plausible
    scoreline (each side 0..MAXG per game) to get the full set of possible
    third-place lines.

From those:
  * HIGHEST possible cut-off = the strongest the 8th-best third can be forced to
    be: the 8th-best line when every still-open group fields its STRONGEST
    possible third (independent groups, so all maxima are simultaneously real).
  * LOWEST possible cut-off  = the weakest line still good enough for that 8th
    seat: the 8th-best when every open group fields its WEAKEST possible third.
  * CLINCHED  = qualifies (top-2, or a top-8 third) in EVERY remaining scenario.
  * ELIMINATED= qualifies in NONE.
  * BUBBLE    = everything else: the seat is still live for them.

Outputs:
  --svg  bubble.svg     the /#bubble graphic: the two line bounds, who sets each,
                        and where every contender for the last seat stands on GD/GF.
  --knockout-json PATH  clamp knockout_odds.json in place to the math: odds = 1.0
                        only for clinched, 0.0 only for eliminated, else [0.01,0.99].
  --json PATH           machine-readable summary.

  python3 simulate_group_maxmin.py --svg bubble.svg --knockout-json knockout_odds.json

--sims/--seed/--workers/--top are accepted and ignored (this is exact, not sampled).

Note: conduct cards in games still to play are not predicted; conduct is held at
its current value (it only matters as a 4th-level tie-break after GF anyway).
"""

import argparse
import itertools
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simulate_pool import load, build_model, rank_group  # noqa: E402


def LINEKEY(L):
    return (-L["points"], -L["gd"], -L["gf"], -(L["conduct"] or 0),
            (999 if L["fifa"] is None else L["fifa"]))


# ----------------------------------------------------------------------------
# State per group.
# ----------------------------------------------------------------------------
def group_records(model):
    sim = model["sim"]
    gdef = sim["groupsDef"]
    res = sim.get("groupResults") or {}
    rem = sim.get("groupRemaining") or {}
    fifa = model["fifa_of"]
    cond = model["conduct_of"]
    out = {}
    for letter in gdef:
        names = list(gdef[letter])
        base = {t: {"pts": 0, "gf": 0, "ga": 0} for t in names}
        played = []
        for m in (res.get(letter) or []):
            played.append(m)
            for (t, gf, ga) in ((m["h"], m["hg"], m["ag"]), (m["a"], m["ag"], m["hg"])):
                base[t]["gf"] += gf
                base[t]["ga"] += ga
                if gf > ga:
                    base[t]["pts"] += 3
                elif gf == ga:
                    base[t]["pts"] += 1
        out[letter] = {
            "names": names, "base": base, "played": played,
            "rem": [(m["h"], m["a"]) for m in (rem.get(letter) or [])],
            "fifa": {t: fifa.get(t) for t in names},
            "cond": {t: cond.get(t, 0) for t in names},
        }
    return out


def _line(team, pts, gd, gf, con, fifa, group):
    return {"team": team, "points": pts, "gd": gd, "gf": gf,
            "conduct": con, "fifa": fifa, "group": group}


def current_standings(g, letter=None):
    rows = [{
        "team": t, "points": g["base"][t]["pts"],
        "gd": g["base"][t]["gf"] - g["base"][t]["ga"],
        "gf": g["base"][t]["gf"], "ga": g["base"][t]["ga"],
        "p": sum(1 for m in g["played"] if t in (m["h"], m["a"])),
        "conduct": g["cond"].get(t, 0), "fifa": g["fifa"].get(t),
    } for t in g["names"]]
    return rank_group(rows, g["played"])


def _adaptive_maxg(n_rem):
    """Cap goals per side per game so enumeration stays cheap; smaller when many
    games remain (early stage, where points already separate the field)."""
    slots = 2 * n_rem
    if slots == 0:
        return 0
    cap = 200_000
    g = 9
    while g > 1 and (g + 1) ** slots > cap:
        g -= 1
    return min(9, g)


def group_outcomes(g, letter):
    """Enumerate scorelines of the remaining games -> per-team possible positions
    and (when 3rd) third-place lines, plus the group's full set of possible
    third-place lines."""
    rem = g["rem"]
    names = g["names"]
    maxg = _adaptive_maxg(len(rem))
    per = {t: {"pos": set(), "third": []} for t in names}
    third_lines = []
    seen_third_key = set()
    for goals in itertools.product(range(maxg + 1), repeat=2 * len(rem)):
        res = list(g["played"])
        for i, (h, a) in enumerate(rem):
            res.append({"h": h, "a": a, "hg": goals[2 * i], "ag": goals[2 * i + 1]})
        rec = {t: {"pts": 0, "gf": 0, "ga": 0} for t in names}
        for m in res:
            for (t, gf, ga) in ((m["h"], m["hg"], m["ag"]), (m["a"], m["ag"], m["hg"])):
                rec[t]["gf"] += gf
                rec[t]["ga"] += ga
                if gf > ga:
                    rec[t]["pts"] += 3
                elif gf == ga:
                    rec[t]["pts"] += 1
        rows = [{"team": t, "points": rec[t]["pts"], "gd": rec[t]["gf"] - rec[t]["ga"],
                 "gf": rec[t]["gf"], "ga": rec[t]["ga"], "conduct": g["cond"].get(t, 0),
                 "fifa": g["fifa"].get(t)} for t in names]
        ranked = rank_group(rows, res)
        for pos, r in enumerate(ranked):
            per[r["team"]]["pos"].add(pos + 1)
        r = ranked[2]
        L = _line(r["team"], r["points"], r["gd"], r["gf"], r["conduct"], r["fifa"], letter)
        k = (L["team"], L["points"], L["gd"], L["gf"], L["conduct"], L["fifa"])
        if k not in seen_third_key:
            seen_third_key.add(k)
            third_lines.append(L)
            per[r["team"]]["third"].append(L)
    return per, third_lines


# ----------------------------------------------------------------------------
# The exact analysis.
# ----------------------------------------------------------------------------
def analyze(model):
    G = group_records(model)
    letters = list(G.keys())
    n_slots = 8

    fixed = []                 # fixed third lines (finished groups)
    fixed_standings = {}       # letter -> ranked standings (finished groups)
    var_third = {}             # letter -> list of possible third lines (open groups)
    var_per = {}               # letter -> per-team {pos, third}
    open_letters = []

    for L in letters:
        if G[L]["rem"]:
            open_letters.append(L)
            per, tl = group_outcomes(G[L], L)
            var_per[L] = per
            var_third[L] = tl
        else:
            ranked = current_standings(G[L], L)
            fixed_standings[L] = ranked
            t = ranked[2]
            fixed.append(_line(t["team"], t["points"], t["gd"], t["gf"],
                               t["conduct"], t["fifa"], L))

    # ---- Line bounds via the 8th order statistic (monotonic, so the extremes
    # come from each open group's strongest / weakest possible third) ----
    def nth(lines):
        s = sorted(lines, key=LINEKEY)
        return s[n_slots - 1] if len(s) >= n_slots else (s[-1] if s else None)

    strongest = {L: min(var_third[L], key=LINEKEY) for L in open_letters}
    weakest = {L: max(var_third[L], key=LINEKEY) for L in open_letters}
    highest_line = nth(fixed + [strongest[L] for L in open_letters])
    lowest_line = nth(fixed + [weakest[L] for L in open_letters])

    # ---- Per-line qualification tests (how many OTHER thirds can / must outrank) ----
    def fixed_outrank(Lx, own):
        return sum(1 for f in fixed if f["group"] != own and LINEKEY(f) < LINEKEY(Lx))

    def can_qualify(Lx, own):                       # favourable: weakest others
        c = fixed_outrank(Lx, own)
        for Lg in open_letters:
            if Lg == own:
                continue
            if all(LINEKEY(t) < LINEKEY(Lx) for t in var_third[Lg]):
                c += 1                              # even its weakest third beats Lx
        return c <= n_slots - 1

    def always_qualifies(Lx, own):                  # adversarial: strongest others
        c = fixed_outrank(Lx, own)
        for Lg in open_letters:
            if Lg == own:
                continue
            if any(LINEKEY(t) < LINEKEY(Lx) for t in var_third[Lg]):
                c += 1                              # it can field a third beating Lx
        return c <= n_slots - 1

    clinched, eliminated, bubble = set(), set(), set()

    for L in letters:
        if L in fixed_standings:
            for pos, r in enumerate(fixed_standings[L]):
                t = r["team"]
                if pos < 2:
                    clinched.add(t)
                elif pos == 3:
                    eliminated.add(t)
                else:
                    Lx = _line(r["team"], r["points"], r["gd"], r["gf"],
                               r["conduct"], r["fifa"], L)
                    if always_qualifies(Lx, L):
                        clinched.add(t)
                    elif not can_qualify(Lx, L):
                        eliminated.add(t)
                    else:
                        bubble.add(t)
        else:
            for t, info in var_per[L].items():
                pos = info["pos"]
                tls = info["third"]
                can = any(p <= 2 for p in pos) or any(can_qualify(Lx, L) for Lx in tls)
                always = (4 not in pos) and all(always_qualifies(Lx, L) for Lx in tls)
                if always:
                    clinched.add(t)
                elif not can:
                    eliminated.add(t)
                else:
                    bubble.add(t)

    # ---- Where the twelve CURRENT thirds stand (provisional ordering) ----
    thirds = []
    for L in letters:
        if L in fixed_standings:
            r = fixed_standings[L][2]
        else:
            r = current_standings(G[L], L)[2]
        team = r["team"]
        status = "through" if team in clinched else ("out" if team in eliminated else "bubble")
        thirds.append({"group": L, "team": team, "points": r["points"], "gd": r["gd"],
                       "gf": r["gf"], "conduct": r["conduct"], "fifa": r["fifa"],
                       "played": r.get("p", 0),
                       "rem": len(G[L]["rem"]), "status": status})
    thirds.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"], -(r["conduct"] or 0),
                               (999 if r["fifa"] is None else r["fifa"])))

    # Bubble teams that are NOT a current third (sitting below 3rd in an open
    # group but still able to climb into a qualifying third-place spot).
    team_group = {t: L for L in letters for t in G[L]["names"]}
    third_teams = {t["team"] for t in thirds}
    off_table_bubble = sorted(
        ({"team": t, "group": team_group.get(t)} for t in bubble if t not in third_teams),
        key=lambda x: (x["group"] or "", x["team"]))

    # ---- Contenders for the table: every through third + every bubble team
    # (eliminated sides are dropped). Bubble teams not currently 3rd get their
    # current group record so they still appear. ----
    def team_row(t):
        L = team_group[t]
        for r in current_standings(G[L], L):
            if r["team"] == t:
                st = "through" if t in clinched else ("out" if t in eliminated else "bubble")
                return {"group": L, "team": t, "points": r["points"], "gd": r["gd"],
                        "gf": r["gf"], "conduct": r["conduct"], "fifa": r["fifa"],
                        "played": r.get("p", 0),
                        "rem": len(G[L]["rem"]), "status": st}
        return None

    contenders = []
    seen = set()
    for r in thirds:
        if r["status"] != "out":
            contenders.append(r)
            seen.add(r["team"])
    for t in bubble:
        if t not in seen:
            row = team_row(t)
            if row:
                contenders.append(row)
                seen.add(t)
    contenders.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"], -(r["conduct"] or 0),
                                   (999 if r["fifa"] is None else r["fifa"])))

    return {
        "groups": letters,
        "highest_line": highest_line,
        "lowest_line": lowest_line,
        "clinched": clinched,
        "eliminated": eliminated,
        "bubble": bubble,
        "current_thirds": thirds,
        "contenders": contenders,
        "off_table_bubble": off_table_bubble,
        "games_remaining": sum(len(G[L]["rem"]) for L in letters),
        "open_groups": open_letters,
    }


# ----------------------------------------------------------------------------
# Monte-Carlo over the remaining group games: per-team qualification odds and
# the distribution of the cut-off line. Seeded, so output is deterministic.
# ----------------------------------------------------------------------------
def bubble_montecarlo(model, n_sims, seed=1):
    import random
    from simulate_pool import make_engine
    random.seed(seed)
    _, elo_score, _ = make_engine(model)
    G = group_records(model)
    letters = list(G.keys())
    fifa, cond = model["fifa_of"], model["conduct_of"]

    def key(L):
        return (-L["points"], -L["gd"], -L["gf"], -(L["conduct"] or 0),
                (999 if L["fifa"] is None else L["fifa"]), L["team"])

    fixed = []
    og = []
    for L in letters:
        if G[L]["rem"]:
            og.append((L, G[L]["names"], G[L]["played"], G[L]["rem"]))
        else:
            r = current_standings(G[L], L)[2]
            fixed.append({"team": r["team"], "points": r["points"], "gd": r["gd"],
                          "gf": r["gf"], "conduct": r["conduct"], "fifa": r["fifa"]})

    qual = {}
    cut_gd, cut_pts = {}, {}
    for _ in range(n_sims):
        thirds = list(fixed)
        qualified = set()
        for (L, names, played, rem) in og:
            rec = {t: [0, 0, 0] for t in names}      # pts, gf, ga
            res = list(played)
            for (h, a) in rem:
                hg, ag = elo_score(h, a, group=True)
                res.append({"h": h, "a": a, "hg": hg, "ag": ag})
            for m in res:
                for (t, gf, ga) in ((m["h"], m["hg"], m["ag"]), (m["a"], m["ag"], m["hg"])):
                    rc = rec[t]
                    rc[1] += gf
                    rc[2] += ga
                    if gf > ga:
                        rc[0] += 3
                    elif gf == ga:
                        rc[0] += 1
            rows = [{"team": t, "points": rec[t][0], "gd": rec[t][1] - rec[t][2],
                     "gf": rec[t][1], "conduct": cond.get(t, 0), "fifa": fifa.get(t)}
                    for t in names]
            ranked = rank_group(rows, res)
            qualified.add(ranked[0]["team"])
            qualified.add(ranked[1]["team"])
            thirds.append(ranked[2])
        thirds.sort(key=key)
        for t in thirds[:8]:
            qualified.add(t["team"])
        c = thirds[7]
        cut_gd[c["gd"]] = cut_gd.get(c["gd"], 0) + 1
        cut_pts[c["points"]] = cut_pts.get(c["points"], 0) + 1
        for t in qualified:
            qual[t] = qual.get(t, 0) + 1

    return {
        "n": n_sims,
        "qualify": {t: qual.get(t, 0) / n_sims for t in qual},
        "cut_gd": {g: c / n_sims for g, c in cut_gd.items()},
        "cut_pts": {p: c / n_sims for p, c in cut_pts.items()},
    }


# ----------------------------------------------------------------------------
# knockout_odds.json clamp.
# ----------------------------------------------------------------------------
def clamp_knockout_odds(path, clinched, eliminated):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"knockout-json: could not read {path} ({e}); nothing clamped.")
        return False
    d5 = lambda x: round(x, 5)
    changed = False
    for t in doc.get("teams", []):
        nm = t.get("name")
        old = t.get("odds")
        if nm in clinched:
            new = 1.0
        elif nm in eliminated:
            new = 0.0
        else:
            new = min(0.99, max(0.01, float(old or 0.0)))
        new = d5(new)
        if old != new:
            t["odds"] = new
            changed = True
        base = t.get("base")
        if nm in clinched or nm in eliminated:
            if t.get("delta") is not None:
                t["delta"] = None
                changed = True
        elif base is not None:
            nd = d5(t["odds"] - base)
            nd = nd if nd != 0 else None
            if t.get("delta") != nd:
                t["delta"] = nd
                changed = True
    doc.get("teams", []).sort(key=lambda t: (-(t.get("odds") or 0), t.get("name", "").lower()))
    if not changed:
        print(f"knockout-json: {path} already mathematically consistent; left untouched.")
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"knockout-json: clamped {path} "
          f"({len(clinched)} clinched -> 1.0, {len(eliminated)} eliminated -> 0.0).")
    return True


# ----------------------------------------------------------------------------
# SVG graphic. Editorial styling; monospaced scoreboard numerals. No embedded
# timestamp (identical model output -> identical bytes). No em dashes; Unicode
# minus for negative GD.
# ----------------------------------------------------------------------------
def _xesc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _gd(v):
    return f"+{v}" if v > 0 else (f"\u2212{-v}" if v < 0 else "0")


def _serif_width(s, size):
    """Approximate rendered width of a serif string, calibrated to err slightly
    long so a strikethrough fully covers the name across fonts/renderers."""
    w = 0.0
    for ch in s:
        if ch == " ":
            w += 0.26
        elif ch in "iIjl.,'!|":
            w += 0.30
        elif ch in "ft":
            w += 0.34
        elif ch in "mwMW":
            w += 0.84
        elif ch.isupper():
            w += 0.64
        else:
            w += 0.50
    return w * size * 1.18 + 3


def _disp_width(s, size, ls):
    """Approximate rendered width of an Oswald (condensed) string at the given
    size and px letter-spacing; calibrated to err slightly long."""
    return len(s) * (size * 0.47 + ls)


def _catmull_rom(pts):
    """Smooth path through points (list of (x,y)) using Catmull-Rom -> cubic
    beziers; returns the 'd' segment after an initial move/line to pts[0]."""
    if len(pts) < 2:
        return ""
    d = []
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else pts[i + 1]
        c1x = p1[0] + (p2[0] - p0[0]) / 6.0
        c1y = p1[1] + (p2[1] - p0[1]) / 6.0
        c2x = p2[0] - (p3[0] - p1[0]) / 6.0
        c2y = p2[1] - (p3[1] - p1[1]) / 6.0
        d.append(f"C {c1x:.1f} {c1y:.1f} {c2x:.1f} {c2y:.1f} {p2[0]:.1f} {p2[1]:.1f}")
    return " ".join(d)


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve"]


def _word(n):
    return _ONES[n] if 0 <= n < len(_ONES) else str(n)


def render_bubble_svg(A, MC=None):
    hi, lo = A["highest_line"], A["lowest_line"]
    contenders = A["contenders"]
    rem_games = A["games_remaining"]
    clinched, eliminated, bubble = A["clinched"], A["eliminated"], A["bubble"]
    n_groups = len(A["groups"])

    BG, BG2 = "#15431C", "#0E3014"
    CREAM = "#F2F0E4"
    DIM = "rgba(242,240,228,0.84)"
    FAINT = "rgba(242,240,228,0.68)"
    LINE = "rgba(242,240,228,0.22)"
    GOLD, UP, DOWN, ICE = "#E8C34A", "#5FD083", "#FF6B57", "#7FDCEF"
    SHADOW = "rgba(0,0,0,0.30)"

    def gdcol(v):
        return UP if v > 0 else (DOWN if v < 0 else DIM)

    qual = (MC or {}).get("qualify", {})

    def qof(t):
        return qual.get(t, 1.0 if t in clinched else 0.0)

    rows = sorted(contenders, key=lambda r: (-qof(r["team"]), -r["points"], -r["gd"],
                                             -r["gf"], r["team"]))
    n_in = sum(1 for t in A["current_thirds"] if t["status"] == "through")
    has_curve = bool(MC and MC.get("cut_gd"))

    out = []
    a = out.append
    W, LX, RX = 1080, 80, 1000

    # ---- layout cursor (new order: bounds -> curve -> race) ----
    bounds_head = 360
    cards_y = bounds_head + 26
    cards_h = 196
    if has_curve:
        curve_head = cards_y + cards_h + 62
        curve_top = curve_head + 22
        curve_h = 188
        curve_bottom = curve_top + curve_h
        race_head = curve_bottom + 86
    else:
        curve_head = curve_top = curve_bottom = None
        race_head = cards_y + cards_h + 58
    race_rowtop = race_head + 34
    rh = 40
    band_h = 42
    band_after = 8
    show_band = len(rows) > band_after
    table_h = len(rows) * rh + (band_h if show_band else 0)
    race_bottom = race_rowtop + table_h + 14
    legend_y = race_bottom + 28
    foot_y = legend_y + 50
    H = foot_y + 38

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">')
    a('<defs><style>'
      '.disp{font-family:Oswald,"Arial Narrow",Helvetica,Arial,sans-serif;}'
      '.body{font-family:Barlow,"Helvetica Neue",Arial,sans-serif;}'
      '</style>'
      '<radialGradient id="glow" cx="50%" cy="0%" r="80%">'
      '<stop offset="0%" stop-color="#F2F0E4" stop-opacity="0.07"/>'
      '<stop offset="55%" stop-color="#F2F0E4" stop-opacity="0"/>'
      '</radialGradient></defs>')
    a(f'<rect x="0" y="0" width="{W}" height="{H}" fill="{BG}"/>')
    k = 88
    while k < H:
        a(f'<rect x="0" y="{k}" width="{W}" height="88" fill="{BG2}"/>')
        k += 176
    a(f'<rect x="0" y="0" width="{W}" height="{H}" fill="url(#glow)"/>')

    def section(label, y, size=16, ls=2):
        a(f'<text class="disp" x="{LX}" y="{y}" fill="{CREAM}" font-size="{size}" '
          f'font-weight="600" letter-spacing="{ls}">{label}</text>')
        rx0 = LX + _disp_width(label, size, ls) + 16
        a(f'<line x1="{rx0:.0f}" y1="{y - 5}" x2="{RX}" y2="{y - 5}" stroke="{LINE}" '
          'stroke-width="2"/>')

    # ---- hero with DYNAMIC deck ----
    a(f'<text class="disp" x="{LX}" y="60" fill="{DIM}" font-size="13" font-weight="500" '
      'letter-spacing="4">WORLD CUP 2026&#160;&#160;&#183;&#160;&#160;THE THIRD-PLACE RACE</text>')

    def shadowed(label, y, size, ls=1):
        a(f'<text class="disp" x="{LX}" y="{y + 2}" fill="{SHADOW}" font-size="{size}" '
          f'font-weight="700" letter-spacing="{ls}">{label}</text>')
        a(f'<text class="disp" x="{LX}" y="{y}" fill="{CREAM}" font-size="{size}" '
          f'font-weight="700" letter-spacing="{ls}">{label}</text>')
    shadowed("THE RACE FOR THE LAST", 132, 58)
    shadowed("THIRD-PLACE TICKET", 196, 58)

    spots, grps = "Eight", _word(n_groups)
    if rem_games == 0:
        deckA = f"{spots} of the {grps} third-placed teams advance. The group stage is"
        deckB = "complete, so the eight qualifiers are now locked."
    elif n_in >= 8:
        g = "game" if rem_games == 1 else "games"
        deckA = f"{spots} of the {grps} third-placed teams advance, and all eight places are"
        deckB = f"already settled with {_word(rem_games)} {g} still to play."
    else:
        g = "game" if rem_games == 1 else "games"
        deckA = (f"{spots} of the {grps} third-placed teams advance. "
                 f"{_word(n_in).capitalize()} are already in; the rest is a")
        deckB = (f"goal-difference scrap between the sides below, with "
                 f"{_word(rem_games)} {g} left to play.")
    a(f'<text class="body" x="{LX}" y="250" fill="{DIM}" font-size="20">{deckA}</text>')
    a(f'<text class="body" x="{LX}" y="280" fill="{DIM}" font-size="20">{deckB}</text>')

    # ---- exact bounds cards ----
    section("THE CUT-OFF LINE, BOUNDED", bounds_head)
    cards = [("LOWEST POSSIBLE", "the easiest the bar can get", UP, lo),
             ("HIGHEST POSSIBLE", "the hardest the bar can get", DOWN, hi)]
    cw = 440
    for i, (tier, sub, accent, L) in enumerate(cards):
        x = LX + i * (cw + 40)
        a(f'<rect x="{x}" y="{cards_y}" width="{cw}" height="{cards_h}" rx="8" fill="{BG2}" '
          f'stroke="{LINE}" stroke-width="1"/>')
        a(f'<rect x="{x}" y="{cards_y}" width="6" height="{cards_h}" fill="{accent}"/>')
        a(f'<text class="disp" x="{x + 30}" y="{cards_y + 40}" fill="{accent}" font-size="18" '
          f'font-weight="600" letter-spacing="1.5">{tier}</text>')
        a(f'<text class="body" x="{x + 30}" y="{cards_y + 62}" fill="{DIM}" font-size="13">{sub}</text>')
        if L is None:
            a(f'<text class="body" x="{x + 30}" y="{cards_y + 120}" fill="{DIM}" font-size="22" '
              'font-style="italic">not yet defined</text>')
        else:
            for cxp, lab, val in [(x + 42, "PTS", str(L["points"])), (x + 178, "GD", _gd(L["gd"])),
                                  (x + 322, "GF", str(L["gf"]))]:
                a(f'<text class="disp" x="{cxp}" y="{cards_y + 96}" fill="{DIM}" font-size="12" '
                  f'font-weight="600" letter-spacing="2">{lab}</text>')
                a(f'<text class="disp" x="{cxp}" y="{cards_y + 148}" fill="{CREAM}" font-size="50" '
                  f'font-weight="700">{val}</text>')
            a(f'<text class="body" x="{x + 30}" y="{cards_y + 182}" fill="{DIM}" font-size="13">'
              f'set by <tspan fill="{GOLD}" font-weight="700">{_xesc(L["team"])}</tspan> '
              f'(Group {L["group"]})</text>')

    # ---- cut-off distribution curve ----
    if has_curve:
        section("WHERE THE CUT-OFF LANDS", curve_head)
        dist = MC["cut_gd"]
        gmin, gmax = min(dist), max(dist)
        xs = list(range(gmin - 1, gmax + 2))
        ymax = max(dist.values())
        px0, px1 = LX + 30, RX - 30
        baseline = curve_bottom
        sx = (px1 - px0) / (len(xs) - 1)
        sy = (curve_h - 26) / ymax

        def X(g):
            return px0 + (g - xs[0]) * sx

        pts = [(X(g), baseline - dist.get(g, 0.0) * sy) for g in xs]
        area = (f'M {pts[0][0]:.1f} {baseline} L {pts[0][0]:.1f} {pts[0][1]:.1f} '
                + _catmull_rom(pts) + f' L {pts[-1][0]:.1f} {baseline} Z')
        a(f'<path d="{area}" fill="{ICE}" fill-opacity="0.16"/>')
        a(f'<path d="M {pts[0][0]:.1f} {pts[0][1]:.1f} ' + _catmull_rom(pts)
          + f'" fill="none" stroke="{ICE}" stroke-width="2.5"/>')
        a(f'<line x1="{px0}" y1="{baseline}" x2="{px1}" y2="{baseline}" stroke="{LINE}" '
          'stroke-width="1"/>')
        for g in range(gmin, gmax + 1):
            pr = dist.get(g, 0.0)
            xg = X(g)
            if pr > 0:
                yg = baseline - pr * sy
                a(f'<circle cx="{xg:.1f}" cy="{yg:.1f}" r="3.5" fill="{ICE}"/>')
                a(f'<text class="disp" x="{xg:.1f}" y="{yg - 12:.1f}" fill="{CREAM}" '
                  f'font-size="14" font-weight="700" text-anchor="middle">{round(pr * 100)}%</text>')
            a(f'<text class="disp" x="{xg:.1f}" y="{baseline + 22:.1f}" fill="{DIM}" '
              f'font-size="15" font-weight="600" text-anchor="middle">{_gd(g)}</text>')
        a(f'<text class="body" x="{(px0 + px1) / 2:.0f}" y="{baseline + 44:.0f}" fill="{FAINT}" '
          'font-size="12.5" text-anchor="middle">Goal difference of the last qualifying third '
          'across every simulated finish (it is 3 points in every run).</text>')
        for L, lab, col in [(lo, "LOWEST", UP), (hi, "HIGHEST", DOWN)]:
            if L and gmin <= L["gd"] <= gmax:
                xg = X(L["gd"])
                a(f'<line x1="{xg:.1f}" y1="{baseline}" x2="{xg:.1f}" y2="{curve_top + 8}" '
                  f'stroke="{col}" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.85"/>')
                a(f'<text class="disp" x="{xg:.1f}" y="{curve_top}" fill="{col}" font-size="11" '
                  f'font-weight="600" letter-spacing="1" text-anchor="middle">{lab}</text>')

    # ---- contenders table with qualify bars + gold cut-off band ----
    section("THE RACE FOR THE LAST SPOTS", race_head)
    a(f'<rect x="72" y="{race_rowtop - 22}" width="936" height="{race_bottom - race_rowtop + 12}" '
      f'rx="8" fill="rgba(10,38,18,0.55)" stroke="{LINE}" stroke-width="1"/>')
    hy = race_rowtop - 4
    for hx, lab, anch in [(132, "TEAM", "start"), (372, "PL", "middle"), (430, "PTS", "middle"),
                          (486, "GD", "middle"), (542, "GF", "middle")]:
        a(f'<text class="disp" x="{hx}" y="{hy}" fill="{DIM}" font-size="11.5" font-weight="600" '
          f'letter-spacing="2" text-anchor="{anch}">{lab}</text>')
    a(f'<text class="disp" x="600" y="{hy}" fill="{DIM}" font-size="11.5" font-weight="600" '
      'letter-spacing="2">CHANCE TO QUALIFY&#160;&#160;(share of simulations)</text>')

    bar_x0, bar_x1 = 600, 940
    for i, r in enumerate(rows):
        offset = band_h if (show_band and i >= band_after) else 0
        top = race_rowtop + i * rh + offset
        cyl = top + rh / 2
        if i % 2 == 1:
            a(f'<rect x="{LX}" y="{top}" width="920" height="{rh}" fill="rgba(242,240,228,0.05)"/>')
        nm = r["team"]
        a(f'<text class="body" x="132" y="{cyl + 6}" fill="{CREAM}" font-size="19.5" '
          f'font-weight="600">{_xesc(nm)}</text>')
        a(f'<text class="disp" x="372" y="{cyl + 6}" fill="{DIM}" font-size="18" '
          f'text-anchor="middle">{r["played"]}</text>')
        a(f'<text class="disp" x="430" y="{cyl + 6}" fill="{CREAM}" font-size="20" '
          f'font-weight="700" text-anchor="middle">{r["points"]}</text>')
        a(f'<text class="disp" x="486" y="{cyl + 6}" fill="{gdcol(r["gd"])}" font-size="20" '
          f'font-weight="700" text-anchor="middle">{_gd(r["gd"])}</text>')
        a(f'<text class="disp" x="542" y="{cyl + 6}" fill="{CREAM}" font-size="20" '
          f'text-anchor="middle">{r["gf"]}</text>')
        p = qof(nm)
        through = nm in clinched
        a(f'<rect x="{bar_x0}" y="{cyl - 8:.0f}" width="{bar_x1 - bar_x0}" height="16" rx="8" '
          f'fill="rgba(242,240,228,0.10)"/>')
        w = max(2, (bar_x1 - bar_x0) * p)
        col = GOLD if through else ICE
        a(f'<rect x="{bar_x0}" y="{cyl - 8:.0f}" width="{w:.1f}" height="16" rx="8" fill="{col}"/>')
        pct = "100%" if p >= 0.9995 else ("&lt;1%" if 0 < p < 0.005 else f"{round(p * 100)}%")
        a(f'<text class="disp" x="{RX}" y="{cyl + 6}" fill="{CREAM}" font-size="17" '
          f'font-weight="700" text-anchor="end">{pct}</text>')

    if show_band:
        band_top = race_rowtop + band_after * rh
        bcy = band_top + band_h / 2
        a(f'<line x1="92" y1="{bcy}" x2="988" y2="{bcy}" stroke="{GOLD}" stroke-width="2" '
          'stroke-dasharray="6 5"/>')
        plabel = "TOP 8 ADVANCE"
        pw = _disp_width(plabel, 14, 2) + 40
        a(f'<rect x="{540 - pw / 2:.0f}" y="{bcy - 15:.0f}" width="{pw:.0f}" height="30" rx="15" '
          f'fill="{GOLD}"/>')
        a(f'<text class="disp" x="540" y="{bcy + 6:.0f}" fill="{BG2}" font-size="14" '
          f'font-weight="600" letter-spacing="2" text-anchor="middle">{plabel}</text>')

    # ---- legend + footer ----
    nstr = f"{MC['n']:,}" if MC else ""
    a(f'<text class="body" x="{LX}" y="{legend_y}" fill="{FAINT}" font-size="12.5">'
      'Gold = already through as a top-eight third. Teal = on the bubble: chance to qualify'
      + (f' across {nstr} simulated finishes,' if MC else ',') + ' sorted most to least likely.</text>')
    a(f'<text class="body" x="{LX}" y="{legend_y + 18}" fill="{FAINT}" font-size="12.5">'
      'Rows above the gold line are the projected top eight. Eliminated sides are not shown.</text>')
    a(f'<rect x="{LX}" y="{foot_y - 12}" width="11" height="11" fill="{GOLD}"/>')
    a(f'<text class="body" x="100" y="{foot_y - 2}" fill="{CREAM}" font-size="15" '
      'font-weight="700">The bounds are exact; the chances and the curve are from the '
      'simulations.</text>')
    a('</svg>')
    return "\n".join(out) + "\n"






# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Exact third-place qualification line bounds (GD/GF), 8th best third.")
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--elo", default="elo.json")
    ap.add_argument("--svg", default=None)
    ap.add_argument("--knockout-json", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--sims", type=int, default=None)      # also used for the bubble MC
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--bubble-sims", type=int, default=None,
                    help="sims for the qualify-odds/cut-off curve (default: --sims, else 200000)")
    args = ap.parse_args()

    data = load(args.data)
    try:
        elo = load(args.elo)
    except Exception:
        elo = {}
    model = build_model(elo, data)
    A = analyze(model)

    def fmt(L):
        return "none" if L is None else (f'{L["team"]} (G{L["group"]}): {L["points"]} pts, '
                                         f'GD {_gd(L["gd"])}, GF {L["gf"]}')
    print(f"Highest possible cut-off: {fmt(A['highest_line'])}")
    print(f"Lowest  possible cut-off: {fmt(A['lowest_line'])}")
    print(f"Clinched ({len(A['clinched'])}), Eliminated ({len(A['eliminated'])}), "
          f"Bubble ({len(A['bubble'])}): {', '.join(sorted(A['bubble']))}")

    MC = None
    if A["open_groups"]:
        n = args.bubble_sims or args.sims or 200000
        MC = bubble_montecarlo(model, n, seed=args.seed or 1)
        cg = ", ".join(f"GD {_gd(g)}: {round(p * 100)}%"
                       for g, p in sorted(MC["cut_gd"].items()))
        print(f"Cut-off GD distribution ({MC['n']:,} sims): {cg}")

    if args.svg:
        svg = render_bubble_svg(A, MC)
        if os.path.exists(args.svg):
            try:
                with open(args.svg, encoding="utf-8") as f:
                    if f.read() == svg:
                        print(f"{args.svg} unchanged; left untouched.")
                        svg = None
            except Exception:
                pass
        if svg is not None:
            with open(args.svg, "w", encoding="utf-8") as f:
                f.write(svg)
            print(f"Wrote {args.svg}.")

    if args.knockout_json:
        clamp_knockout_odds(args.knockout_json, A["clinched"], A["eliminated"])

    if args.json:
        summary = {
            "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "highest_line": A["highest_line"],
            "lowest_line": A["lowest_line"],
            "clinched": sorted(A["clinched"]),
            "eliminated": sorted(A["eliminated"]),
            "bubble": sorted(A["bubble"]),
            "current_thirds": A["current_thirds"],
            "qualify_odds": (MC["qualify"] if MC else None),
            "cut_gd_distribution": (MC["cut_gd"] if MC else None),
            "sims": (MC["n"] if MC else None),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Wrote {args.json}.")


if __name__ == "__main__":
    main()
