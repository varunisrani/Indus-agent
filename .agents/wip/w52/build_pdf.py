"""Build a side-by-side Hermes vs Indus PDF for one surface (desktop or web).

usage: python3 build_pdf.py <compare-dir> <surface> <title> <out.pdf>
Reads <compare-dir>/{hermes,indus}/<surface>/manifest.json, writes an HTML book next to the PDF and prints it with
headless Chromium (one A3-landscape page per screen: light row on top, dark row below, Hermes left, Indus right).
"""
import html, json, re, subprocess, sys
from pathlib import Path

ROOT, SURFACE, TITLE, OUT = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
BRIEF = (ROOT / "brief.md").read_text(encoding="utf-8")
section = BRIEF.split(f"Canonical {SURFACE} slugs:")[1].split("\n\n")[0].split("Canonical")[0]
CANON = [s.strip().rstrip(".") for s in re.split(r"[,\n]", section) if s.strip().rstrip(".")]


def load(app):
    d = ROOT / app / SURFACE
    try:
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return d, [], []
    entries = m["entries"] if isinstance(m, dict) and "entries" in m else (m if isinstance(m, list) else m.get("shots", []))
    missing = m.get("missing", []) if isinstance(m, dict) else []
    return d, [e for e in entries if isinstance(e, dict) and e.get("slug")], missing


hd, hent, hmiss = load("hermes")
idir, ient, imiss = load("indus")
H = {e["slug"]: e for e in hent}
I = {e["slug"]: e for e in ient}
order = [s for s in CANON if s in H or s in I]
order += [e["slug"] for e in hent if e["slug"] not in order]
order += [e["slug"] for e in ient if e["slug"] not in order]


def img(d, e, theme, label):
    f = e and e.get(theme)
    if f and (d / f).is_file():
        return f'<figure><figcaption>{label}</figcaption><img src="{(d / f).as_uri()}"></figure>'
    return f'<figure><figcaption>{label}</figcaption><div class="none">Not captured / not in this app</div></figure>'


def text(e, key):
    return html.escape(str(e.get(key, ""))) if e else ""


css = """@page{size:420mm 297mm;margin:8mm}body{font-family:'DejaVu Sans',sans-serif;margin:0;color:#111}
.pg{page-break-after:always;height:279mm;overflow:hidden;display:flex;flex-direction:column}
h1{font-size:20pt;margin:0 0 2mm}h2{font-size:15pt;margin:0}.meta{font-size:9pt;color:#444;margin:1mm 0 3mm}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:4mm;flex:1}figure{margin:0;display:flex;flex-direction:column}
figcaption{font-size:9pt;font-weight:bold;margin-bottom:1mm}img{max-width:100%;max-height:118mm;width:auto;height:auto;object-fit:contain;align-self:flex-start;border:1px solid #bbb}
.none{flex:1;border:1px dashed #999;display:flex;align-items:center;justify-content:center;color:#777;font-size:12pt;
height:118mm}table{border-collapse:collapse;font-size:8.5pt}td,th{border:1px solid #ccc;padding:1mm 2mm;text-align:left}
.cols{columns:3;column-gap:8mm}"""
parts = [f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>"]
rows = "".join(
    f"<tr><td>{n}</td><td>{html.escape(s)}</td><td>{'✓' if s in H else '—'}</td><td>{'✓' if s in I else '—'}</td></tr>"
    for n, s in enumerate(order, 1))
parts.append(f"<div class='pg'><h1>{html.escape(TITLE)}</h1><div class='meta'>Hermes (original, left) vs Indus (rebuild, right); "
             f"each page: light theme on top, dark theme below. {len(order)} screens; Hermes {len(H)}, Indus {len(I)}, both "
             f"{len(set(H) & set(I))}.</div><div class='cols'><table><tr><th>#</th><th>Screen</th><th>Hermes</th><th>Indus</th>"
             f"</tr>{rows}</table></div></div>")
for n, s in enumerate(order, 1):
    h, i = H.get(s), I.get(s)
    title = text(h, "title") or text(i, "title") or s
    group = text(h, "group") or text(i, "group")
    note = " · ".join(x for x in (f"Hermes: {text(h, 'note')}" if text(h, "note") else "",
                                    f"Indus: {text(i, 'note')}" if text(i, "note") else "") if x)
    parts.append(f"<div class='pg'><h2>{n}. {title} <span style='font-weight:normal;color:#666'>({html.escape(s)}"
                 f"{' · ' + group if group else ''})</span></h2><div class='meta'>{note}</div><div class='grid'>"
                 + img(hd, h, "light", "Hermes · light") + img(idir, i, "light", "Indus · light")
                 + img(hd, h, "dark", "Hermes · dark") + img(idir, i, "dark", "Indus · dark") + "</div></div>")
parts.append("</body></html>")
page = OUT.with_suffix(".html")
page.write_text("".join(parts), encoding="utf-8")
subprocess.run(["/opt/pw-browsers/chromium", "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                "--allow-file-access-from-files", f"--print-to-pdf={OUT}", page.as_uri()], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
print(OUT, OUT.stat().st_size, "bytes,", len(order), "screens")
