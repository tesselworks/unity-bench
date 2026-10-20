#!/usr/bin/env python3
"""Regenerate the figures of the README and docs/ from docs/figures/data/*.json.

    python3 docs/figures/make_figures.py

Standard library only. Every figure is written twice, `<name>-light.svg` and `<name>-dark.svg`
(same drawing, two palettes); the Markdown picks one with `<picture>` and `prefers-color-scheme`.
The data files were extracted from real run directories (raw runs stay out of git); each says
where its numbers come from. Flow and sequence diagrams are Mermaid blocks in the Markdown itself.

The script estimates text widths and prints a warning for any text that would run outside its
figure, so check its output after changing a label.
"""
import json
import math
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PAD = 24

# One accent colour; everything else is ink, muted text, slate bars and hairlines.
THEMES = {
    "light": {
        "card": "#F6F8FA", "node": "#FFFFFF", "soft": "#EAEEF2", "bar": "#57606A", "accent": "#7B4FE8",
        "tint": "#F1ECFF", "ink": "#1F2328", "muted": "#59636E", "accent-text": "#5532A8",
        "on-accent": "#FFFFFF", "hairline": "#D1D9E0", "grid": "#E4E8EC", "flow": "#818B98",
    },
    "dark": {
        "card": "#151B23", "node": "#0D1117", "soft": "#262C36", "bar": "#8B949E", "accent": "#A68BFA",
        "tint": "#251E3D", "ink": "#E6EDF3", "muted": "#9198A1", "accent-text": "#C9B8FF",
        "on-accent": "#0D1117", "hairline": "#3D444D", "grid": "#262C36", "flow": "#768390",
    },
}
FILLS = ("card", "node", "soft", "bar", "accent", "tint")
TEXTS = ("ink", "muted", "accent-text", "on-accent")
STROKES = ("hairline", "grid", "flow", "accent", "bar")

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
SIZES = {"title": (17, 600), "sub": (12.5, 400), "label": (13, 400), "strong": (13, 600), "small": (12, 400),
         "tiny": (11, 400), "big": (20, 600)}

_NARROW = set("iljtfr.,:;|!()[]' ")
_WIDE = set("mwMW@%")


def text_width(s, size, weight=400, mono=False):
    """Rough advance width of a string in a system sans or monospace font (px)."""
    if mono:
        return len(s) * size * 0.61
    w = 0.0
    for ch in s:
        if ch in _NARROW:
            w += 0.30
        elif ch in _WIDE:
            w += 0.86
        elif ch.isupper():
            w += 0.67
        elif ch.isdigit():
            w += 0.56
        else:
            w += 0.53
    return w * size * (1.06 if weight >= 600 else 1.0)


def wrap(s, width, style="small", mono=False):
    size, weight = SIZES[style]
    lines, cur = [], ""
    for word in s.split(" "):
        trial = (cur + " " + word).strip()
        if cur and text_width(trial, size, weight, mono) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


class Fig:
    def __init__(self, name, width, height, title, sub=()):
        self.name, self.w, self.h = name, width, height
        self.title = title
        self.items = []
        self.warnings = []
        self.text(PAD, 34, title, "title")
        for i, line in enumerate([sub] if isinstance(sub, str) else sub):
            self.text(PAD, 56 + i * 18, line, "sub", fill="muted")
        self.top = 56 + 18 * len([sub] if isinstance(sub, str) else sub) + 10

    # drawing primitives -------------------------------------------------------------------
    def rect(self, x, y, w, h, fill="node", rx=6, stroke=None, sw=1, dash=None):
        a = f' class="f-{fill}' + (f' s-{stroke}"' if stroke else '"')
        st = f' stroke-width="{sw}"' if stroke else ""
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{max(h, 0):.1f}" rx="{rx}"{a}{st}{da}/>')

    def line(self, x1, y1, x2, y2, stroke="hairline", sw=1, dash=None):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" class="s-{stroke}" stroke-width="{sw}"{da}/>')

    def poly(self, pts, stroke="flow", sw=2, dash=None, fill="none"):
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(f'<polyline points="{d}" fill="{fill}" class="s-{stroke}" stroke-width="{sw}" stroke-linejoin="round"{da}/>')

    def circle(self, x, y, r, fill="accent", stroke=None, sw=1.5):
        if fill == "none":
            self.items.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="none" class="s-{stroke}" stroke-width="{sw}"/>')
        else:
            s = f' s-{stroke}" stroke-width="{sw}"' if stroke else '"'
            self.items.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" class="f-{fill}{s}/>')

    def text(self, x, y, s, style="label", anchor="start", fill=None, mono=False):
        size, weight = SIZES[style]
        fill = fill or ("muted" if style in ("small", "tiny", "sub") else "ink")
        w = text_width(s, size, weight, mono)
        x0 = x - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
        if x0 < 2 or x0 + w > self.w - 2:
            self.warnings.append(f"{self.name}: text may be clipped: {s!r} ({x0:.0f}..{x0 + w:.0f} of {self.w})")
        fam = f' style="font-family:{MONO}"' if mono else ""
        wt = f' font-weight="{weight}"' if weight != 400 else ""
        self.items.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}"{wt} text-anchor="{anchor}" class="t-{fill}"{fam}>{escape(s)}</text>')
        return w

    def lines(self, x, y, lines, style="small", step=16, fill=None, mono=False, anchor="start"):
        for i, s in enumerate(lines):
            self.text(x, y + i * step, s, style, anchor, fill, mono)
        return y + (len(lines) - 1) * step

    # output -------------------------------------------------------------------------------
    def svg(self, theme):
        p = THEMES[theme]
        css = [f"text{{font-family:{SANS}}}"]
        css += [f".f-{k}{{fill:{p[k]}}}" for k in FILLS]
        css += [f".t-{k}{{fill:{p[k]}}}" for k in TEXTS]
        css += [f".s-{k}{{stroke:{p[k]}}}" for k in STROKES]
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" '
                f'role="img" aria-label="{escape(self.title)}">\n<title>{escape(self.title)}</title>\n'
                f'<style>{"".join(css)}</style>\n'
                f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="12" class="f-card s-hairline" stroke-width="1"/>\n'
                + "\n".join(self.items) + "\n</svg>\n")

    def save(self):
        for theme in THEMES:
            (HERE / f"{self.name}-{theme}.svg").write_text(self.svg(theme), encoding="utf-8")
        for w in self.warnings:
            print("warning:", w)
        print(f"wrote {self.name}-light.svg, {self.name}-dark.svg ({self.w}x{self.h})")


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def fmt_ms(v):
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:.0f}"
    if v >= 10:
        return f"{v:.1f}"
    return f"{v:.3f}"


# ---------------------------------------------------------------------------------------------
# Ratio bars on a log axis (README sample report, case studies)

def ratio_chart(name, title, sub, rows, series, ticks, lo, hi, width=820, label_w=250, right_w=96,
                row_gap=10, bar_h=16, note=None, right_head=None):
    """rows: [{label, ratios: [r or None per series], right: str or None, dim: bool}]"""
    nser = len(series)
    row_h = nser * bar_h + (nser - 1) * 3 + row_gap * 2
    legend_h = 26 if nser > 1 else 0
    tmp = Fig(name, width, 10, title, sub)
    top = tmp.top + legend_h + 8
    axis_h = len(rows) * row_h
    note_lines = wrap(note, width - 2 * PAD, "small") if note else []
    height = top + axis_h + 34 + len(note_lines) * 16 + (12 if note_lines else 0) + 8
    f = Fig(name, width, height, title, sub)
    x0 = PAD + label_w + 16
    x1 = width - PAD - right_w
    k = (x1 - x0) / math.log(hi / lo)
    X = lambda r: x0 + k * math.log(r / lo)
    if nser > 1:
        lx = x0
        for sname, cls in series:
            f.rect(lx, f.top - 2, 14, 10, cls, rx=2)
            lx += 20 + f.text(lx + 20, f.top + 7, sname, "small") + 24
    bottom = top + axis_h
    for t in ticks:
        f.line(X(t), top - 6, X(t), bottom, "flow" if t == 1 else "grid", 1.2 if t == 1 else 1)
        f.text(X(t), bottom + 16, f"{t:g}×", "small", "middle", fill="ink" if t == 1 else "muted")
    if right_head:
        f.text(width - PAD, top - 10, right_head, "tiny", "end")
    for i, row in enumerate(rows):
        y = top + i * row_h + row_gap
        mid = y + (nser * bar_h + (nser - 1) * 3) / 2
        f.text(x0 - 16, mid + 4.5, row["label"], "label", "end", fill="muted" if row.get("dim") else "ink")
        for j, (r, (sname, cls)) in enumerate(zip(row["ratios"], series)):
            by = y + j * (bar_h + 3)
            if r is None:
                f.text(X(1) + 6, by + bar_h - 4, "not measured", "tiny")
                continue
            a, b = sorted((X(1), X(r)))
            f.rect(a, by, max(b - a, 2), bar_h, cls, rx=3)
            lab = f"{r:.2f}"
            if r < 1:
                f.text(a - 5, by + bar_h - 4, lab, "small", "end", fill="ink")
            else:
                f.text(b + 5, by + bar_h - 4, lab, "small", "start", fill="ink")
        if row.get("right"):
            f.text(width - PAD, mid + 4.5, row["right"], "small", "end", fill="muted")
    if note_lines:
        f.lines(PAD, bottom + 40, note_lines, "small")
    return f


def fig_sample_ratios():
    d = load("sample-report.json")
    rows = []
    for r in d["rows"]:
        n = f"n={r['n'][0]} vs {r['n'][1]}"
        rows.append({"label": r["label"], "ratios": [r["ratio"]], "right": n, "dim": r["n"] == [1, 1]})
    f = ratio_chart(
        "sample-ratios", "Sample report: Unity 7 / Unity 6.7",
        ["Ratio of medians across processes, log axis. Left of 1×: Unity 7 took less time.",
         "One run of examples/minimal.toml, 6000.7.0b2 (Mono) vs 7000.0.0a9 (CoreCLR), on a busy Apple M4 Max."],
        rows, [("Unity 7 / Unity 6.7", "accent")], [0.05, 0.1, 0.25, 0.5, 1, 2], 0.04, 2.6,
        label_w=236, right_w=78, right_head="processes",
        note="Every row: ranges separate. With no real difference that happens 33% of the time for 2 vs 2 "
             "and always for 1 vs 1 (the greyed row). This shows the report's shape, not a benchmark result.")
    f.save()


def fig_case_editor():
    d = load("case-matterkit.json")
    rows = [{"label": r["label"], "ratios": [r["ratio"]]} for r in d["editor"]]
    f = ratio_chart(
        "case-matterkit-editor", "MatterKit in the Editor: Unity 7 / Unity 6.7",
        ["7000.0.0a9 (CoreCLR) vs 6000.7.0b2 (Mono). Ratio of medians, log axis. Left of 1×: Unity 7 took less time.",
         "Windows 11, Ryzen 9 7900X3D; 3 to 4 fresh processes per Editor."],
        rows, [("Unity 7 / Unity 6.7", "accent")], [0.25, 0.5, 1, 2, 4], 0.25, 4.0, label_w=236, right_w=40)
    f.save()


def fig_case_player():
    d = load("case-matterkit.json")["player"]
    rows = [{"label": r["label"], "ratios": r["ratios"]} for r in d["rows"]]
    f = ratio_chart(
        "case-matterkit-player", "MatterKit players: time relative to Unity 6.7 IL2CPP",
        ["Release players, same build settings. Ratio of medians, log axis. Left of 1×: less time than 6.7 IL2CPP.",
         "Windows 11, Ryzen 9 7900X3D; 3 to 4 fresh processes per player (the 4-bottle row: Mac, 7000.0.0a7)."],
        rows, [("Unity 7 IL2CPP", "bar"), ("Unity 7 CoreCLR", "accent")], [0.25, 0.5, 1, 2, 4], 0.18, 4.0,
        label_w=256, right_w=40, bar_h=13, row_gap=9)
    f.save()


# ---------------------------------------------------------------------------------------------
# Leak control (#18): survivors and resident memory per reload

def fig_leak_control():
    d = load("leak-control.json")["variants"]
    a7, a9, mono = d["unity-7-a7"], d["unity-7-a9"], d["unity-6.7"]
    W, H = 860, 400
    f = Fig("leak-control", W, H, "Leak control (#18): one Editor, ten script reloads",
            ["editor.reload_leak on the example project, read after forced collections; median of 2 processes per Editor.",
             "macOS, Apple M4 Max, 2026-10-19. 7000.0.0a7 leaks every reload; 7000.0.0a9 has the fix."])
    gap = 56
    pw = (W - 2 * PAD - gap) / 2
    top, bottom = f.top + 30, H - 54

    def panel(px, title, ymax, yticks, yfmt):
        left = px + 44
        right = px + pw - 6
        f.text(px, f.top + 8, title, "strong")
        for v in yticks:
            y = bottom - (bottom - top) * v / ymax
            f.line(left, y, right, y, "grid", 1)
            f.text(left - 8, y + 4, yfmt(v), "small", "end")
        for c in range(1, 11):
            x = left + (right - left) * (c - 1) / 9
            if c in (1, 5, 10):
                f.text(x, bottom + 17, str(c), "small", "middle")
        f.text((left + right) / 2, bottom + 35, "reload", "small", "middle")
        sx = lambda c: left + (right - left) * (c - 1) / 9
        sy = lambda v: bottom - (bottom - top) * v / ymax
        return sx, sy, left, right

    # left: unloaded contexts still alive
    sx, sy, left, right = panel(PAD, "Unloaded contexts still alive", 10, [0, 2, 4, 6, 8, 10], lambda v: f"{v:g}")
    pts7 = [(sx(i + 1), sy(v)) for i, v in enumerate(a7["survivors"]["median"])]
    pts9 = [(sx(i + 1), sy(v)) for i, v in enumerate(a9["survivors"]["median"])]
    f.poly(pts9, "bar", 2)
    f.poly(pts7, "accent", 2.5)
    for x, y in pts9:
        f.circle(x, y, 3, "bar")
    for x, y in pts7:
        f.circle(x, y, 3.2, "accent")
    f.text(sx(1) + 2, sy(9.2), "7000.0.0a7: 1, 2, ..., 10", "strong", fill="accent-text")
    f.text(sx(1) + 2, sy(9.2) + 17, "every old generation stays", "small")
    f.text(sx(10), sy(0) - 10, "7000.0.0a9: 0 at every reading", "small", "end", fill="ink")
    f.text(sx(10), sy(3.2), "6000.7.0b2 (Mono): not counted,", "tiny", "end")
    f.text(sx(10), sy(3.2) + 14, "no collectible contexts", "tiny", "end")

    # right: App Resident Memory
    px = PAD + pw + gap
    sx, sy, left, right = panel(px, "App Resident Memory (MB)", 2000, [0, 500, 1000, 1500, 2000], lambda v: f"{v:,}")
    for v, cls, sw, dash in ((mono, "bar", 2, "5 4"), (a9, "bar", 2, None), (a7, "accent", 2.5, None)):
        f.poly([(sx(i + 1), sy(m)) for i, m in enumerate(v["resident_mb"]["median"])], cls, sw, dash)
    f.text(sx(1) + 2, sy(1880), f"7000.0.0a7: {a7['resident_slope_mb']:+.0f} MB per reload", "strong", fill="accent-text")
    lo7, hi7 = a7["resident_mb"]["median"][0], a7["resident_mb"]["median"][-1]
    f.text(sx(1) + 2, sy(1880) + 17, f"{lo7:,.0f} MB to {hi7:,.0f} MB", "small")
    f.text(sx(10), sy(a9["resident_mb"]["median"][-1]) - 9,
           f"7000.0.0a9: {a9['resident_slope_mb']:+.1f} MB per reload", "small", "end", fill="ink")
    f.text(sx(10), sy(mono["resident_mb"]["median"][-1]) + 19,
           f"6000.7.0b2 (Mono, dashed): {mono['resident_slope_mb']:+.1f} MB per reload", "small", "end", fill="ink")
    f.save()


# ---------------------------------------------------------------------------------------------
# Annotate, don't refuse: one report row with callouts

def fig_annotated_row():
    d = load("annotated-row.json")["row"]
    W = 860
    f = Fig("annotated-row", W, 10, "Annotate, don't refuse: one row of a report",
            "The row is from the sample run (Editor adapter, xorshift). Nothing here stopped or discarded a measurement.")
    a, b = d["a"], d["b"]
    cells = [
        ("Item", "xorshift", 100),
        ("unity-6.7 (A)", f"{a['median']:.3f} [{a['min']:.3f}-{a['max']:.3f}] n={a['n']}", 196),
        ("unity-7", f"{b['median']:.3f} [{b['min']:.3f}-{b['max']:.3f}] n={b['n']}", 196),
        ("unity-7 / unity-6.7", f"{d['ratio']:.2f} (ranges separate, n={d['n'][0]} vs {d['n'][1]})", 246),
        ("Work", d["work"], W - 2 * PAD - 100 - 196 - 196 - 246),
    ]
    # host annotation above the variant columns
    host_top = f.top + 4
    xa = PAD + cells[0][2]
    xb = xa + cells[1][2] + cells[2][2]
    oa, ob = d["other_cpu_median"]["a"], d["other_cpu_median"]["b"]
    f.rect(xa, host_top, xb - xa, 42, "tint", rx=8)
    f.text(xa + 12, host_top + 17, "Host load is recorded, not judged", "strong", fill="accent-text")
    f.text(xa + 12, host_top + 34, f"other CPU median {oa:.0f}% (6.7), {ob:.0f}% (7): machine minus the launched tree", "small", fill="ink")
    f.line((xa + xb) / 2, host_top + 42, (xa + xb) / 2, host_top + 56, "accent", 1.5)
    # the table
    ty = host_top + 56
    f.rect(PAD, ty, W - 2 * PAD, 64, "node", rx=8, stroke="hairline")
    f.line(PAD, ty + 30, W - PAD, ty + 30, "hairline")
    x = PAD
    centers = []
    for i, (head, val, w) in enumerate(cells):
        if i:
            f.line(x, ty, x, ty + 64, "hairline")
        f.text(x + 10, ty + 20, head, "strong")
        f.text(x + 10, ty + 51, val, "label", mono=i in (1, 2), fill="accent-text" if i == 4 else "ink")
        centers.append(x + w / 2)
        x += w
    # callouts
    cy = ty + 64 + 44
    cw = (W - 2 * PAD - 2 * 16) / 3
    fa, fb = d["first_use_ms"]["a"], d["first_use_ms"]["b"]
    cov = d["coverage"]
    callouts = [
        ("First use is set apart",
         f"Round 0 of each process (first call, JIT) took {fa[0]:.2f} and {fa[1]:.2f} ms on 6.7, "
         f"{fb[0]:.2f} and {fb[1]:.2f} ms on 7. Kept in results.jsonl and in the first-use table, left out of these medians.",
         [centers[1], centers[2]]),
        ("Ranges separate, with the odds",
         f"Every Unity 7 process was faster than every 6.7 process. With no real difference that happens "
         f"{d['chance_separated'] * 100:.0f}% of the time for 2 vs 2; the report prints it under every table.",
         [centers[3]]),
        ("Work identity: same",
         f"Hash and counters agreed in {cov['unity-6.7'][0]} of {cov['unity-6.7'][1]} samples on 6.7 and "
         f"{cov['unity-7'][0]} of {cov['unity-7'][1]} on 7. One side silent reads partial; a mismatch takes the row out of the ratios.",
         [centers[4]]),
    ]
    body = [wrap(t, cw - 24, "small") for _, t, _ in callouts]
    ch = 34 + max(len(b) for b in body) * 16 + 8
    for i, ((head, _, targets), lines) in enumerate(zip(callouts, body)):
        cx = PAD + i * (cw + 16)
        f.rect(cx, cy, cw, ch, "node", rx=8, stroke="hairline")
        f.rect(cx, cy, 4, ch, "accent", rx=2)
        f.text(cx + 14, cy + 22, head, "strong")
        f.lines(cx + 14, cy + 42, lines, "small", fill="ink")
        mx = cx + cw / 2
        for t in targets:
            mid = ty + 64 + 20
            f.poly([(t, ty + 64), (t, mid), (mx, mid), (mx, cy)], "accent", 1.5)
            f.circle(t, ty + 64, 3, "accent")
    f.h = cy + ch + PAD
    f.save()


# ---------------------------------------------------------------------------------------------
# Workloads grid

def fig_workloads():
    W = 860
    f = Fig("workloads", W, 10, "Workloads: where they run and how much code they need",
            "Every Editor workload and the scene workloads also record a memory breakdown. Keys: docs/workloads.md.")
    lab_w = 92
    gap = 10
    cw = (W - 2 * PAD - lab_w - 3 * gap) / 3
    heads = [("No code", "built-in, any project"), ("Hooks in the game", "a scene workload plus Bench calls"),
             ("C# adapter", "your entry point, your stopwatch")]
    # (mono lines, muted lines)
    cells = {
        "Editor": [
            (["editor.cold_open", "editor.warm_open", "editor.script_edit", "editor.play",
              "editor.reload_leak", "editor.play_leak", "editor.scene_play"], []),
            (["editor.scene_play"], ["plus, in game code:"], ["Bench.Mark", "Bench.Hash", "Bench.Begin / End"]),
            (["editor.adapter"], ["a static method that takes"], ["AdapterContext:", "Measure, Add,", "AddDistribution"]),
        ],
        "Player": [
            (["player.build_time", "player.startup", "player.scene"], []),
            (["player.scene"], ["the same calls, compiled", "into the built player"]),
            (["player.adapter"], ["Run(PlayerContext), may be an", "IEnumerator over many frames;", "RecordFrames for frame times"]),
        ],
    }
    y = f.top + 4
    x0 = PAD + lab_w + gap
    for i, (h, s) in enumerate(heads):
        x = x0 + i * (cw + gap)
        f.text(x + 12, y + 16, h, "strong")
        f.text(x + 12, y + 33, s, "small")
    y += 46
    for row, cs in cells.items():
        def n_lines(c):
            return sum(len(part) for part in c)
        rh = 22 + max(n_lines(c) for c in cs) * 18 + 4
        f.rect(PAD, y, lab_w, rh, "soft", rx=8)
        f.text(PAD + lab_w / 2, y + rh / 2 + 5, row, "strong", "middle")
        for i, c in enumerate(cs):
            x = x0 + i * (cw + gap)
            f.rect(x, y, cw, rh, "node", rx=8, stroke="hairline")
            ly = y + 24
            for j, part in enumerate(c):
                mono = j != 1
                for s in part:
                    f.text(x + 12, ly, s, "small", fill="ink" if mono else "muted", mono=mono)
                    ly += 18
        y += rh + gap
    # work identity band
    f.text(PAD + lab_w / 2, y + 17, "Work", "strong", "middle")
    f.text(PAD + lab_w / 2, y + 33, "in the report", "tiny", "middle")
    bands = [("work not reported", "(scene: work not verified)", "soft", "muted"),
             ("same / differs / partial", "from Mark and Hash", "tint", "accent-text"),
             ("same / differs / partial", "from each sample's hash and counters", "tint", "accent-text")]
    for i, (a, b, fill, ink) in enumerate(bands):
        x = x0 + i * (cw + gap)
        f.rect(x, y, cw, 44, fill, rx=8)
        f.text(x + 12, y + 18, a, "strong", fill=ink)
        f.text(x + 12, y + 35, b, "small")
    f.h = y + 44 + PAD
    f.save()


# ---------------------------------------------------------------------------------------------
# Methodology: schedule, and the chance of separated ranges

def fig_schedule():
    W, H = 860, 440
    f = Fig("schedule", W, H, "Schedule: balanced order, fresh processes, medians of medians",
            ["Two variants, four repeats. Each box is one fresh Editor or player process with its own timeout; ticks are its samples.",
             "Illustrative values. The hollow tick is the first-use sample: kept in the raw data, left out of the median."])
    order = "ABBAABBA"
    med = {"A": [10.3, 10.6, 10.0, 10.4], "B": [8.2, 8.6, 7.9, 8.3]}
    bw, bg = 82, 10
    x0 = PAD + 70
    y0 = f.top + 20
    bh = 64
    idx = {"A": 0, "B": 0}
    dots = []
    f.text(PAD, y0 + bh / 2 + 5, "processes", "small")
    for i, v in enumerate(order):
        x = x0 + i * (bw + bg)
        cls = "bar" if v == "A" else "accent"
        f.rect(x, y0, bw, bh, "node", rx=8, stroke=cls, sw=1.5)
        f.text(x + 10, y0 + 18, v, "strong", fill="ink" if v == "A" else "accent-text")
        m = med[v][idx[v]]
        idx[v] += 1
        vals = [m + 2.6, m - 0.25, m + 0.3, m - 0.1]
        for j, s in enumerate(vals):
            tx = x + 34 + j * 12
            hgt = 6 + (s - 7) * 3
            if j == 0:
                f.rect(tx - 3, y0 + bh - 10 - hgt, 6, hgt, "node", rx=1.5, stroke=cls, sw=1.2)
            else:
                f.rect(tx - 3, y0 + bh - 10 - hgt, 6, hgt, cls, rx=1.5)
        if i % 2 == 0:
            f.text(x + bw + bg / 2, y0 - 8, f"repeat {i // 2 + 1}: {order[i]}{order[i + 1]}", "tiny", "middle")
        dots.append((x + bw / 2, v, m))
    f.line(x0, y0 + bh + 14, x0 + 8 * (bw + bg) - bg, y0 + bh + 14, "flow", 1)
    f.text(x0 + 8 * (bw + bg) - bg, y0 + bh + 30, "time", "tiny", "end")
    # per-process medians
    my = y0 + bh + 52
    f.text(PAD, my + 4, "median of", "small")
    f.text(PAD, my + 19, "each process", "small")
    for x, v, m in dots:
        f.line(x, y0 + bh, x, my - 6, "hairline", 1)
        f.circle(x, my, 5, "bar" if v == "A" else "accent")
    # value axis: gather the process medians per variant
    ay = my + 74
    left, right = x0 + 40, x0 + 8 * (bw + bg) - bg - 230
    lo, hi = 7.5, 11.0
    sx = lambda v: left + (right - left) * (v - lo) / (hi - lo)
    f.text(PAD, ay - 14, "per variant:", "small")
    f.text(PAD, ay + 1, "median and", "small")
    f.text(PAD, ay + 16, "min-max range", "small")
    for k, (v, cls) in enumerate((("B", "accent"), ("A", "bar"))):
        y = ay - 14 + k * 32
        vals = sorted(med[v])
        f.rect(sx(vals[0]), y - 4, sx(vals[-1]) - sx(vals[0]), 8, "soft", rx=4)
        for m in vals:
            f.circle(sx(m), y, 4.5, cls)
        mm = (vals[1] + vals[2]) / 2
        f.line(sx(mm), y - 11, sx(mm), y + 11, cls, 2.5)
        f.text(sx(vals[0]) - 12, y + 4, v, "strong", "end", fill="ink" if v == "A" else "accent-text")
    f.line(left, ay + 34, right, ay + 34, "flow", 1)
    f.text(right, ay + 50, "time per sample, faster to the left", "tiny", "end")
    # result
    rx = right + 30
    f.rect(rx, ay - 34, W - PAD - rx, 76, "tint", rx=8)
    f.text(rx + 12, ay - 13, "ratio B / A = 0.80", "strong", fill="accent-text")
    f.text(rx + 12, ay + 5, "ranges separate, n=4 vs 4", "small", fill="ink")
    f.text(rx + 12, ay + 22, "by chance with no difference: 2.9%", "small", fill="ink")
    # more variants
    f.lines(PAD, H - 40, wrap("More than two variants: each rotation, then its reverse (ABC CBA, BCA ACB, CAB BAC), so every "
                              "pair runs in both orders equally often; repeats are raised to an even number.", W - 2 * PAD), "small")
    f.save()


def fig_chance():
    W, H = 720, 300
    f = Fig("chance-separate", W, H, "How often ranges separate when nothing differs",
            ["Two groups of n processes drawn from the same distribution: chance that one group's range lies",
             "entirely below the other's (either direction) = 2 / C(2n, n). The report prints it under each table."])
    left, right = PAD + 44, W - PAD
    top, bottom = f.top + 14, H - 46
    for v in (0, 25, 50, 75, 100):
        y = bottom - (bottom - top) * v / 100
        f.line(left, y, right, y, "grid", 1)
        f.text(left - 8, y + 4, f"{v}%", "small", "end")
    n_max = 6
    slot = (right - left) / n_max
    for n in range(1, n_max + 1):
        p = 2 / math.comb(2 * n, n) * 100
        x = left + (n - 1) * slot + slot * 0.22
        w = slot * 0.56
        y = bottom - (bottom - top) * p / 100
        f.rect(x, y, w, bottom - y, "accent" if n == 2 else "bar", rx=3)
        lab = f"{p:.0f}%" if p >= 10 else f"{p:.1f}%" if p >= 1 else f"{p:.2f}%"
        f.text(x + w / 2, y - 7, lab, "strong", "middle", fill="accent-text" if n == 2 else "ink")
        f.text(x + w / 2, bottom + 18, f"{n} vs {n}", "small", "middle", fill="ink")
    f.text(left + slot * 1.5, bottom - (bottom - top) * 0.33 - 30, "a quick run", "small", "middle", fill="accent-text")
    f.text((left + right) / 2, bottom + 36, "processes per variant", "tiny", "middle")
    f.save()


if __name__ == "__main__":
    fig_annotated_row()
    fig_workloads()
    fig_sample_ratios()
    fig_leak_control()
    fig_case_editor()
    fig_case_player()
    fig_schedule()
    fig_chance()
