#!/usr/bin/env python3
"""
simulate_group_maxmin.py — EXACT third-place qualification bounds.

The Round of 32 takes the 12 group winners, the 12 runners-up, and the 8 BEST
third-placed teams. Those 8 are picked by ranking the twelve thirds on, in turn:
Points, Goal Difference, Goals For, Conduct score, then FIFA ranking. The 8th
best third is the last team in; everything from the 9th down is out.

This tool answers three questions EXACTLY, by enumerating every possible result
(win / draw / loss) of the group games still to be played — no Monte Carlo, no
Elos, no sampling:

  * the HIGHEST possible cut-off — the strongest the 8th-best third could be
    forced to be, i.e. the hardest the bar can get;
  * the LOWEST possible cut-off — the weakest line still good enough for that
    8th seat, i.e. the easiest the bar can get;
  * which teams are mathematically CLINCHED (through in EVERY remaining
    scenario) and which are mathematically ELIMINATED (through in NONE).

Goals are treated adversarially / unbounded, so the bars are reported in POINTS
only: a team tied on points with the bar is NOT safe, because an opponent can
always out-score it on goal difference. This is the "degenerate" (points-pure)
treatment — the only guarantees that hold are the ones points alone can give.

  * Highest bar  = the 8th-highest, across the 12 groups, of each group's MAX
    possible third-place points (each group pushed independently to its strongest
    possible third). All twelve maxima are simultaneously achievable because the
    groups' remaining games are independent, so this is an exact upper bound.
  * Lowest bar   = the same with each group's MIN possible third-place points.
  * Clinched(X)  = in every completion of X's own group, X is either guaranteed
    top-2, or is the 3rd-placed side on px points with at most 7 OTHER groups able
    to field a third on >= px points (ties counted against X, since GD is
    adversarial). Anything weaker is not a mathematical clinch.
  * Eliminated(X)= in no completion can X be top-2, nor a 3rd-placed side on px
    points with at most 7 other groups FORCED above px (ties in X's favour).

Outputs:
  --svg  bubble.svg          the news-desk graphic served at the site's /#bubble
                             view, now showing the exact bounds and where the
                             twelve current thirds stand against them.
  --knockout-json PATH       CLAMP an existing knockout_odds.json in place so it
                             carries the mathematical truth the page keys on:
                             odds = 1.0 only for clinched teams, 0.0 only for
                             eliminated teams, and every other team pinned into
                             [0.01, 0.99] (never a false 100% / 0%). Re-written
                             only if something changed.
  --json PATH                a small machine-readable summary of the bounds and
                             the clinched / eliminated sets.

  python3 simulate_group_maxmin.py --svg bubble.svg --knockout-json knockout_odds.json

--sims / --seed / --workers / --top are accepted and ignored (the computation is
exact, not sampled); they exist only so the existing pipeline invocation keeps
working unchanged.
"""

import argparse
import itertools
import json
import os
import sys
from datetime import datetime, timezone

# Run from anywhere: make sure the directory holding this file (where
# simulate_pool.py also lives) is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simulate_pool import load, build_model, rank_group  # noqa: E402


# ----------------------------------------------------------------------------
# State: per group, the played-game record, the remaining intra-group games,
# and the static team attributes (conduct, FIFA) the ranking needs.
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
            "names": names,
            "base": base,
            "played": played,
            "rem": [(m["h"], m["a"]) for m in (rem.get(letter) or [])],
            "fifa": {t: fifa.get(t) for t in names},
            "cond": {t: cond.get(t, 0) for t in names},
        }
    return out


def group_point_outcomes(g):
    """Every W/D/L completion of the group's remaining games -> {team: final points}.
    Points are exact under win/draw/loss; goals are not enumerated (adversarial)."""
    rem = g["rem"]
    base_pts = {t: g["base"][t]["pts"] for t in g["names"]}
    outs = []
    for combo in itertools.product(("H", "D", "A"), repeat=len(rem)):
        pts = dict(base_pts)
        for (h, a), r in zip(rem, combo):
            if r == "H":
                pts[h] += 3
            elif r == "A":
                pts[a] += 3
            else:
                pts[h] += 1
                pts[a] += 1
        outs.append(pts)
    return outs


def _third_points(pts_map):
    """The 3rd-highest point total among the four teams (the 3rd-place points)."""
    return sorted(pts_map.values(), reverse=True)[2]


def max_min_third_points(g):
    vals = [_third_points(p) for p in group_point_outcomes(g)]
    return max(vals), min(vals)


def current_standings(g):
    """Rank the group on games played so far (provisional). Returns rank_group order."""
    rows = [{
        "team": t,
        "points": g["base"][t]["pts"],
        "gd": g["base"][t]["gf"] - g["base"][t]["ga"],
        "gf": g["base"][t]["gf"],
        "ga": g["base"][t]["ga"],
        "p": sum(1 for m in g["played"] if t in (m["h"], m["a"])),
        "conduct": g["cond"].get(t, 0),
        "fifa": g["fifa"].get(t),
    } for t in g["names"]]
    return rank_group(rows, g["played"])


# ----------------------------------------------------------------------------
# The exact analysis.
# ----------------------------------------------------------------------------
def analyze(model):
    G = group_records(model)
    letters = list(G.keys())
    n_slots = 8  # best 8 thirds qualify

    maxT, minT = {}, {}
    for L in letters:
        mx, mn = max_min_third_points(G[L])
        maxT[L], minT[L] = mx, mn

    def nth_best(values):
        s = sorted(values, reverse=True)
        return s[n_slots - 1] if len(s) >= n_slots else (s[-1] if s else 0)

    highest_bar = nth_best(list(maxT.values()))
    lowest_bar = nth_best(list(minT.values()))

    clinched, eliminated = set(), set()
    for L in letters:
        g = G[L]
        names = g["names"]
        outs = group_point_outcomes(g)
        others = [o for o in letters if o != L]
        for X in names:
            # ---- CLINCHED: survive the WORST completion of X's own group ----
            cl = True
            for pts in outs:
                px = pts[X]
                n_gt = sum(1 for t in names if t != X and pts[t] > px)
                n_eq = sum(1 for t in names if t != X and pts[t] == px)
                worst_pos = n_gt + n_eq + 1          # X loses every goal tie
                if worst_pos <= 2:
                    continue                          # guaranteed top-2 here
                if worst_pos >= 4:
                    cl = False
                    break                             # could be 4th -> not clinched
                # worst_pos == 3: X is the third on px points. It survives iff at
                # most 7 OTHER groups can field a third on >= px (tie against X).
                if sum(1 for o in others if maxT[o] >= px) <= n_slots - 1:
                    continue
                cl = False
                break
            if cl:
                clinched.add(X)

            # ---- ELIMINATED: fail the BEST completion of X's own group ----
            elim = True
            for pts in outs:
                px = pts[X]
                n_gt = sum(1 for t in names if t != X and pts[t] > px)
                best_pos = n_gt + 1                   # X wins every goal tie
                if best_pos <= 2:
                    elim = False
                    break                             # can be top-2
                if best_pos == 3:
                    # X is the third on px points; it sneaks in iff at most 7 other
                    # groups are FORCED above px (their weakest third still > px).
                    if sum(1 for o in others if minT[o] > px) <= n_slots - 1:
                        elim = False
                        break
                # best_pos >= 4: cannot even be third in this completion.
            if elim:
                eliminated.add(X)

    # Where the twelve CURRENT third-placed teams stand (provisional ordering).
    thirds = []
    for L in letters:
        ranked = current_standings(G[L])
        if len(ranked) >= 3:
            t = ranked[2]
            thirds.append({
                "group": L, "team": t["team"], "points": t["points"],
                "gd": t["gd"], "gf": t["gf"], "rem": len(G[L]["rem"]),
            })
    thirds.sort(key=lambda r: (-r["points"], -r["gd"], -r["gf"], r["team"].lower()))

    return {
        "groups": letters,
        "max_third_pts": maxT,
        "min_third_pts": minT,
        "highest_bar": highest_bar,
        "lowest_bar": lowest_bar,
        "clinched": clinched,
        "eliminated": eliminated,
        "current_thirds": thirds,
        "games_remaining": sum(len(G[L]["rem"]) for L in letters),
    }


# ----------------------------------------------------------------------------
# knockout_odds.json clamp: make the file carry the mathematical truth.
#   odds = 1.0  iff clinched      (page draws the gold "through" line)
#   odds = 0.0  iff eliminated    (page greys / strikes the team)
#   else        pinned to [0.01, 0.99] so no team is a false 100% / 0%.
# Re-writes the file only if a value actually changed (keeps the pipeline's
# no-change push check meaningful).
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
        # Keep the momentum trend coherent: settled (1.0 / 0.0) teams show no
        # arrow; live teams keep an arrow relative to their stored base.
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
          f"({len(clinched)} clinched -> 1.0, {len(eliminated)} eliminated -> 0.0, "
          f"rest pinned to [0.01, 0.99]).")
    return True


# ----------------------------------------------------------------------------
# SVG graphic (served at /#bubble). Editorial news-desk styling; numbers in a
# monospaced "scoreboard" face. NO embedded timestamp (identical model output
# -> identical bytes, so the pipeline's no-change check still fires). NO em
# dashes in the copy (colons / commas instead); Unicode minus for negatives.
# ----------------------------------------------------------------------------
def _xesc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sgn(v):
    return f"+{v}" if v > 0 else (f"\u2212{-v}" if v < 0 else "0")


def render_bubble_svg(A):
    hi = A["highest_bar"]
    lo = A["lowest_bar"]
    clinched = A["clinched"]
    eliminated = A["eliminated"]
    thirds = A["current_thirds"]
    rem_games = A["games_remaining"]

    pts_word = lambda n: f"{n} point" + ("" if n == 1 else "s")

    out = []
    a = out.append
    W = 1080
    row_h = 46
    table_top = 792
    n_rows = len(thirds)
    table_bottom = table_top + 30 + n_rows * row_h
    H = table_bottom + 150

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
      'third-placed teams advance to the Round of 32. With group games still</text>')
    a('<text class="sans" x="80" y="316" fill="#4A4E57" font-size="20">to be played, the '
      'cut-off for that eighth and final seat is not yet fixed, but it can</text>')
    a('<text class="sans" x="80" y="346" fill="#4A4E57" font-size="20">only land between '
      'these two bounds, computed exactly over every possible result.</text>')
    a('<line x1="80" y1="384" x2="1000" y2="384" stroke="#E2DFD7" stroke-width="1"/>')

    # bounds heading
    a('<text class="sans" x="80" y="432" fill="#15171C" font-size="15" font-weight="700" '
      'letter-spacing="2.5">THE CUT-OFF, BOUNDED</text>')
    a('<rect x="80" y="442" width="46" height="3" fill="#15171C"/>')
    a('<text class="sans" x="80" y="467" fill="#4A4E57" font-size="15">Points only: with goal '
      'difference adversarial, a team level on points with the bar is not safe.</text>')

    # two bound cards
    cards = [
        ("LOWEST POSSIBLE", "the easiest the bar can get", "#2E6E66", lo,
         ["The weakest 8th-best third any", "sequence of results can produce."]),
        ("HIGHEST POSSIBLE", "the hardest the bar can get", "#A23139", hi,
         ["The strongest 8th-best third the", "results can be forced to require."]),
    ]
    cy = 502
    cardw = 440
    for i, (tier, sub, color, val, notes) in enumerate(cards):
        x = 80 + i * (cardw + 40)
        a(f'<rect x="{x}" y="{cy}" width="{cardw}" height="150" rx="4" fill="#FFFFFF" '
          'stroke="#E7E3DA" stroke-width="1"/>')
        a(f'<rect x="{x}" y="{cy}" width="6" height="150" fill="{color}"/>')
        a(f'<text class="sans" x="{x + 28}" y="{cy + 38}" fill="{color}" font-size="15" '
          f'font-weight="700" letter-spacing="1">{tier}</text>')
        a(f'<text class="sans" x="{x + 28}" y="{cy + 60}" fill="#7C808A" font-size="13">{sub}</text>')
        a(f'<text class="mono" x="{x + 28}" y="{cy + 120}" fill="#15171C" font-size="68" '
          f'font-weight="700">{val}</text>')
        a(f'<text class="sans" x="{x + 150}" y="{cy + 104}" fill="#15171C" font-size="20">'
          f'{"point" if val == 1 else "points"}</text>')
        a(f'<text class="sans" x="{x + 150}" y="{cy + 126}" fill="#7C808A" font-size="12.5">{_xesc(notes[0])}</text>')
        a(f'<text class="sans" x="{x + 150}" y="{cy + 142}" fill="#7C808A" font-size="12.5">{_xesc(notes[1])}</text>')

    # tally strip
    a(f'<text class="sans" x="80" y="712" fill="#4A4E57" font-size="15">'
      f'<tspan fill="#15171C" font-weight="700">{len(clinched)}</tspan> teams are already '
      f'mathematically through&#160;&#160;&#183;&#160;&#160;'
      f'<tspan fill="#15171C" font-weight="700">{len(eliminated)}</tspan> are out&#160;&#160;&#183;&#160;&#160;'
      f'<tspan fill="#15171C" font-weight="700">{rem_games}</tspan> group games still to play.</text>')
    a('<line x1="80" y1="740" x2="1000" y2="740" stroke="#E2DFD7" stroke-width="1"/>')

    # thirds table
    a('<text class="sans" x="80" y="772" fill="#15171C" font-size="15" font-weight="700" '
      'letter-spacing="2.5">WHERE THE TWELVE THIRDS STAND</text>')
    a('<rect x="80" y="782" width="46" height="3" fill="#15171C"/>')
    # column headers
    hy = table_top + 22
    a(f'<text class="sans" x="120" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5">TEAM</text>')
    a(f'<text class="sans" x="560" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5" text-anchor="middle">GRP</text>')
    a(f'<text class="sans" x="648" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5" text-anchor="middle">PTS</text>')
    a(f'<text class="sans" x="744" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5" text-anchor="middle">LEFT</text>')
    a(f'<text class="sans" x="1000" y="{hy}" fill="#7C808A" font-size="12" font-weight="700" '
      'letter-spacing="1.5" text-anchor="end">STATUS</text>')
    a(f'<line x1="80" y1="{table_top + 30}" x2="1000" y2="{table_top + 30}" '
      'stroke="#C9C5BA" stroke-width="1.5"/>')

    for i, t in enumerate(thirds):
        top = table_top + 30 + i * row_h
        cyl = top + row_h / 2
        if i % 2 == 1:
            a(f'<rect x="80" y="{top}" width="920" height="{row_h}" fill="#F4F2EC"/>')
        nm = t["team"]
        struck = nm in eliminated
        through = nm in clinched
        # team name
        name_fill = "#9A9EA6" if struck else "#15171C"
        a(f'<text class="serif" x="120" y="{cyl + 7}" fill="{name_fill}" font-size="21">'
          f'{_xesc(nm)}</text>')
        if struck:
            tw = 11 * len(nm) * 0.62
            a(f'<line x1="118" y1="{cyl + 1}" x2="{120 + tw:.0f}" y2="{cyl + 1}" '
              'stroke="#A23139" stroke-width="2"/>')
        a(f'<text class="mono" x="560" y="{cyl + 6}" fill="#4A4E57" font-size="18" '
          f'text-anchor="middle">{t["group"]}</text>')
        a(f'<text class="mono" x="648" y="{cyl + 6}" fill="#15171C" font-size="20" '
          f'font-weight="700" text-anchor="middle">{t["points"]}</text>')
        left = t["rem"]
        a(f'<text class="mono" x="744" y="{cyl + 6}" fill="#7C808A" font-size="18" '
          f'text-anchor="middle">{left if left else "\u2212"}</text>')
        # status pill
        if through:
            label, col, bg = "THROUGH", "#8A6A12", "#FBF1D6"
        elif struck:
            label, col, bg = "OUT", "#A23139", "#F7E4E4"
        else:
            label, col, bg = "IN THE HUNT", "#4A4E57", "#ECEAE3"
        pill_w = 9.2 * len(label) + 28
        a(f'<rect x="{1000 - pill_w:.0f}" y="{cyl - 14:.0f}" width="{pill_w:.0f}" height="26" '
          f'rx="13" fill="{bg}"/>')
        a(f'<text class="sans" x="{1000 - pill_w / 2:.0f}" y="{cyl + 4:.0f}" fill="{col}" '
          f'font-size="12.5" font-weight="700" letter-spacing="0.8" text-anchor="middle">{label}</text>')

    a(f'<line x1="80" y1="{table_bottom}" x2="1000" y2="{table_bottom}" '
      'stroke="#C9C5BA" stroke-width="1.5"/>')

    # footer
    fy = table_bottom + 28
    a(f'<text class="sans" x="80" y="{fy}" fill="#7C808A" font-size="12.5">Thirds are ranked by '
      'points, goal difference, goals scored, conduct score, then FIFA ranking. PTS and LEFT are '
      'current points and group games left.</text>')
    a(f'<text class="sans" x="80" y="{fy + 22}" fill="#7C808A" font-size="12.5">'
      'THROUGH and OUT are mathematical certainties: a side is THROUGH only if it qualifies in '
      'every remaining result, OUT only if it qualifies in none.</text>')
    a(f'<rect x="80" y="{fy + 46}" width="11" height="11" fill="#A23139"/>')
    a(f'<text class="sans" x="100" y="{fy + 56}" fill="#15171C" font-size="15" font-weight="700">'
      'Provisional: the standings above shift as the remaining group fixtures are played.</text>')
    a(f'<text class="sans" x="80" y="{fy + 82}" fill="#7C808A" font-size="12.5">Method&#160;&#160;'
      '&#183;&#160;&#160;exact enumeration of every win / draw / loss outcome of the group games '
      'still to play. Goals are unbounded, so bounds are in points.</text>')
    a('</svg>')
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Exact highest/lowest third-place qualification bounds (8th best third).")
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--elo", default="elo.json")
    ap.add_argument("--svg", default=None, help="write the bubble.svg graphic to this path.")
    ap.add_argument("--knockout-json", default=None,
                    help="clamp this knockout_odds.json in place to the math "
                         "(1.0 clinched, 0.0 eliminated, else [0.01, 0.99]).")
    ap.add_argument("--json", default=None, help="write a JSON summary of the bounds/sets.")
    # Accepted and ignored (computation is exact, not sampled): keeps the
    # existing pipeline invocation working unchanged.
    ap.add_argument("--sims", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    data = load(args.data)
    try:
        elo = load(args.elo)
    except Exception:
        elo = {}                      # bounds need only points; Elos are irrelevant
    model = build_model(elo, data)
    A = analyze(model)

    print(f"Highest possible cut-off: {A['highest_bar']} pts")
    print(f"Lowest  possible cut-off: {A['lowest_bar']} pts")
    print(f"Clinched ({len(A['clinched'])}): {', '.join(sorted(A['clinched']))}")
    print(f"Eliminated ({len(A['eliminated'])}): {', '.join(sorted(A['eliminated']))}")

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
            "highest_bar_points": A["highest_bar"],
            "lowest_bar_points": A["lowest_bar"],
            "max_third_points": A["max_third_pts"],
            "min_third_points": A["min_third_pts"],
            "clinched": sorted(A["clinched"]),
            "eliminated": sorted(A["eliminated"]),
            "current_thirds": A["current_thirds"],
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Wrote {args.json}.")


if __name__ == "__main__":
    main()
