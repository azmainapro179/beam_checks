"""
Self-contained web UI for the beam checker.

Upload a DXF drawing; the page runs BeamStirrupChecker on it and displays,
clearly:

  * every longitudinal beam elevation's parsed properties
    (section size, supports, stirrup zones, span dimensions, rebar callouts)
  * stirrup-spacing and top/bottom reinforcement-ratio checks for each section
  * any parsing/checking errors

Uses only the Python standard library plus the project's own checker module
(which needs ezdxf). Run it with the project's virtualenv:

    .venv/bin/python webapp.py

then open http://127.0.0.1:8000/ in a browser.
"""

import html
import io
import math
import os
import secrets
import tempfile
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fpdf import FPDF

from checker.beam_checker import BeamStirrupChecker

HOST, PORT = "127.0.0.1", 4000
MAX_UPLOAD = 64 * 1024 * 1024   # 64 MB cap on uploaded DXF

_upload_cache: dict[str, tuple[bytes, str, float, float]] = {}


# ---------------------------------------------------------------------------
# Minimal multipart/form-data parser (extract the single uploaded file)
# ---------------------------------------------------------------------------

def extract_multipart_form(body, content_type):
    """
    Return (filename, file_bytes, fields) from a multipart/form-data body.
    """
    marker = "boundary="
    if marker not in content_type:
        return None, None, {}
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    delimiter = ("--" + boundary).encode()
    filename = file_bytes = None
    fields = {}

    for part in body.split(delimiter):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        header_text = raw_headers.decode("utf-8", "replace")
        headers = header_text.lower()
        # strip the trailing CRLF that precedes the next boundary
        if content.endswith(b"\r\n"):
            content = content[:-2]
        if "filename=" in headers and file_bytes is None:
            candidate = header_text.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
            if candidate:
                filename, file_bytes = candidate, content
            continue
        if "name=" not in headers:
            continue
        name = header_text.split("name=", 1)[1].split(";", 1)[0].split("\r\n", 1)[0].strip().strip('"')
        if name:
            fields[name] = content.decode("utf-8", "replace").strip()
    return filename, file_bytes, fields


def extract_uploaded_file(body, content_type):
    """Backward-compatible helper returning only the uploaded file."""
    filename, file_bytes, _fields = extract_multipart_form(body, content_type)
    return filename, file_bytes


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

PAGE_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beam Reinforcement Checker</title>
<style>
  :root { --blue:#2575fc; --purple:#6a11cb; --ink:#2c3e50; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#f4f7fb;
         margin:0; padding:0 20px 60px; color:#333; }
  header { text-align:center; padding:28px 0 8px; }
  h1 { color:var(--ink); margin:0 0 4px; font-size:26px; }
  .sub { color:#7a8699; margin:0; }
  form { background:#fff; padding:22px; border-radius:12px; max-width:640px; margin:24px auto;
         box-shadow:0 6px 18px rgba(0,0,0,.08); text-align:center; }
  input[type=file] { padding:10px; border:1px dashed #b9c4d6; border-radius:8px; width:100%;
                     margin-bottom:14px; background:#fbfdff; }
  .materials { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:0 0 14px; text-align:left; }
  .materials label { font-size:13px; color:var(--ink); font-weight:600; }
  .materials input { display:block; width:100%; margin-top:5px; padding:9px; border:1px solid #cdd6e3;
                     border-radius:7px; font-size:14px; }
  button { background:var(--blue); color:#fff; border:0; padding:11px 26px; border-radius:8px;
           font-size:15px; cursor:pointer; transition:.2s; }
  button:hover { background:#1a5fe0; }
  main { max-width:1040px; margin:0 auto; }
  h2 { color:var(--ink); border-bottom:2px solid #e3e9f2; padding-bottom:8px; margin-top:44px; }
  .beam { background:#fff; border-radius:12px; box-shadow:0 4px 14px rgba(0,0,0,.06);
          padding:18px 22px; margin:22px 0; }
  .beam h3 { background:linear-gradient(90deg,var(--purple),var(--blue)); color:#fff;
             padding:11px 14px; border-radius:8px; margin:0 0 14px; font-size:16px; }
  .meta { display:flex; flex-wrap:wrap; gap:10px 26px; margin:0 0 14px; }
  .meta div { font-size:14px; }
  .meta b { color:var(--ink); }
  table { border-collapse:collapse; width:100%; margin:10px 0 18px; font-size:14px; }
  th { background:#34495e; color:#fff; padding:8px 10px; text-align:center; }
  td { border:1px solid #eef1f6; padding:7px 10px; text-align:center; }
  tr:nth-child(even) td { background:#f9fbff; }
  .cap { font-weight:600; color:var(--ink); margin:14px 0 4px; }
  .pass { color:#188a42; font-weight:700; }
  .fail { color:#d64545; font-weight:700; }
  .pill { display:inline-block; padding:2px 9px; border-radius:20px; font-size:12px; font-weight:700; }
  .pill.ok { background:#e4f6ea; color:#188a42; }
  .pill.no { background:#fce7e7; color:#d64545; }
  .pill.na { background:#eef1f6; color:#667085; }
  .errbox { background:#fff5f5; border:1px solid #f3c7c7; border-radius:10px; padding:14px 18px;
            margin:22px 0; }
  .errbox li { color:#a33; }
  .empty { color:#8a94a6; font-style:italic; }
  .filerow { text-align:center; color:#7a8699; margin-top:8px; }
  a.back { display:inline-block; margin-top:26px; color:var(--blue); text-decoration:none; }
</style>
</head>
<body>
<header>
  <h1>Beam Reinforcement Checker</h1>
  <p class="sub">Upload a DXF drawing to parse beam properties and run reinforcement checks.</p>
</header>
"""

UPLOAD_FORM = """
<form method="POST" action="/check" enctype="multipart/form-data">
  <input type="file" name="file" accept=".dxf" required>
  <div class="materials">
    <label>Concrete strength f&#8242;c (MPa)
      <input type="number" name="fc_mpa" value="28" min="0.1" step="0.1" required>
    </label>
    <label>Steel yield strength f<sub>y</sub> (MPa)
      <input type="number" name="fy_mpa" value="420" min="0.1" step="0.1" required>
    </label>
  </div>
  <br>
  <button type="submit">Upload &amp; Check</button>
  <div class="filerow">Accepts AutoCAD .dxf files.</div>
</form>
"""

PAGE_FOOT = "</body></html>"


def esc(v):
    return html.escape(str(v))


def fmt(v, nd=0):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return esc(v)


def fmt_area(v):
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.0f} mm&sup2;"
    except (TypeError, ValueError):
        return esc(v)


def fmt_ratio(v):
    if v is None:
        return "N/A"
    value = float(v)
    return f"{value:.6f}<br><span class=\"sub\">({value * 100:.3f}%)</span>"


def fmt_moment(v):
    if v is None:
        return "N/A"
    return f"{float(v):.1f} kN&middot;m"


def fmt_section_source(source):
    section = source.get("section")
    note = source.get("note") or ""
    if section:
        return f"{esc(section)}<br><span class=\"sub\">{esc(note)}</span>"
    return f"N/A<br><span class=\"sub\">{esc(note)}</span>"


def render_beam(beam):
    """Render one longitudinal-elevation beam's parsed properties."""
    parts = [f'<div class="beam"><h3>{esc(beam["label"])}</h3>']

    parts.append(
        '<div class="meta">'
        f'<div><b>Beam ID:</b> {esc(beam["id"])}</div>'
        f'<div><b>Section:</b> {esc(beam["section_width"])} &times; {esc(beam["section_depth"])} mm '
        f'(b &times; h)</div>'
        f'<div><b>Support faces:</b> {len(beam["support_positions"])}</div>'
        '</div>'
    )

    # Associated cross-section checks (from nearby section drawings)
    parts.append(render_beam_section_checks(beam.get("section_checks") or []))

    # Support positions, shown relative to the first support so they're readable
    sp = beam["support_positions"]
    if sp:
        base = min(sp)
        rel = " , ".join(fmt(x - base, 0) for x in sp)
        parts.append(f'<div class="cap">Support face positions (mm from first face)</div>')
        parts.append(f'<div style="font-size:14px">{rel}</div>')

    # Stirrup zones
    parts.append('<div class="cap">Stirrup zones</div>')
    zones = beam["stirrup_zones"]
    if zones:
        rows = "".join(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>&Oslash;{esc(z['stirrup_diameter'])} mm</td>"
            f"<td>{esc(z['stirrup_spacing'])} mm c/c</td>"
            f"<td>{fmt(z['length'], 0)} mm</td>"
            "</tr>"
            for i, z in enumerate(zones, 1)
        )
        parts.append(
            '<table><tr><th>#</th><th>Stirrup dia.</th><th>Spacing</th>'
            f'<th>Zone length</th></tr>{rows}</table>'
        )
    else:
        parts.append('<div class="empty">No stirrup zones parsed.</div>')

    # Span dimensions
    parts.append('<div class="cap">Span / support dimensions</div>')
    spans = beam["span_dimensions"]
    if spans:
        rows = "".join(
            f"<tr><td>{i}</td><td>{fmt(s['length'], 0)} mm</td></tr>"
            for i, s in enumerate(spans, 1)
        )
        parts.append(f'<table><tr><th>#</th><th>Length</th></tr>{rows}</table>')
    else:
        parts.append('<div class="empty">No span dimensions parsed.</div>')

    # Flexural Mn reinforcement inputs and checks
    parts.append('<div class="cap">Flexural Mn reinforcement inputs</div>')
    flexural = beam.get("flexural_span_areas") or []
    if flexural:
        rows = ""
        for c in flexural:
            rows += (
                "<tr>"
                f"<td>{esc(c['span_index'])}</td>"
                f"<td>{fmt(c['span_end'] - c['span_start'], 0)} mm</td>"
                f"<td>{fmt_section_source(c['left'])}</td>"
                f"<td>{fmt_area(c['mn_l_minus_area'])}</td>"
                f"<td>{fmt_area(c['mn_l_plus_area'])}</td>"
                f"<td>{fmt_section_source(c['right'])}</td>"
                f"<td>{fmt_area(c['mn_r_minus_area'])}</td>"
                f"<td>{fmt_area(c['mn_r_plus_area'])}</td>"
                f"<td>{fmt_section_source(c['middle'])}</td>"
                f"<td>{fmt_area(c['mn_mid_minus_area'])}</td>"
                f"<td>{fmt_area(c['mn_mid_plus_area'])}</td>"
                "</tr>"
            )
        parts.append(
            '<table><tr><th>Span</th><th>ln</th><th>Left section</th>'
            '<th>Mn,l- top As</th><th>Mn,l+ bottom As</th>'
            '<th>Right section</th><th>Mn,r- top As</th><th>Mn,r+ bottom As</th>'
            '<th>Middle section</th><th>Mn- top As</th><th>Mn+ bottom As</th></tr>'
            f'{rows}</table>'
        )

        parts.append('<div class="cap">Reinforcement area ratio checks</div>')
        parts.append(
            '<p class="sub" style="text-align:left">'
            'Mn = &Sigma;A<sub>s</sub> (sum of bar areas). '
            'Mn- = top bar area (negative bending); '
            'Mn+ = bottom bar area (positive bending).</p>'
        )
        moment_rows = ""
        for span in flexural:
            for check in span.get("moment_checks") or []:
                moment_rows += (
                    "<tr>"
                    f"<td>{esc(span['span_index'])}</td>"
                    f"<td>{esc(check['label'])}</td>"
                    f"<td>{fmt_area(check['provided'])}</td>"
                    f"<td>{fmt_area(check['required'])}</td>"
                    f"<td>{badge(check['passed'])}</td>"
                    "</tr>"
                )
        parts.append(
            '<table><tr><th>Span</th><th>Rule</th><th>Provided A<sub>s</sub></th>'
            f'<th>Required A<sub>s</sub></th><th>Result</th></tr>{moment_rows}</table>'
        )
    else:
        parts.append('<div class="empty">No span section cuts found for flexural Mn area extraction.</div>')

    # Clear-span/depth check (ln >= 4d)
    parts.append('<div class="cap">Clear span to depth (ln &ge; 4d)</div>')
    ln_checks = beam.get("ln_depth_checks") or []
    if ln_checks:
        rows = ""
        for i, c in enumerate(ln_checks, 1):
            if c["passed"] is None:
                rows += (
                    "<tr>"
                    f"<td>{i}</td>"
                    f"<td>{fmt(c['ln'], 0)} mm</td>"
                    "<td>N/A</td>"
                    "<td>N/A</td>"
                    f"<td>{esc(c['note'])}</td>"
                    f"<td>{badge(None)}</td>"
                    "</tr>"
                )
            else:
                rows += (
                    "<tr>"
                    f"<td>{i}</td>"
                    f"<td>{fmt(c['ln'], 0)} mm</td>"
                    f"<td>{fmt(c['min_ln'], 0)} mm</td>"
                    f"<td>{esc(c['d_from'])}</td>"
                    "<td>ln &ge; 4d</td>"
                    f"<td>{badge(c['passed'])}</td>"
                    "</tr>"
                )
        parts.append(
            '<table><tr><th>Span</th><th>ln</th><th>Required min.</th>'
            f'<th>d from</th><th>Rule</th><th>Result</th></tr>{rows}</table>'
        )
    else:
        parts.append('<div class="empty">No clear spans found to check.</div>')

    # First-stirrup-from-support-face check (>= 50 mm each side of every span)
    parts.append('<div class="cap">First stirrup clear of support face (&ge; 50 mm)</div>')
    checks = beam.get("first_stirrup_checks") or []
    if checks:
        rows = "".join(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{fmt(c['gap_left'], 0)} mm</td>"
            f"<td>{fmt(c['gap_right'], 0)} mm</td>"
            f"<td>{badge(c['passed'])}</td>"
            "</tr>"
            for i, c in enumerate(checks, 1)
        )
        parts.append(
            '<table><tr><th>Span</th><th>Left gap</th><th>Right gap</th>'
            f'<th>Result</th></tr>{rows}</table>'
        )
    else:
        parts.append('<div class="empty">No stirrups found to check.</div>')

    # Stirrup zone length check (left/right zone >= 2h)
    h_mm = beam.get("section_depth") or 0
    parts.append(f'<div class="cap">Stirrup zone length (&ge; 2h = {fmt(2 * h_mm, 0)} mm)</div>')
    zl_checks = beam.get("zone_length_checks") or []
    if zl_checks:
        zl_rows = ""
        for i, zc in enumerate(zl_checks, 1):
            left_txt = f"{fmt(zc['left_zone_length'], 0)} mm" if zc["left_zone_length"] is not None else "&mdash;"
            right_txt = f"{fmt(zc['right_zone_length'], 0)} mm" if zc["right_zone_length"] is not None else "&mdash;"
            zl_rows += (
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{left_txt}</td>"
                f"<td>{right_txt}</td>"
                f"<td>{fmt(zc['min_length'], 0)} mm</td>"
                f"<td>{badge(zc['passed'])}</td>"
                "</tr>"
            )
        parts.append(
            '<table><tr><th>Span</th><th>Left zone</th><th>Right zone</th>'
            f'<th>Min (2h)</th><th>Result</th></tr>{zl_rows}</table>'
        )
    else:
        parts.append('<div class="empty">No stirrup zones found to check.</div>')

    # Mid-span spacing check (s <= d/2), located from the elevation
    parts.append('<div class="cap">Mid-span stirrup spacing (s &le; d/2)</div>')
    mc = beam.get("midspan_check")
    if not mc:
        parts.append('<div class="empty">No mid-span section marker found on this beam.</div>')
    elif mc["passed"] is None:
        parts.append(f'<div class="empty">Section {esc(mc["section"])}: {esc(mc["note"])}.</div>')
    else:
        parts.append(
            '<table><tr><th>Section</th><th>Spacing s</th><th>d/2</th>'
            '<th>d from</th><th>Result</th></tr>'
            "<tr>"
            f"<td>{esc(mc['section'])}</td>"
            f"<td class=\"{'pass' if mc['passed'] else 'fail'}\">{esc(mc['s'])} mm</td>"
            f"<td>{fmt(mc['s_max'], 0)} mm</td>"
            f"<td>{esc(mc['d_from'])}</td>"
            f"<td>{badge(mc['passed'])}</td>"
            "</tr></table>"
        )

    # Longitudinal bars
    parts.append('<div class="cap">Longitudinal bars</div>')
    bars = beam["longitudinal_bars"]
    if bars:
        rows = ""
        for b in bars:
            desc = " + ".join(
                f"{p['count']}&ndash;&Oslash;{p['diameter']} mm" for p in b["bars"]
            )
            rows += (
                f"<tr><td>{esc(b['position']).title()}</td><td>{desc}</td>"
                f"<td>{esc(b['text'])}</td></tr>"
            )
        parts.append(
            '<table><tr><th>Position</th><th>Bars</th><th>Callout text</th></tr>'
            f'{rows}</table>'
        )
    else:
        parts.append('<div class="empty">No longitudinal bar callouts parsed.</div>')

    parts.append("</div>")
    return "".join(parts)


def badge(ok):
    if ok is None:
        return '<span class="pill na">N/A</span>'
    return ('<span class="pill ok">PASS</span>' if ok
            else '<span class="pill no">FAIL</span>')


def render_beam_section_checks(section_checks):
    """Render section check results inline within a beam card."""
    if not section_checks:
        return ""

    parts = ['<div class="cap">Associated cross-section checks</div>']

    summary_head = (
        "<tr><th>Section</th><th>h</th><th>d</th>"
        "<th>Stirrup</th><th>Spacing s</th><th>Max s</th><th>Spacing</th>"
        "<th>Top / Bot bars</th><th>&ge;2 each</th><th>&rho;</th><th>Section</th></tr>"
    )
    summary_rows = ""
    rho_rows = ""
    for r in section_checks:
        sp_ok = r["spacing_passed"]
        bars_ok = r["bars_passed"]
        rho_ok = r["rho_passed"]
        ok = r["passed"]
        summary_rows += (
            "<tr>"
            f"<td>{esc(r['section'])}</td>"
            f"<td>{fmt(r['h'], 0)}</td>"
            f"<td>{fmt(r['d'], 1)}</td>"
            f"<td>&Oslash;{esc(r['stirrup_dia'])} mm</td>"
            f"<td class=\"{'pass' if sp_ok else 'fail'}\">{esc(r['s'])} mm</td>"
            f"<td>{fmt(r['s_max'], 0)} mm</td>"
            f"<td>{badge(sp_ok)}</td>"
            f"<td>{esc(r['top_bars'])} / {esc(r['bottom_bars'])}</td>"
            f"<td>{badge(bars_ok)}</td>"
            f"<td>{badge(rho_ok)}</td>"
            f"<td>{badge(ok)}</td>"
            "</tr>"
        )
        for face, state in (("Top", r["top_rho"]), ("Bottom", r["bottom_rho"])):
            rho_rows += (
                "<tr>"
                f"<td>{esc(r['section'])}</td>"
                f"<td>{face}</td>"
                f"<td>{fmt(r['b'], 0)} &times; {fmt(r['h'], 0)} mm</td>"
                f"<td>{fmt_area(state['area_mm2'])}</td>"
                f"<td>{fmt_ratio(state['rho'])}</td>"
                f"<td>{fmt_ratio(state['rho_min'])}</td>"
                f"<td>{fmt_ratio(state['rho_max'])}</td>"
                f"<td>{badge(state['passed'])}</td>"
                "</tr>"
            )
    parts.append(f"<table>{summary_head}{summary_rows}</table>")
    rho_head = (
        "<tr><th>Section</th><th>Face</th><th>b &times; h</th><th>A<sub>s</sub></th>"
        "<th>&rho;</th><th>&rho;<sub>min</sub></th><th>&rho;<sub>max</sub></th>"
        "<th>Result</th></tr>"
    )
    parts.append(f'<div class="cap">Reinforcement ratio</div>')
    parts.append(f"<table>{rho_head}{rho_rows}</table>")
    return "".join(parts)


def render_results(filename, results, elevations, errors, pdf_token=None):
    out = [PAGE_HEAD, "<main>"]
    out.append(f'<p class="filerow">Analysed file: <b>{esc(filename)}</b></p>')

    if pdf_token:
        out.append(
            '<div style="text-align:right; margin:10px 0">'
            f'<form method="POST" action="/pdf" style="display:inline; background:none; '
            f'box-shadow:none; padding:0; margin:0; text-align:right">'
            f'<input type="hidden" name="token" value="{esc(pdf_token)}">'
            '<button type="submit" style="background:#e74c3c; padding:9px 22px; '
            'font-size:14px; border-radius:8px; color:#fff; border:0; cursor:pointer">'
            'Export PDF</button>'
            '</form></div>'
        )

    out.append(f"<h2>Beam elevation properties ({len(elevations)} beam"
               f"{'s' if len(elevations) != 1 else ''})</h2>")
    if elevations:
        for beam in elevations:
            out.append(render_beam(beam))
    else:
        out.append('<p class="empty">No longitudinal beam elevations were found in this drawing.</p>')

    if errors:
        out.append('<div class="errbox"><b>Notes / parsing issues</b><ul>')
        for err in errors:
            detail = "; ".join(f"{k}: {v}" for k, v in err.items())
            out.append(f"<li>{esc(detail)}</li>")
        out.append("</ul></div>")

    out.append('<a class="back" href="/">&larr; Check another drawing</a>')
    out.append("</main>")
    out.append(PAGE_FOOT)
    return "".join(out)


def render_upload_page(message=""):
    body = PAGE_HEAD + "<main>"
    if message:
        body += f'<div class="errbox">{esc(message)}</div>'
    body += UPLOAD_FORM + "</main>" + PAGE_FOOT
    return body


# ---------------------------------------------------------------------------
# PDF report generation
# ---------------------------------------------------------------------------

def _pdf_badge(ok):
    if ok is None:
        return "N/A"
    return "PASS" if ok else "FAIL"


def _pdf_area(v):
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.0f} mm2"
    except (TypeError, ValueError):
        return str(v)


def _pdf_section_source(source):
    section = source.get("section")
    note = source.get("note") or ""
    if note:
        note = (note
                .replace("fallback from ", "")
                .replace("section", "sec")
                .replace("longitudinal-bar extents at span midpoint", "bar extents")
                .replace("from ", "")
                .replace("elevation ", "elev ")
                .replace("previous ", "prev "))
        if "x=" in note:
            note = note.split("(")[0].strip() if "(" in note else note
    if section:
        return f"{section} ({note})" if note else section
    return f"N/A ({note})" if note else "N/A"


def generate_pdf(filename, results, elevations, errors, fc_mpa, fy_mpa):
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    avail = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text):
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(44, 62, 80)
        pdf.cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(227, 233, 242)
        pdf.line(pdf.get_x(), pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)

    def subheading(text):
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(44, 62, 80)
        pdf.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    def table(headers, rows, col_widths=None, font_size=None):
        if not col_widths:
            col_widths = [avail / len(headers)] * len(headers)
        hdr_size = font_size or 7.5
        body_size = (font_size - 0.5) if font_size else 7
        pdf.set_font("Helvetica", "B", hdr_size)
        pdf.set_fill_color(52, 73, 94)
        pdf.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            pdf.cell(col_widths[i], 6, h, border=1, align="C", fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", body_size)
        pdf.set_text_color(51, 51, 51)
        for ri, row in enumerate(rows):
            if pdf.get_y() + 6 > pdf.h - pdf.b_margin:
                pdf.add_page()
            fill = ri % 2 == 1
            if fill:
                pdf.set_fill_color(249, 251, 255)
            for i, cell_val in enumerate(row):
                text = str(cell_val)
                pdf.cell(col_widths[i], 5.5, text, border=1, align="C", fill=fill)
            pdf.ln()

    # ---- Title page ----
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(44, 62, 80)
    pdf.cell(0, 14, "Beam Reinforcement Check Report", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 7, f"File: {filename}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"f'c = {fc_mpa} MPa,  fy = {fy_mpa} MPa", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"{len(elevations)} beam elevation(s) analysed", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ---- Summary table ----
    heading("Summary")
    pass_count = sum(1 for b in elevations for c in (b.get("section_checks") or []) if c["passed"])
    fail_count = sum(1 for b in elevations for c in (b.get("section_checks") or []) if not c["passed"])
    total_checks = pass_count + fail_count
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(51, 51, 51)
    pdf.cell(0, 6, f"Total cross-section checks: {total_checks}  |  "
             f"PASS: {pass_count}  |  FAIL: {fail_count}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ---- Per-beam details ----
    for beam in elevations:
        if pdf.get_y() + 40 > pdf.h - pdf.b_margin:
            pdf.add_page()
        heading(beam["label"])

        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(51, 51, 51)
        pdf.cell(0, 5.5,
                 f"ID: {beam['id']}  |  Section: {beam['section_width']} x "
                 f"{beam['section_depth']} mm (b x h)  |  "
                 f"Support faces: {len(beam['support_positions'])}",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # -- Section checks --
        section_checks = beam.get("section_checks") or []
        if section_checks:
            subheading("Cross-section checks")
            headers = ["Section", "h", "d", "Stirrup", "s (mm)", "Max s",
                       "Spacing", "Top/Bot", ">=2", "rho", "Overall"]
            avail = pdf.w - pdf.l_margin - pdf.r_margin
            cw = [avail * f for f in [.1, .06, .07, .09, .07, .07, .07,
                                      .1, .07, .07, .07]]
            rows = []
            for r in section_checks:
                rows.append([
                    r["section"], f"{r['h']:.0f}", f"{r['d']:.1f}",
                    f"d{r['stirrup_dia']}mm", str(r["s"]), f"{r['s_max']:.0f}",
                    _pdf_badge(r["spacing_passed"]),
                    f"{r['top_bars']}/{r['bottom_bars']}",
                    _pdf_badge(r["bars_passed"]),
                    _pdf_badge(r["rho_passed"]),
                    _pdf_badge(r["passed"]),
                ])
            table(headers, rows, cw)
            pdf.ln(2)

            subheading("Reinforcement ratio detail")
            rh = ["Section", "Face", "b x h", "As (mm2)", "rho", "rho_min", "rho_max", "Result"]
            rcw = [avail * f for f in [.1, .08, .12, .12, .12, .12, .12, .08]]
            rrows = []
            for r in section_checks:
                for face, st in (("Top", r["top_rho"]), ("Bottom", r["bottom_rho"])):
                    rho_val = f"{st['rho']:.6f}" if st["rho"] is not None else "N/A"
                    rho_min = f"{st['rho_min']:.6f}" if st["rho_min"] is not None else "N/A"
                    rho_max = f"{st['rho_max']:.6f}" if st["rho_max"] is not None else "N/A"
                    area = f"{st['area_mm2']:.0f}" if st["area_mm2"] is not None else "N/A"
                    rrows.append([
                        r["section"], face,
                        f"{r['b']:.0f} x {r['h']:.0f}",
                        area, rho_val, rho_min, rho_max,
                        _pdf_badge(st["passed"]),
                    ])
            table(rh, rrows, rcw)
            pdf.ln(2)

        # -- Stirrup zones --
        zones = beam["stirrup_zones"]
        if zones:
            subheading("Stirrup zones")
            zh = ["#", "Stirrup dia.", "Spacing", "Zone length"]
            zcw = [avail * f for f in [.1, .3, .3, .3]]
            zrows = [[str(i), f"d{z['stirrup_diameter']}mm",
                       f"{z['stirrup_spacing']}mm c/c", f"{z['length']:.0f}mm"]
                      for i, z in enumerate(zones, 1)]
            table(zh, zrows, zcw)
            pdf.ln(2)

        # -- Span dimensions --
        spans = beam["span_dimensions"]
        if spans:
            subheading("Span / support dimensions")
            sh = ["#", "Length (mm)"]
            scw = [avail * .2, avail * .8]
            srows = [[str(i), f"{s['length']:.0f}"] for i, s in enumerate(spans, 1)]
            table(sh, srows, scw)
            pdf.ln(2)

        # -- Flexural Mn --
        flexural = beam.get("flexural_span_areas") or []
        if flexural:
            subheading("Flexural Mn section sources")
            src_h = ["Span", "ln (mm)", "Left section", "Right section", "Middle section"]
            src_cw = [avail * f for f in [.06, .1, .28, .28, .28]]
            src_rows = []
            for c in flexural:
                src_rows.append([
                    str(c["span_index"]),
                    f"{c['span_end'] - c['span_start']:.0f}",
                    _pdf_section_source(c["left"]),
                    _pdf_section_source(c["right"]),
                    _pdf_section_source(c["middle"]),
                ])
            table(src_h, src_rows, src_cw)
            pdf.ln(1)

            subheading("Flexural Mn reinforcement areas")
            fh = ["Span", "Mn,l-", "Mn,l+", "Mn,r-", "Mn,r+", "Mn,mid-", "Mn,mid+"]
            fcw = [avail * f for f in [.1, .15, .15, .15, .15, .15, .15]]
            frows = []
            for c in flexural:
                frows.append([
                    str(c["span_index"]),
                    _pdf_area(c["mn_l_minus_area"]),
                    _pdf_area(c["mn_l_plus_area"]),
                    _pdf_area(c["mn_r_minus_area"]),
                    _pdf_area(c["mn_r_plus_area"]),
                    _pdf_area(c["mn_mid_minus_area"]),
                    _pdf_area(c["mn_mid_plus_area"]),
                ])
            table(fh, frows, fcw)
            pdf.ln(1)

            subheading("Reinforcement area ratio checks")
            mh = ["Span", "Rule", "Provided As", "Required As", "Result"]
            mcw = [avail * f for f in [.08, .32, .2, .2, .1]]
            mrows = []
            for span in flexural:
                for chk in span.get("moment_checks") or []:
                    mrows.append([
                        str(span["span_index"]), chk["label"],
                        _pdf_area(chk["provided"]),
                        _pdf_area(chk["required"]),
                        _pdf_badge(chk["passed"]),
                    ])
            table(mh, mrows, mcw)
            pdf.ln(2)

        # -- Clear span / depth --
        ln_checks = beam.get("ln_depth_checks") or []
        if ln_checks:
            subheading("Clear span to depth (ln >= 4d)")
            lh = ["Span", "ln (mm)", "Required min", "d from", "Rule", "Result"]
            lcw = [avail * f for f in [.08, .15, .15, .22, .15, .1]]
            lrows = []
            for i, c in enumerate(ln_checks, 1):
                if c["passed"] is None:
                    lrows.append([str(i), f"{c['ln']:.0f}", "N/A", "N/A",
                                  c.get("note", ""), "N/A"])
                else:
                    lrows.append([str(i), f"{c['ln']:.0f}", f"{c['min_ln']:.0f}",
                                  c["d_from"], "ln >= 4d", _pdf_badge(c["passed"])])
            table(lh, lrows, lcw)
            pdf.ln(2)

        # -- First stirrup --
        fst_checks = beam.get("first_stirrup_checks") or []
        if fst_checks:
            subheading("First stirrup clear of support face (>= 50 mm)")
            fsh = ["Span", "Left gap", "Right gap", "Result"]
            fscw = [avail * f for f in [.15, .25, .25, .15]]
            fsrows = [[str(i), f"{c['gap_left']:.0f}mm", f"{c['gap_right']:.0f}mm",
                        _pdf_badge(c["passed"])]
                       for i, c in enumerate(fst_checks, 1)]
            table(fsh, fsrows, fscw)
            pdf.ln(2)

        # -- Stirrup zone length check --
        zl_checks = beam.get("zone_length_checks") or []
        if zl_checks:
            h_mm = beam.get("section_depth") or 0
            subheading(f"Stirrup zone length (>= 2h = {2 * h_mm:.0f} mm)")
            zlh = ["Span", "Left zone", "Right zone", "Min (2h)", "Result"]
            zlcw = [avail * f for f in [.1, .2, .2, .2, .1]]
            zlrows = []
            for i, zc in enumerate(zl_checks, 1):
                lt = f"{zc['left_zone_length']:.0f}mm" if zc["left_zone_length"] is not None else "-"
                rt = f"{zc['right_zone_length']:.0f}mm" if zc["right_zone_length"] is not None else "-"
                zlrows.append([str(i), lt, rt, f"{zc['min_length']:.0f}mm",
                               _pdf_badge(zc["passed"])])
            table(zlh, zlrows, zlcw)
            pdf.ln(2)

        # -- Mid-span check --
        mc = beam.get("midspan_check")
        if mc and mc["passed"] is not None:
            subheading("Mid-span stirrup spacing (s <= d/2)")
            msh = ["Section", "s (mm)", "d/2 (mm)", "d from", "Result"]
            mscw = [avail * f for f in [.2, .15, .15, .25, .1]]
            table(msh, [[mc["section"], str(mc["s"]), f"{mc['s_max']:.0f}",
                          mc["d_from"], _pdf_badge(mc["passed"])]], mscw)
            pdf.ln(2)

        # -- Longitudinal bars --
        bars = beam["longitudinal_bars"]
        if bars:
            subheading("Longitudinal bars")
            bh = ["Position", "Bars", "Callout"]
            bcw = [avail * .15, avail * .4, avail * .45]
            brows = []
            for b in bars:
                desc = " + ".join(f"{p['count']}-d{p['diameter']}mm" for p in b["bars"])
                brows.append([b["position"].title(), desc, b["text"]])
            table(bh, brows, bcw)
            pdf.ln(2)

    # ---- Errors ----
    if errors:
        if pdf.get_y() + 20 > pdf.h - pdf.b_margin:
            pdf.add_page()
        heading("Notes / parsing issues")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(163, 51, 51)
        for err in errors:
            detail = "; ".join(f"{k}: {v}" for k, v in err.items())
            pdf.cell(0, 5, f"- {detail}", new_x="LMARGIN", new_y="NEXT")

    return pdf.output()


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

def run_checker(dxf_bytes, display_name, fc_mpa=28.0, fy_mpa=420.0):
    """
    Write the uploaded bytes to a temp file and run the checker on it. Any
    exception has the internal temp path rewritten to the user's filename so
    error messages don't leak scratch paths.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".dxf", delete=False)
    try:
        tmp.write(dxf_bytes)
        tmp.close()
        try:
            checker = BeamStirrupChecker(tmp.name, fc_mpa=fc_mpa, fy_mpa=fy_mpa)
            with contextlib.redirect_stdout(io.StringIO()):   # silence the checker's prints
                results = checker.run()
        except Exception as exc:
            raise RuntimeError(str(exc).replace(tmp.name, display_name)) from exc
        return results, checker.elevations, checker.errors
    finally:
        os.unlink(tmp.name)


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html(render_upload_page())
        else:
            self._send_html(render_upload_page("Page not found."), status=404)

    def _read_post_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > MAX_UPLOAD:
            return None
        return self.rfile.read(length)

    def do_POST(self):
        if self.path == "/check":
            self._handle_check()
        elif self.path == "/pdf":
            self._handle_pdf()
        else:
            self._send_html(render_upload_page("Unknown action."), status=404)

    def _handle_check(self):
        body = self._read_post_body()
        if body is None:
            self._send_html(render_upload_page("No file was uploaded or file too large."), status=400)
            return

        filename, data, fields = extract_multipart_form(
            body, self.headers.get("Content-Type", "")
        )
        if not data:
            self._send_html(render_upload_page("No DXF file found in the upload."), status=400)
            return

        try:
            fc_mpa = float(fields.get("fc_mpa", "28"))
            fy_mpa = float(fields.get("fy_mpa", "420"))
            if (
                not math.isfinite(fc_mpa)
                or not math.isfinite(fy_mpa)
                or fc_mpa <= 0
                or fy_mpa <= 0
            ):
                raise ValueError
        except (TypeError, ValueError):
            self._send_html(render_upload_page(
                "Material strengths must be positive numbers in MPa."), status=400)
            return

        try:
            results, elevations, errors = run_checker(
                data, filename, fc_mpa=fc_mpa, fy_mpa=fy_mpa
            )
        except Exception as exc:
            self._send_html(render_upload_page(
                f"Could not process '{filename}': {exc}"), status=422)
            return

        token = secrets.token_urlsafe(16)
        _upload_cache[token] = (data, filename, fc_mpa, fy_mpa)

        self._send_html(render_results(filename, results, elevations, errors,
                                       pdf_token=token))

    def _handle_pdf(self):
        body = self._read_post_body()
        if body is None:
            self._send_html(render_upload_page("Bad request."), status=400)
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            _, _, fields = extract_multipart_form(body, content_type)
        else:
            from urllib.parse import parse_qs
            fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}

        token = fields.get("token", "")
        cached = _upload_cache.get(token)
        if not cached:
            self._send_html(render_upload_page(
                "Session expired. Please upload the file again."), status=400)
            return

        data, filename, fc_mpa, fy_mpa = cached

        try:
            results, elevations, errors = run_checker(
                data, filename, fc_mpa=fc_mpa, fy_mpa=fy_mpa
            )
        except Exception as exc:
            self._send_html(render_upload_page(
                f"Could not generate PDF: {exc}"), status=422)
            return

        pdf_bytes = generate_pdf(filename, results, elevations, errors,
                                 fc_mpa, fy_mpa)
        safe_name = os.path.splitext(filename)[0] + "_report.pdf"

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{safe_name}"')
        self.send_header("Content-Length", str(len(pdf_bytes)))
        self.end_headers()
        self.wfile.write(pdf_bytes)

    def log_message(self, *args):   # keep the console quiet
        pass


class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        import socket
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        super().server_bind()


def main():
    server = ReusableServer((HOST, PORT), Handler)
    print(f"Beam checker web UI running at http://{HOST}:{PORT}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
