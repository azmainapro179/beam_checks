#!/usr/bin/env python3
"""Generate a beam reinforcement check PDF in BNBC format.

Usage:
    python generate_report.py <drawing.dxf> [--fc 28] [--fy 420] [--frame smrf|imrf] [-o output.pdf]
"""
import argparse
import contextlib
import io
import os
import re
import sys

from fpdf import FPDF
from checker.beam_checker import BeamStirrupChecker


def _badge(ok):
    if ok is None:
        return "N/A"
    return "Passed" if ok else "Failed"


def _short_title(label):
    """Extract 'FB1(12" x 24") ALONG GRID-7' from the full label."""
    m = re.search(r'BEAM-(\w+)\(([^)]+)\)', label)
    if not m:
        return label[:60]
    btype = m.group(1)
    size = m.group(2).replace("''", '"')
    size = re.sub(r'(\d)"?\s*x\s*', r'\1" x ', size)
    if not size.endswith('"'):
        size += '"'
    grid_m = re.search(
        r'((?:ALONG|IN BETWEEN)\s+GRID[-\s]?[\w~]+'
        r'(?:\s*&\s*(?:IN BETWEEN\s+)?GRID[-\s]?[\w~]+)?)',
        label,
    )
    grid = grid_m.group(1).strip() if grid_m else ""
    return f'{btype}({size}) {grid}'.strip()


HDR_BG = (52, 73, 94)
HDR_CLR = (255, 255, 255)
DATA_CLR = (30, 30, 30)
STRIPE_BG = (242, 244, 248)
LINE_CLR = (180, 180, 180)
PASS_CLR = (39, 120, 55)
FAIL_CLR = (180, 40, 40)


def _rect_text(pdf, x, y, w, h, text, font_size=6.5, bold=False,
               bg=None, text_color=None, align="L", border=True,
               vcenter=False):
    """Draw bordered rect and write wrapped text inside it."""
    pdf.set_font("Helvetica", "B" if bold else "", font_size)
    if text_color:
        pdf.set_text_color(*text_color)
    if bg:
        pdf.set_fill_color(*bg)
        pdf.rect(x, y, w, h, "DF")
    elif border:
        pdf.rect(x, y, w, h)
    pad = 1.0
    lh = font_size * 0.42
    text_str = str(text)
    n_lines = text_str.count('\n') + 1
    total_h = n_lines * lh
    if vcenter and total_h < h:
        ty = y + (h - total_h) / 2
    else:
        ty = y + 0.8
    pdf.set_xy(x + pad, ty)
    pdf.multi_cell(w - 2 * pad, lh, text_str, align=align)


def generate_pdf(filename, elevations, errors, fc_mpa, fy_mpa, frame_system="smrf"):
    is_imrf = frame_system.lower() == "imrf"
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.set_draw_color(*LINE_CLR)
    LM = pdf.l_margin
    RM = pdf.r_margin
    PW = pdf.w - LM - RM
    PH = pdf.h

    BM = 12

    def page_break():
        pdf.add_page()
        return pdf.get_y()

    def ensure_space(needed):
        if pdf.get_y() + needed > PH - BM:
            return page_break()
        return pdf.get_y()

    def hline():
        y = pdf.get_y()
        pdf.set_draw_color(*LINE_CLR)
        pdf.line(LM, y, pdf.w - RM, y)
        pdf.set_y(y + 3)

    frame_label = (
        "Intermediate Moment Resisting Frame (IMRF)" if is_imrf
        else "Special Moment Resisting Frame (SMRF)"
    )

    # ================================================================
    #  TITLE & INPUT PARAMETERS
    # ================================================================
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*DATA_CLR)
    pdf.cell(0, 8, "BEAM REINFORCEMENT CHECK REPORT", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("Helvetica", "BU", 8)
    pdf.cell(0, 4, "Input Parameters:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 7)
    pdf.cell(0, 4, f"Concrete Strength fc' = {fc_mpa:.0f} MPa",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4, f"Rebar Strength fy = {fy_mpa:.0f} MPa",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 7)
    pdf.cell(28, 4, "Framing System:")
    pdf.set_font("Helvetica", "", 7)
    pdf.cell(0, 4, f" {frame_label}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    hline()
    pdf.ln(2)

    # ================================================================
    #  PER-BEAM RESULTS
    # ================================================================
    for beam in elevations:
        title = _short_title(beam["label"])

        section_checks = beam.get("section_checks") or []
        ln_checks = beam.get("ln_depth_checks") or []
        fst_checks = beam.get("first_stirrup_checks") or []
        zl_checks = beam.get("zone_length_checks") or []
        midspan = beam.get("midspan_check")
        flexural = beam.get("flexural_span_areas") or []

        # ---------- beam header ----------
        ensure_space(55)
        pdf.set_font("Helvetica", "BU", 11)
        pdf.set_text_color(*DATA_CLR)
        pdf.cell(0, 7, f"{title} Results:", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # =============================================================
        #  SECTION CHECKS TABLE
        # =============================================================
        if section_checks:
            pdf.set_font("Helvetica", "BU", 8)
            pdf.set_text_color(*DATA_CLR)
            pdf.cell(0, 5, "Section Checks:", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

            if is_imrf:
                cw = [30, 50, 40, 79, 78]
            else:
                cw = [28, 30, 21, 28, 21, 30, 21, 49, 49]

            hdr1_h = 16
            sub_h = 7
            row_h = 12

            hoop_sec_ref = "8.3.10.4(b)" if is_imrf else "8.3.4.3(b)"

            y0 = ensure_space(hdr1_h + sub_h + row_h * min(len(section_checks), 4) + 4)
            pdf.set_draw_color(*LINE_CLR)

            if is_imrf:
                x = LM
                _rect_text(pdf, x, y0, cw[0], hdr1_h + sub_h,
                           "Location", 7, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += cw[0]
                _rect_text(pdf, x, y0, cw[1] + cw[2], hdr1_h,
                           f"Max Hoop Spacing at\nSupport Check\n(BNBC {hoop_sec_ref})",
                           7, True, HDR_BG, HDR_CLR, vcenter=True)
                x += cw[1] + cw[2]
                _rect_text(pdf, x, y0, cw[3] + cw[4], hdr1_h,
                           "Longitudinal Rebar\nRatio (rho) Check\n(BNBC 8.3.4.2(a))",
                           7, True, HDR_BG, HDR_CLR, vcenter=True)

                y1 = y0 + hdr1_h
                x = LM + cw[0]
                _rect_text(pdf, x, y1, cw[1], sub_h,
                           "Provided\n(mm)", 5.5, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += cw[1]
                _rect_text(pdf, x, y1, cw[2], sub_h,
                           "As Per BNBC\n(mm)", 5, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += cw[2]
                _rect_text(pdf, x, y1, cw[3], sub_h,
                           "Provided", 5.5, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += cw[3]
                _rect_text(pdf, x, y1, cw[4], sub_h,
                           "As Per BNBC", 5, True, HDR_BG, HDR_CLR,
                           vcenter=True)
            else:
                x = LM
                _rect_text(pdf, x, y0, cw[0], hdr1_h + sub_h,
                           "Location", 7, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += cw[0]
                _rect_text(pdf, x, y0, cw[1] + cw[2], hdr1_h,
                           "Min Width Check\n(BNBC 8.3.4.1(c),\nBNBC 8.3.4.1(d))",
                           7, True, HDR_BG, HDR_CLR, vcenter=True)
                x += cw[1] + cw[2]
                _rect_text(pdf, x, y0, cw[3] + cw[4], hdr1_h,
                           f"Max Hoop Spacing at\nSupport Check\n(BNBC {hoop_sec_ref})",
                           7, True, HDR_BG, HDR_CLR, vcenter=True)
                x += cw[3] + cw[4]
                _rect_text(pdf, x, y0, cw[5] + cw[6], hdr1_h,
                           "Min No of Bars\nCheck\n(BNBC 8.3.4.2(a))",
                           7, True, HDR_BG, HDR_CLR, vcenter=True)
                x += cw[5] + cw[6]
                _rect_text(pdf, x, y0, cw[7] + cw[8], hdr1_h,
                           "Longitudinal Rebar\nRatio (rho) Check\n(BNBC 8.3.4.2(a))",
                           7, True, HDR_BG, HDR_CLR, vcenter=True)

                y1 = y0 + hdr1_h
                x = LM + cw[0]
                sec_units = ["(mm)", "(mm)", "(nos.)", ""]
                for ui in range(4):
                    ci = 1 + ui * 2
                    unit = sec_units[ui]
                    prov_lbl = f"Provided\n{unit}" if unit else "Provided"
                    bnbc_lbl = f"As Per BNBC\n{unit}" if unit else "As Per BNBC"
                    _rect_text(pdf, x, y1, cw[ci], sub_h,
                               prov_lbl, 5, True, HDR_BG, HDR_CLR,
                               vcenter=True)
                    x += cw[ci]
                    _rect_text(pdf, x, y1, cw[ci + 1], sub_h,
                               bnbc_lbl, 4.5, True, HDR_BG, HDR_CLR,
                               vcenter=True)
                    x += cw[ci + 1]

            # ---- data rows ----
            row_y = y0 + hdr1_h + sub_h
            for ri, sc in enumerate(section_checks):
                if row_y + row_h > PH - BM:
                    row_y = page_break()

                min_w_req = max(0.3 * sc["h"], 250)
                min_w_ok = sc["b"] >= min_w_req - 0.5
                bg = STRIPE_BG if ri % 2 else None

                top_rho = sc.get("top_rho", {})
                bottom_rho = sc.get("bottom_rho", {})
                rho_t = top_rho.get("rho")
                rho_b = bottom_rho.get("rho")
                rho_min = top_rho.get("rho_min")
                rho_max = top_rho.get("rho_max")

                if rho_t is not None and rho_b is not None:
                    rho_prov = (f'Top: {rho_t:.5f}\nBottom: {rho_b:.5f}'
                                f'\n({_badge(sc["rho_passed"])})')
                else:
                    rho_prov = _badge(sc["rho_passed"])

                if rho_min is not None and rho_max is not None:
                    rho_bnbc = f'{rho_min:.5f} ~\n{rho_max:.4f}'
                else:
                    rho_bnbc = "-"

                bars_prov = (f'Top: {sc["top_bars"]}\nBottom: {sc["bottom_bars"]}'
                             f'\n({_badge(sc["bars_passed"])})')
                bars_bnbc = "2 each\nface"

                x = LM
                _rect_text(pdf, x, row_y, cw[0], row_h,
                           sc["section"], 6.5, False, bg, DATA_CLR,
                           vcenter=True)
                x += cw[0]

                if is_imrf:
                    _rect_text(pdf, x, row_y, cw[1], row_h,
                               f'{sc["s"]} ({_badge(sc["spacing_passed"])})',
                               6, False, bg, DATA_CLR, vcenter=True)
                    x += cw[1]
                    _rect_text(pdf, x, row_y, cw[2], row_h,
                               f'{sc["s_max"]:.0f}', 6, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[2]
                    _rect_text(pdf, x, row_y, cw[3], row_h,
                               rho_prov, 5.5, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[3]
                    _rect_text(pdf, x, row_y, cw[4], row_h,
                               rho_bnbc, 5.5, False, bg, DATA_CLR,
                               vcenter=True)
                else:
                    _rect_text(pdf, x, row_y, cw[1], row_h,
                               f'{sc["b"]:.0f} ({_badge(min_w_ok)})',
                               6, False, bg, DATA_CLR, vcenter=True)
                    x += cw[1]
                    _rect_text(pdf, x, row_y, cw[2], row_h,
                               f'{min_w_req:.0f}', 6, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[2]
                    _rect_text(pdf, x, row_y, cw[3], row_h,
                               f'{sc["s"]} ({_badge(sc["spacing_passed"])})',
                               6, False, bg, DATA_CLR, vcenter=True)
                    x += cw[3]
                    _rect_text(pdf, x, row_y, cw[4], row_h,
                               f'{sc["s_max"]:.0f}', 6, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[4]
                    _rect_text(pdf, x, row_y, cw[5], row_h,
                               bars_prov, 5.5, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[5]
                    _rect_text(pdf, x, row_y, cw[6], row_h,
                               bars_bnbc, 5.5, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[6]
                    _rect_text(pdf, x, row_y, cw[7], row_h,
                               rho_prov, 5.5, False, bg, DATA_CLR,
                               vcenter=True)
                    x += cw[7]
                    _rect_text(pdf, x, row_y, cw[8], row_h,
                               rho_bnbc, 5.5, False, bg, DATA_CLR,
                               vcenter=True)

                row_y += row_h

            pdf.set_y(row_y + 3)

        # =============================================================
        #  SPAN CHECKS TABLE
        # =============================================================
        n_spans = max(len(ln_checks), len(fst_checks), len(zl_checks), len(flexural))
        if n_spans > 0:
            pdf.set_font("Helvetica", "BU", 8)
            pdf.set_text_color(*DATA_CLR)
            pdf.cell(0, 5, "Span Checks:", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

            if is_imrf:
                sc_cw = [13, 28, 16, 36, 24, 28, 18, 114]
                span_cols = ["span", "fst_p", "fst_b", "hoop_p", "hoop_b",
                             "mid_p", "mid_b", "moment"]
            else:
                sc_cw = [13, 24, 16, 24, 14, 32, 20, 24, 16, 94]
                span_cols = ["span", "ln_p", "ln_b", "fst_p", "fst_b",
                             "hoop_p", "hoop_b", "mid_p", "mid_b", "moment"]

            hdr1_h = 16
            sub_h = 7
            row_h = 10
            moment_row_h = 20

            y0 = ensure_space(hdr1_h + sub_h + 12)
            pdf.set_draw_color(*LINE_CLR)

            # ---- header row 1 ----
            x = LM
            _rect_text(pdf, x, y0, sc_cw[0], hdr1_h + sub_h,
                       "Span", 7, True, HDR_BG, HDR_CLR, vcenter=True)
            x += sc_cw[0]

            if not is_imrf:
                _rect_text(pdf, x, y0, sc_cw[1] + sc_cw[2], hdr1_h,
                           "Min Clear Span\nCheck\n(BNBC 8.3.4.1(b))",
                           6.5, True, HDR_BG, HDR_CLR, vcenter=True)
                x += sc_cw[1] + sc_cw[2]

            fst_idx = 1 if is_imrf else 3
            fst_ref = "8.3.10.4(b)" if is_imrf else "8.3.4.3(b)"
            _rect_text(pdf, x, y0, sc_cw[fst_idx] + sc_cw[fst_idx + 1], hdr1_h,
                       f"Max 1st Stirrup\nPosition from\nSupport\n(BNBC {fst_ref})",
                       6.5, True, HDR_BG, HDR_CLR, vcenter=True)
            x += sc_cw[fst_idx] + sc_cw[fst_idx + 1]

            hoop_idx = fst_idx + 2
            hoop_ref = "8.3.10.4(b)" if is_imrf else "8.3.4.3(a)"
            _rect_text(pdf, x, y0, sc_cw[hoop_idx] + sc_cw[hoop_idx + 1], hdr1_h,
                       f"Hoop Bar at\nSupport Length\nLeft & Right\n(BNBC {hoop_ref})",
                       6.5, True, HDR_BG, HDR_CLR, vcenter=True)
            x += sc_cw[hoop_idx] + sc_cw[hoop_idx + 1]

            mid_idx = hoop_idx + 2
            mid_ref = "8.3.10.4(c)" if is_imrf else "8.3.4.3(c)"
            _rect_text(pdf, x, y0, sc_cw[mid_idx] + sc_cw[mid_idx + 1], hdr1_h,
                       f"Max MidSpan\nStirrup Spacing\n(BNBC {mid_ref})",
                       6.5, True, HDR_BG, HDR_CLR, vcenter=True)
            x += sc_cw[mid_idx] + sc_cw[mid_idx + 1]

            moment_idx = mid_idx + 2
            moment_ref = "8.3.10.4(a)" if is_imrf else "8.3.4.2(a)"
            _rect_text(pdf, x, y0, sc_cw[moment_idx], hdr1_h + sub_h,
                       f"Moment Strength Check\n(BNBC {moment_ref})",
                       7, True, HDR_BG, HDR_CLR, vcenter=True)

            # ---- header row 2 (sub-headers with units) ----
            y1 = y0 + hdr1_h
            x = LM + sc_cw[0]
            start_col = 1
            end_col = moment_idx
            for ci in range(start_col, end_col, 2):
                _rect_text(pdf, x, y1, sc_cw[ci], sub_h,
                           "Provided\n(mm)", 5, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += sc_cw[ci]
                _rect_text(pdf, x, y1, sc_cw[ci + 1], sub_h,
                           "As Per BNBC\n(mm)", 4.5, True, HDR_BG, HDR_CLR,
                           vcenter=True)
                x += sc_cw[ci + 1]

            # ---- data rows ----
            row_y = y0 + hdr1_h + sub_h
            for si in range(n_spans):
                ln_c = ln_checks[si] if si < len(ln_checks) else None
                fs_c = fst_checks[si] if si < len(fst_checks) else None
                zl_c = zl_checks[si] if si < len(zl_checks) else None
                fl_c = flexural[si] if si < len(flexural) else None

                has_moment = fl_c and fl_c.get("moment_checks")
                rh = moment_row_h if has_moment else row_h

                if row_y + rh > PH - BM:
                    row_y = page_break()

                bg = STRIPE_BG if si % 2 else None

                x = LM
                _rect_text(pdf, x, row_y, sc_cw[0], rh,
                           str(si + 1), 6.5, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[0]

                # Min Clear Span (SMRF only)
                if not is_imrf:
                    if ln_c and ln_c["passed"] is not None:
                        ln_prov = f'{ln_c["ln"]:.0f}\n({_badge(ln_c["passed"])})'
                        ln_bnbc = f'{ln_c["min_ln"]:.0f}'
                    elif ln_c:
                        ln_prov = f'{ln_c["ln"]:.0f}'
                        ln_bnbc = "-"
                    else:
                        ln_prov = ln_bnbc = "-"
                    _rect_text(pdf, x, row_y, sc_cw[1], rh,
                               ln_prov, 6, False, bg, DATA_CLR,
                               vcenter=True)
                    x += sc_cw[1]
                    _rect_text(pdf, x, row_y, sc_cw[2], rh,
                               ln_bnbc, 6, False, bg, DATA_CLR,
                               vcenter=True)
                    x += sc_cw[2]

                # 1st stirrup
                if fs_c:
                    fs_prov = (f'Left: {fs_c["gap_left"]:.0f}'
                               f'\nRight: {fs_c["gap_right"]:.0f}'
                               f'\n({_badge(fs_c["passed"])})')
                    fs_bnbc = "50"
                else:
                    fs_prov = "-"
                    fs_bnbc = "-"
                _rect_text(pdf, x, row_y, sc_cw[fst_idx], rh,
                           fs_prov, 5.5, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[fst_idx]
                _rect_text(pdf, x, row_y, sc_cw[fst_idx + 1], rh,
                           fs_bnbc, 6, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[fst_idx + 1]

                # Hoop zone length
                if zl_c:
                    zl_l = (f'{zl_c["left_zone_length"]:.0f}'
                            if zl_c["left_zone_length"] is not None else "N/A")
                    zl_r = (f'{zl_c["right_zone_length"]:.0f}'
                            if zl_c["right_zone_length"] is not None else "N/A")
                    zl_prov = (f'Left: {zl_l}\nRight: {zl_r}'
                               f'\n({_badge(zl_c["passed"])})')
                    zl_bnbc = f'{zl_c["min_length"]:.0f}'
                else:
                    zl_prov = "-"
                    zl_bnbc = "-"
                _rect_text(pdf, x, row_y, sc_cw[hoop_idx], rh,
                           zl_prov, 5.5, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[hoop_idx]
                _rect_text(pdf, x, row_y, sc_cw[hoop_idx + 1], rh,
                           zl_bnbc, 6, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[hoop_idx + 1]

                # MidSpan stirrup spacing
                if midspan and midspan["passed"] is not None:
                    ms_prov = f'{midspan["s"]}\n({_badge(midspan["passed"])})'
                    ms_bnbc = f'{midspan["s_max"]:.0f}'
                else:
                    ms_prov = "-"
                    ms_bnbc = "-"
                _rect_text(pdf, x, row_y, sc_cw[mid_idx], rh,
                           ms_prov, 6, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[mid_idx]
                _rect_text(pdf, x, row_y, sc_cw[mid_idx + 1], rh,
                           ms_bnbc, 6, False, bg, DATA_CLR,
                           vcenter=True)
                x += sc_cw[mid_idx + 1]

                # Moment strength
                if has_moment:
                    mc = fl_c["moment_checks"]
                    all_ok = all(c["passed"] for c in mc)
                    lines = [_badge(all_ok)]
                    for c in mc:
                        pv = (f'{c["provided"]:.0f}'
                              if c["provided"] is not None else "?")
                        rq = (f'{c["required"]:.0f}'
                              if c["required"] is not None else "?")
                        short = c["label"]
                        short = short.replace("along span ", "")
                        short = short.replace("joint ", "")
                        lines.append(f'{short}: {pv} >= {rq} sq. mm')
                    moment_txt = "\n".join(lines)
                else:
                    moment_txt = "-"
                _rect_text(pdf, x, row_y, sc_cw[moment_idx], rh,
                           moment_txt, 5, False, bg, DATA_CLR,
                           vcenter=True)

                row_y += rh

            pdf.set_y(row_y + 3)

        hline()
        pdf.ln(1)

    # ================================================================
    #  PARSING NOTES
    # ================================================================
    err_list = [e for e in errors
                if "No stirrup/tie polyline" not in e.get("issue", "")]
    if err_list:
        ensure_space(12)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*DATA_CLR)
        pdf.cell(0, 5, f"Parsing Notes ({len(err_list)})",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 5.5)
        pdf.set_text_color(140, 60, 60)
        for err in err_list[:60]:
            ensure_space(4)
            parts = []
            if "beam" in err:
                short = _short_title(err["beam"])
                parts.append(short)
            if "section" in err:
                parts.append(f'sec {err["section"]}')
            parts.append(err.get("issue", ""))
            pdf.cell(0, 3.5, "- " + "  |  ".join(parts),
                     new_x="LMARGIN", new_y="NEXT")

    return pdf.output()


def main():
    parser = argparse.ArgumentParser(
        description="Generate beam reinforcement check PDF (BNBC format).")
    parser.add_argument("dxf", help="Path to the DXF file")
    parser.add_argument("--fc", type=float, default=28.0,
                        help="Concrete strength f'c in MPa (default: 28)")
    parser.add_argument("--fy", type=float, default=420.0,
                        help="Steel yield strength fy in MPa (default: 420)")
    parser.add_argument("--frame", choices=["smrf", "imrf"], default="smrf",
                        help="Framing system: smrf or imrf (default: smrf)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output PDF path (default: <dxf_name>_report.pdf)")
    args = parser.parse_args()

    if not os.path.isfile(args.dxf):
        print(f"Error: file not found: {args.dxf}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.dxf} ...")
    checker = BeamStirrupChecker(args.dxf, fc_mpa=args.fc, fy_mpa=args.fy,
                                 frame_system=args.frame)
    with contextlib.redirect_stdout(io.StringIO()):
        checker.run()

    print(f"Found {len(checker.elevations)} beams. Generating PDF ...")
    pdf_bytes = generate_pdf(args.dxf, checker.elevations, checker.errors,
                             args.fc, args.fy, frame_system=args.frame)

    out_path = args.output
    if out_path is None:
        out_path = f"{args.frame}_beam_report.pdf"

    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"Report saved to {out_path}  ({len(pdf_bytes) // 1024} KB)")


if __name__ == "__main__":
    main()
