"""The library report: one row per game, written as a spreadsheet (.csv) or a self-contained searchable web page (.html).

Nothing here imports Qt or touches the library itself; organize.find_library builds the entries."""
from __future__ import annotations

import csv
import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import fmt_size


@dataclass(slots=True)
class LibraryEntry:
    name: str  # the game's main file name
    system: str  # 'PlayStation 2', or 'Unknown system'
    kind: str  # 'CHD', 'ISO', 'GBA' ... (the file extension)
    region: str  # 'USA', 'Europe' ... or ''
    size: int  # bytes, counting a .cue's track files
    modified: float  # newest modification time (epoch seconds)
    folder: str  # the folder it sits in, relative to the scanned folder ('' for the top level)
    path: str = ""  # the file's full path (the PC Recomps page reads it to check for the right dump)


CSV_HEADER = ["System", "Name", "Format", "Region", "Size (bytes)", "Size", "Modified", "Folder"]


def _stamp(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _sorted(entries: list[LibraryEntry]) -> list[LibraryEntry]:
    return sorted(entries, key=lambda e: (e.system.casefold(), e.name.casefold()))


def write_csv(path: Path, entries: list[LibraryEntry]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # the BOM makes Excel read accents correctly
        out = csv.writer(f)
        out.writerow(CSV_HEADER)
        for e in _sorted(entries):
            out.writerow([e.system, e.name, e.kind, e.region, e.size, fmt_size(e.size), _stamp(e.modified), e.folder])


def summary(entries: list[LibraryEntry]) -> list[tuple[str, int, int]]:
    """(system, files, bytes) per system, biggest collection first."""
    totals: dict[str, list[int]] = {}
    for e in entries:
        t = totals.setdefault(e.system, [0, 0])
        t[0] += 1
        t[1] += e.size
    return sorted(((name, t[0], t[1]) for name, t in totals.items()), key=lambda row: (-row[1], row[0].casefold()))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1d2330;--muted:#5d6678;--line:#dde1ea;--head:#f1f3f8;--accent:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#161a22;--fg:#e6e9f0;--muted:#9aa3b5;--line:#2c3342;--head:#1f2430;--accent:#7aa2ff}}
body{font:15px/1.45 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px;max-width:1200px;margin-inline:auto}
h1{margin:0 0 4px}.sub{color:var(--muted);margin-bottom:20px}
table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--head);position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap}
td.n,th.n{text-align:right;white-space:nowrap}.wrap{overflow-x:auto;margin-bottom:28px}
input{font:inherit;padding:7px 10px;width:min(420px,100%);border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);margin:0 0 12px}
.chip{color:var(--muted)}a{color:var(--accent)}
</style></head><body>
<h1>__TITLE__</h1>
<div class="sub">__COUNT__ files, __SIZE__ &middot; made __DATE__ by Distill</div>
<h2>By system</h2>
<div class="wrap"><table id="sys"><thead><tr><th>System</th><th class="n">Files</th><th class="n">Size</th></tr></thead><tbody>__SUMMARY__</tbody></table></div>
<h2>Every game</h2>
<input id="q" type="search" placeholder="Filter by name, system, region or folder..." autofocus>
<div class="wrap"><table id="all"><thead><tr><th>System</th><th>Name</th><th>Format</th><th>Region</th><th class="n" data-num="1">Size</th><th>Modified</th><th>Folder</th></tr></thead><tbody>
__ROWS__</tbody></table></div>
<script>
const body=document.querySelector('#all tbody'),rows=[...body.rows],q=document.getElementById('q');
q.addEventListener('input',()=>{const t=q.value.toLowerCase();rows.forEach(r=>r.hidden=t&&!r.textContent.toLowerCase().includes(t))});
document.querySelectorAll('#all th').forEach((th,i)=>{let up=true;th.addEventListener('click',()=>{
 const num=th.dataset.num;const key=r=>num?+r.cells[i].dataset.v:r.cells[i].textContent.toLowerCase();
 rows.sort((a,b)=>{const x=key(a),y=key(b);return(x>y?1:x<y?-1:0)*(up?1:-1)});up=!up;rows.forEach(r=>body.appendChild(r))})});
</script></body></html>
"""


def write_html(path: Path, entries: list[LibraryEntry], title: str = "Game library") -> None:
    esc = html.escape
    rows = "\n".join(
        f"<tr><td>{esc(e.system)}</td><td>{esc(e.name)}</td><td>{esc(e.kind)}</td><td>{esc(e.region)}</td>"
        f"<td class=\"n\" data-v=\"{e.size}\">{esc(fmt_size(e.size))}</td><td>{esc(_stamp(e.modified))}</td>"
        f"<td class=\"chip\">{esc(e.folder)}</td></tr>" for e in _sorted(entries))
    by_system = "\n".join(f"<tr><td>{esc(n)}</td><td class=\"n\">{c}</td><td class=\"n\">{esc(fmt_size(b))}</td></tr>"
                          for n, c, b in summary(entries))
    page = (_PAGE.replace("__TITLE__", esc(title)).replace("__COUNT__", str(len(entries)))
            .replace("__SIZE__", esc(fmt_size(sum(e.size for e in entries)))).replace("__DATE__", datetime.now().strftime("%Y-%m-%d"))
            .replace("__SUMMARY__", by_system).replace("__ROWS__", rows))
    path.write_text(page, encoding="utf-8")


def write_report(path: Path, entries: list[LibraryEntry]) -> None:
    """.csv for a spreadsheet, anything else (normally .html) for the web page."""
    (write_csv if path.suffix.lower() == ".csv" else write_html)(path, entries)
