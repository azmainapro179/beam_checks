"""
Self-contained web UI for the beam checker.

Upload a DXF drawing; the page runs BeamStirrupChecker on it and displays,
clearly:

  * every longitudinal beam elevation's parsed properties
    (section size, supports, stirrup zones, span dimensions, rebar callouts)
  * the stirrup-spacing code check performed on each cross-section
  * any parsing/checking errors

Uses only the Python standard library plus the project's own checker module
(which needs ezdxf). Run it with the project's virtualenv:

    .venv/bin/python webapp.py

then open http://127.0.0.1:8000/ in a browser.
"""

import html
import io
import os
import tempfile
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checker.beam_checker import BeamStirrupChecker

HOST, PORT = "127.0.0.1", 8000
MAX_UPLOAD = 64 * 1024 * 1024   # 64 MB cap on uploaded DXF


# ---------------------------------------------------------------------------
# Minimal multipart/form-data parser (extract the single uploaded file)
# ---------------------------------------------------------------------------

def extract_uploaded_file(body, content_type):
    """
    Return (filename, file_bytes) for the first file part in a
    multipart/form-data body, or (None, None) if there isn't one.
    """
    marker = "boundary="
    if marker not in content_type:
        return None, None
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    delimiter = ("--" + boundary).encode()

    for part in body.split(delimiter):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        headers = raw_headers.decode("utf-8", "replace").lower()
        if "filename=" not in headers:
            continue
        filename = raw_headers.decode("utf-8", "replace")
        filename = filename.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
        if not filename:
            continue
        # strip the trailing CRLF that precedes the next boundary
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return filename, content
    return None, None


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

    # Flexural Mn reinforcement-area sources
    parts.append('<div class="cap">Flexural Mn reinforcement areas from section cuts</div>')
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


def render_section_checks(results):
    """Render every code check performed on each drawn (support/end) section."""
    if not results:
        return '<p class="empty">No beam cross-sections were found to check.</p>'

    head = (
        "<tr><th>Section</th><th>h</th><th>d</th>"
        "<th>Stirrup</th><th>Spacing s</th><th>Max s</th><th>Spacing</th>"
        "<th>Top / Bot bars</th><th>&ge;2 each</th><th>Section</th></tr>"
    )
    rows = ""
    for r in results:
        sp_ok, bars_ok, ok = r["spacing_passed"], r["bars_passed"], r["passed"]
        rows += (
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
            f"<td>{badge(ok)}</td>"
            "</tr>"
        )
    note = ('<p class="sub" style="text-align:left">'
            'These separately-drawn support/end sections must satisfy s &le; min(d/4, '
            '8&times;smallest bar dia., 24&times;stirrup dia., 300 mm), and have &ge;2 bars '
            'top and &ge;2 bottom. Mid-span sections (s &le; d/2 only) are checked per beam below.</p>')
    return f"<table>{head}{rows}</table>{note}"


def render_results(filename, results, elevations, errors):
    out = [PAGE_HEAD, "<main>"]
    out.append(f'<p class="filerow">Analysed file: <b>{esc(filename)}</b></p>')

    out.append("<h2>Stirrup-spacing check (cross-sections)</h2>")
    out.append(render_section_checks(results))

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
# Request handling
# ---------------------------------------------------------------------------

def run_checker(dxf_bytes, display_name):
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
            checker = BeamStirrupChecker(tmp.name)
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

    def do_POST(self):
        if self.path != "/check":
            self._send_html(render_upload_page("Unknown action."), status=404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_html(render_upload_page("No file was uploaded."), status=400)
            return
        if length > MAX_UPLOAD:
            self._send_html(render_upload_page("File too large (max 64 MB)."), status=413)
            return

        body = self.rfile.read(length)
        filename, data = extract_uploaded_file(body, self.headers.get("Content-Type", ""))
        if not data:
            self._send_html(render_upload_page("No DXF file found in the upload."), status=400)
            return

        try:
            results, elevations, errors = run_checker(data, filename)
        except Exception as exc:   # bad/unsupported DXF, parsing crash, etc.
            self._send_html(render_upload_page(
                f"Could not process '{filename}': {exc}"), status=422)
            return

        self._send_html(render_results(filename, results, elevations, errors))

    def log_message(self, *args):   # keep the console quiet
        pass


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Beam checker web UI running at http://{HOST}:{PORT}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
