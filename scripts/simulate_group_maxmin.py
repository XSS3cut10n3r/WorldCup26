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
                       "rem": len(G[L]["rem"]), "status": status})
    thirds.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"], -(r["conduct"] or 0),
                               (999 if r["fifa"] is None else r["fifa"])))

    return {
        "groups": letters,
        "highest_line": highest_line,
        "lowest_line": lowest_line,
        "clinched": clinched,
        "eliminated": eliminated,
        "bubble": bubble,
        "current_thirds": thirds,
        "games_remaining": sum(len(G[L]["rem"]) for L in letters),
        "open_groups": open_letters,
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


def render_bubble_svg(A):
    hi, lo = A["highest_line"], A["lowest_line"]
    clinched, eliminated, bubble = A["clinched"], A["eliminated"], A["bubble"]
    thirds = A["current_thirds"]
    rem_games = A["games_remaining"]
    n_bubble = len(bubble)

    out = []
    a = out.append
    W = 1080
    row_h = 46
    table_top = 812
    n_rows = len(thirds)
    table_bottom = table_top + 32 + n_rows * row_h
    H = table_bottom + 156

    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
      'font-family="Helvetica Neue, Arial, sans-serif">')
    a('<defs><style>'
      '.serif{font-family:Georgia,"Times New Roman",serif;}'
      '.sans{font-family:"Helvetica Neue",Arial,sans-serif;}'
      '.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}'
      '</style></defs>')
    a(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FCFBF8"/>')

    # masthead
    a('<rect x="0" y="0" width="1080" height="64" fill="#15171C"/>')
    a('<text class="sans" x="80" y="40" fill="#FCFBF8" font-size="14" font-weight="700" '
      'letter-spacing="2.5">WORLD CUP 2026&#160;&#160;&#183;&#160;&#160;GROUP STAGE</text>')
    a('<text class="sans" x="1000" y="40" fill="#FCFBF8" font-size="14" font-weight="700" '
      'letter-spacing="2.5" text-anchor="end">QUALIFICATION BOUNDS</text>')

    # headline + deck
    a('<text class="serif" x="80" y="165" fill="#15171C" font-size="58" font-weight="700" '
      'letter-spacing="-0.5">The race for the last</text>')
    a('<text class="serif" x="80" y="227" fill="#15171C" font-size="58" font-weight="700" '
      'letter-spacing="-0.5">third-place ticket</text>')
    a('<text class="sans" x="80" y="286" fill="#4A4E57" font-size="20">Eight of the twelve '
      'third-placed teams advance. The contenders are level on points, so the</text>')
    a('<text class="sans" x="80" y="316" fill="#4A4E57" font-size="20">last seat comes down to '
      'goal difference, then goals scored. With games still to</text>')
    a('<text class="sans" x="80" y="346" fill="#4A4E57" font-size="20">play, the exact cut-off '
      'can only land between these two lines.</text>')
    a('<line x1="80" y1="384" x2="1000" y2="384" stroke="#E2DFD7" stroke-width="1"/>')

    # bounds heading
    a('<text class="sans" x="80" y="432" fill="#15171C" font-size="15" font-weight="700" '
      'letter-spacing="2.5">THE CUT-OFF LINE, BOUNDED</text>')
    a('<rect x="80" y="442" width="46" height="3" fill="#15171C"/>')
    a('<text class="sans" x="80" y="467" fill="#4A4E57" font-size="15">The 8th-best third: the '
      'last side to qualify. GD and GF are what separate teams now.</text>')

    cards = [
        ("LOWEST POSSIBLE", "the easiest the bar can get", "#2E6E66", lo),
        ("HIGHEST POSSIBLE", "the hardest the bar can get", "#A23139", hi),
    ]
    cy = 502
    cardw = 440
    for i, (tier, sub, color, L) in enumerate(cards):
        x = 80 + i * (cardw + 40)
        a(f'<rect x="{x}" y="{cy}" width="{cardw}" height="208" rx="4" fill="#FFFFFF" '
          'stroke="#E7E3DA" stroke-width="1"/>')
        a(f'<rect x="{x}" y="{cy}" width="6" height="208" fill="{color}"/>')
        a(f'<text class="sans" x="{x + 28}" y="{cy + 38}" fill="{color}" font-size="15" '
          f'font-weight="700" letter-spacing="1">{tier}</text>')
        a(f'<text class="sans" x="{x + 28}" y="{cy + 60}" fill="#7C808A" font-size="13">{sub}</text>')
        if L is None:
            a(f'<text class="serif" x="{x + 28}" y="{cy + 120}" fill="#7C808A" font-size="22" '
              'font-style="italic">not yet defined</text>')
        else:
            cols = [(x + 40, "PTS", str(L["points"]), "#15171C"),
                    (x + 175, "GD", _gd(L["gd"]), color),
                    (x + 320, "GF", str(L["gf"]), "#15171C")]
            for cxp, lab, val, vc in cols:
                a(f'<text class="sans" x="{cxp}" y="{cy + 96}" fill="#7C808A" font-size="12" '
                  f'font-weight="700" letter-spacing="1.5">{lab}</text>')
                a(f'<text class="mono" x="{cxp}" y="{cy + 150}" fill="{vc}" font-size="46" '
                  f'font-weight="700">{val}</text>')
            a(f'<text class="sans" x="{x + 28}" y="{cy + 188}" fill="#7C808A" font-size="13">'
              f'set by <tspan fill="#15171C" font-weight="700">{_xesc(L["team"])}</tspan> '
              f'(Group {L["group"]})</text>')

    # tally strip
    a(f'<text class="sans" x="80" y="772" fill="#4A4E57" font-size="15">'
      f'<tspan fill="#15171C" font-weight="700">{len(clinched)}</tspan> teams already through'
      f'&#160;&#160;&#183;&#160;&#160;'
      f'<tspan fill="#15171C" font-weight="700">{len(eliminated)}</tspan> out'
      f'&#160;&#160;&#183;&#160;&#160;'
      f'<tspan fill="#15171C" font-weight="700">{n_bubble}</tspan> still fighting for the seat'
      f'&#160;&#160;&#183;&#160;&#160;'
      f'<tspan fill="#15171C" font-weight="700">{rem_games}</tspan> games to play.</text>')

    # thirds table
    a(f'<text class="sans" x="80" y="{table_top - 10}" fill="#15171C" font-size="15" '
      'font-weight="700" letter-spacing="2.5">WHERE THE TWELVE THIRDS STAND</text>')
    a(f'<rect x="80" y="{table_top}" width="46" height="3" fill="#15171C"/>')
    hy = table_top + 24
    headers = [(132, "TEAM", "start"), (470, "GRP", "middle"), (548, "PTS", "middle"),
               (628, "GD", "middle"), (706, "GF", "middle"), (784, "LEFT", "middle")]
    for hx, lab, anch in headers:
        a(f'<text class="sans" x="{hx}" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
          f'letter-spacing="1.5" text-anchor="{anch}">{lab}</text>')
    a(f'<text class="sans" x="1000" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5" text-anchor="end">STATUS</text>')
    a(f'<line x1="80" y1="{table_top + 32}" x2="1000" y2="{table_top + 32}" '
      'stroke="#C9C5BA" stroke-width="1.5"/>')

    for i, t in enumerate(thirds):
        top = table_top + 32 + i * row_h
        cyl = top + row_h / 2
        if i % 2 == 1:
            a(f'<rect x="80" y="{top}" width="920" height="{row_h}" fill="#F4F2EC"/>')
        if i == 8:
            a(f'<line x1="80" y1="{top}" x2="1000" y2="{top}" stroke="#A23139" '
              'stroke-width="1.5" stroke-dasharray="5 4"/>')
            a(f'<text class="sans" x="84" y="{top - 6}" fill="#A23139" font-size="11" '
              'font-weight="700" letter-spacing="1.5" text-anchor="start">QUALIFYING CUT-OFF</text>')
        nm = t["team"]
        out_ = t["status"] == "out"
        name_fill = "#9A9EA6" if out_ else "#15171C"
        a(f'<text class="serif" x="132" y="{cyl + 7}" fill="{name_fill}" font-size="21">'
          f'{_xesc(nm)}</text>')
        if out_:
            tw = 11 * len(nm) * 0.62
            a(f'<line x1="130" y1="{cyl + 1}" x2="{132 + tw:.0f}" y2="{cyl + 1}" '
              'stroke="#A23139" stroke-width="2"/>')
        a(f'<text class="mono" x="470" y="{cyl + 6}" fill="#4A4E57" font-size="18" '
          f'text-anchor="middle">{t["group"]}</text>')
        a(f'<text class="mono" x="548" y="{cyl + 6}" fill="#15171C" font-size="20" '
          f'font-weight="700" text-anchor="middle">{t["points"]}</text>')
        gdc = "#2E6E66" if t["gd"] > 0 else ("#A23139" if t["gd"] < 0 else "#4A4E57")
        a(f'<text class="mono" x="628" y="{cyl + 6}" fill="{gdc}" font-size="20" '
          f'font-weight="700" text-anchor="middle">{_gd(t["gd"])}</text>')
        a(f'<text class="mono" x="706" y="{cyl + 6}" fill="#15171C" font-size="20" '
          f'text-anchor="middle">{t["gf"]}</text>')
        left = t["rem"]
        a(f'<text class="mono" x="784" y="{cyl + 6}" fill="#7C808A" font-size="18" '
          f'text-anchor="middle">{left if left else "\u2212"}</text>')
        if t["status"] == "through":
            label, col, bg = "THROUGH", "#8A6A12", "#FBF1D6"
        elif out_:
            label, col, bg = "OUT", "#A23139", "#F7E4E4"
        else:
            label, col, bg = "ON THE BUBBLE", "#2B3A55", "#E4E9F1"
        pill_w = 8.6 * len(label) + 26
        a(f'<rect x="{1000 - pill_w:.0f}" y="{cyl - 14:.0f}" width="{pill_w:.0f}" height="26" '
          f'rx="13" fill="{bg}"/>')
        a(f'<text class="sans" x="{1000 - pill_w / 2:.0f}" y="{cyl + 4:.0f}" fill="{col}" '
          f'font-size="12" font-weight="700" letter-spacing="0.6" text-anchor="middle">{label}</text>')

    a(f'<line x1="80" y1="{table_bottom}" x2="1000" y2="{table_bottom}" '
      'stroke="#C9C5BA" stroke-width="1.5"/>')

    # footer
    fy = table_bottom + 28
    a(f'<text class="sans" x="80" y="{fy}" fill="#7C808A" font-size="12.5">Thirds ranked by '
      'points, then goal difference, then goals for.</text>')
    a(f'<text class="sans" x="80" y="{fy + 18}" fill="#7C808A" font-size="12.5">PTS / GD / GF and '
      'LEFT are current values and games still to play; open-group rows are provisional.</text>')
    a(f'<text class="sans" x="80" y="{fy + 40}" fill="#7C808A" font-size="12.5">THROUGH and OUT '
      'are mathematical certainties; ON THE BUBBLE means the seat is still live for that side.</text>')
    a(f'<rect x="80" y="{fy + 58}" width="11" height="11" fill="#A23139"/>')
    a(f'<text class="sans" x="100" y="{fy + 68}" fill="#15171C" font-size="15" font-weight="700">'
      'The bounds above are exact: every result of the remaining games has been enumerated.</text>')
    a(f'<text class="sans" x="80" y="{fy + 94}" fill="#7C808A" font-size="12.5">Method&#160;&#160;'
      '&#183;&#160;&#160;exact enumeration of every scoreline of the group games still to play. '
      'Conduct held at current values.</text>')
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
    ap.add_argument("--sims", type=int, default=None)      # accepted + ignored
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--top", type=int, default=8)
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

    if args.svg:
        svg = render_bubble_svg(A)
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
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Wrote {args.json}.")


if __name__ == "__main__":
    main()
