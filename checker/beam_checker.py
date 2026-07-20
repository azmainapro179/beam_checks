import re
import math
import ezdxf
from collections import defaultdict

try:
    from .parser import strip_mtext_codes, get_vertices, get_line_points, distance
except ImportError:
    from parser import strip_mtext_codes, get_vertices, get_line_points, distance


SECTION_LABEL_RE = re.compile(r'SECTION\s+([0-9A-Za-z]+-[0-9A-Za-z]+)', re.IGNORECASE)
BEAM_LABEL_RE = re.compile(r'([A-Za-z]+-?\d+[A-Za-z]?)\s*\(\s*(\d+)\s*[xX]\s*(\d+)\s*\)')

# AutoCAD renders a diameter symbol either as a unicode glyph (Ø/Φ/∅) or as
# the "%%c"/"%%C" control sequence - accept both so parsing isn't tied to
# how a particular drawing happened to encode it.
DIAMETER_SYMBOL = r'(?:[ØΦ∅]|%%[Cc])'
# "2-Ø20mm" / "1-%%C16mm" / "1-16mm" -> (count, diameter)
COUNT_DIAMETER_RE = re.compile(rf'(\d+)\s*[-xX]\s*{DIAMETER_SYMBOL}?\s*(\d+)\s*(?:mm)?', re.IGNORECASE)
# "2T20" / "3Y16" / "4D12" -> (count, diameter)
COUNT_LETTER_DIAMETER_RE = re.compile(r'(\d+)\s*[TDY]\s*(\d+)\s*(?:mm)?', re.IGNORECASE)
# "2-#5" -> (count, US bar size)
COUNT_US_BAR_RE = re.compile(r'(\d+)\s*[-xX]\s*#\s*(\d+)', re.IGNORECASE)
# "Ø10mm" / "%%C10mm" -> diameter
DIAMETER_RE = re.compile(rf'{DIAMETER_SYMBOL}\s*(\d+)\s*mm', re.IGNORECASE)
# "@ 75mm" -> spacing
SPACING_RE = re.compile(r'@\s*(\d+)\s*mm', re.IGNORECASE)

# Keywords used to classify a longitudinal-bar callout by its position in the
# beam depth. Checked in this order against the lower-cased callout text;
# "bottom"/"top" also match abbreviations like "St.Top" or "Ext.Bottom".
LONGITUDINAL_POSITION_KEYWORDS = (("bottom", "bottom"), ("top", "top"), ("mid", "middle"))

# Section-cut end bubbles are short tokens like "1a" / "2b" drawn at both ends
# of a cut line. A detail titled "SECTION 1a-1a" is cut at the vertical line
# whose end bubbles both read "1a"; locating those bubbles tells us where along
# the beam the section is taken (support zone vs mid-span).
CUT_BUBBLE_RE = re.compile(r'^\s*([0-9A-Za-z]+(?:[._-]?[0-9A-Za-z]+)?)\s*$')


BAR_AREA_MM2 = {
    6: 32, 10: 71, 12: 129, 16: 200, 20: 284,
    22: 387, 25: 510, 29: 645, 32: 819, 36: 1006,
    38: 1140, 43: 1452, 50: 2027, 57: 2581, 64: 3167,
}

US_BAR_SIZE_TO_DIAMETER = {
    2: 6, 3: 10, 4: 12, 5: 16, 6: 20, 7: 22, 8: 25,
    9: 29, 10: 32, 11: 36, 12: 38, 14: 43, 16: 50, 18: 57,
}


def is_dim_line_layer(layer):
    """True for the 'Dim Line' layer family (case-insensitive) - the layer the
    individual stirrups of a longitudinal beam elevation are drawn on."""
    if not layer:
        return False
    return layer.strip().lower().startswith("dim line")


def is_line_layer(layer):
    """True for the 'LINE' layer family (case-insensitive) - the layer the
    section-cut label bubbles (e.g. '1a', '1b') are drawn on."""
    if not layer:
        return False
    return layer.strip().lower().startswith("line")

POINT_TOL = 30.0    # mm tolerance for "touches" tests (drafted leader tips aren't always pixel-exact)
LINE_TOL = 0.5      # mm tolerance for grouping points onto the same horizontal/vertical line
BBOX_MARGIN = 50.0   # mm margin when searching for content inside a section's bbox
MAX_LABEL_DIST = 1500.0   # mm - entities farther than this from every label belong to no section


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def point_to_segment_distance(p, a, b):
    """Shortest distance from point p to the segment a-b (2D)."""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return distance(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    proj = (ax + t * dx, ay + t * dy)
    return distance(p, proj)


def polyline_edges(points, closed):
    """Yield consecutive (a, b) edges of a polyline, wrapping around if closed."""
    n = len(points)
    for i in range(n - 1):
        yield points[i], points[i + 1]
    if closed and n > 1:
        yield points[-1], points[0]


def entity_points(e):
    """Return the list of 2D vertices for a LINE or LWPOLYLINE entity."""
    if e.dxftype() == "LINE":
        return [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
    if e.dxftype() == "LWPOLYLINE":
        return get_vertices(e)
    return []


def multileader_tip_points(ml):
    """
    Return every leader-line vertex of a MULTILEADER entity. These are the
    points closest to the annotated object (the arrowhead end), as opposed to
    `last_leader_point` which sits on the landing line next to the text.
    """
    pts = []
    for leader in ml.context.leaders:
        for line in leader.lines:
            for v in line.vertices:
                pts.append((v.x, v.y))
    return pts


def parse_count_diameter_pairs(text):
    """Return [(count, diameter)] parsed from common bar notations."""
    pairs = []
    seen = set()

    def add(count, diameter):
        key = (int(count), int(diameter))
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    for count, dia in COUNT_DIAMETER_RE.findall(text):
        add(count, dia)
    for count, dia in COUNT_LETTER_DIAMETER_RE.findall(text):
        add(count, dia)
    for count, size in COUNT_US_BAR_RE.findall(text):
        dia = US_BAR_SIZE_TO_DIAMETER.get(int(size))
        if dia is not None:
            add(count, dia)
    return pairs


def bar_area_mm2(diameter):
    """Rebar area in mm², using the provided table and falling back to πd²/4."""
    if diameter in BAR_AREA_MM2:
        return BAR_AREA_MM2[diameter]
    return math.pi * diameter * diameter / 4


# ---------------------------------------------------------------------------
# Longitudinal beam elevation parser
# ---------------------------------------------------------------------------

class BeamElevationParser:
    """
    Parses longitudinal beam elevation drawings (e.g. "LONGITUDINAL SECTION
    OF FLOOR BEAM-FB1(300x600): ALONG GRID-1") from the BEAM SEC layer and
    extracts every property drawn on them:

      - section size (b x h), read from the beam label itself
      - support/span layout, from the vertical support-face lines
      - stirrup diameter + spacing per zone along the beam, from rotated
        DIMENSION objects carrying override text like "Ø10mm @ 75mm c/c"
      - support-width / clear-span lengths, from the plain rotated
        DIMENSION objects below the stirrup-zone row
      - top/middle/bottom longitudinal bar callouts (count + diameter),
        from MULTILEADER objects pointing at the beam

    Pipeline (called via run()):
      1. collect_vertical_lines() - gather vertical LINE/2-point-LWPOLYLINE
                                     segments on BEAM SEC, merging segments
                                     that are close together in x AND y into
                                     one logical support line (a support face
                                     is often split by a gap where the beam
                                     hides the column behind it)
      2. find_beam_labels()       - locate "BEAM...(bxh)" labels
      3. build_blocks()           - cluster logical lines into one group per
                                     physical beam drawing (same y-row,
                                     contiguous in x) and match each group to
                                     the nearest beam label by x position
      4. extract_beam()           - per block: find the beam's top/bottom
                                     edge y (the gap in a support line whose
                                     size equals h), then pull stirrup zones,
                                     span dimensions and rebar callouts from
                                     the region directly below/within it
    """

    LINE_MERGE_X_TOL = 0.5     # mm tolerance for grouping segments onto the same vertical line
    MAX_MERGE_Y_GAP = 2000.0   # mm; segments farther apart than this (different floors) aren't merged
    ROW_TOL = 5.0              # mm tolerance for grouping dimension lines into the same row
    BLOCK_GAP_THRESHOLD = 5000.0   # mm; x-gap beyond which same-row lines belong to a different drawing
    LABEL_X_TOL = 200.0        # mm; how close a label's (left-justified) x must be to a block's leftmost line
    EDGE_GAP_TOL = 15.0        # mm tolerance when matching a support-line gap to the beam height h
    LEADER_BBOX_MARGIN = 50.0  # mm margin when matching multileader tips to a beam's bbox
    MIN_SPAN_WIDTH = 800.0     # mm; a face-to-face interval narrower than this is a column, not a span
    FIRST_STIRRUP_MIN = 50.0   # mm; the first stirrup must be at least this far from each support face
    FIRST_STIRRUP_TOL = 1.0    # mm tolerance on the 50mm first-stirrup rule

    def __init__(self, doc, msp):
        self.doc = doc
        self.msp = msp

        self.logical_lines = []   # [{x, ymin, ymax, segments}]
        self.beam_labels = []     # [{entity, x, y, name, b, h}]
        self.blocks = []          # [{label, line_idxs}]

        self.beams = []     # one parsed-property dict per block
        self.errors = []

    # ------------------------------------------------------------------
    # Step 1 - Collect vertical support lines on BEAM SEC
    # ------------------------------------------------------------------

    def collect_vertical_lines(self):
        """
        Gather every vertical LINE/2-point-LWPOLYLINE on BEAM SEC. Segments
        are first grouped by x, then within each x-group merged into a
        logical line only while consecutive segments are within
        MAX_MERGE_Y_GAP of each other - this merges a support face split by
        the beam hiding the column behind it, without merging the same grid
        line's column across unrelated floors/drawings far away in y.
        """
        raw = []
        for e in self.msp:
            if getattr(e.dxf, "layer", None) != "BEAM SEC":
                continue
            if e.dxftype() not in ("LINE", "LWPOLYLINE"):
                continue
            if e.dxftype() == "LWPOLYLINE" and len(e.get_points()) != 2:
                continue
            pp = get_line_points(e)
            if pp is None:
                continue
            (x1, y1), (x2, y2) = pp
            if abs(x1 - x2) > 0.01:
                continue   # not vertical
            raw.append((x1, y1, y2, e))

        xgroups = defaultdict(list)
        for x, y1, y2, e in raw:
            key = next((k for k in xgroups if abs(k - x) <= self.LINE_MERGE_X_TOL), x)
            xgroups[key].append((y1, y2, e))

        for x, segs in xgroups.items():
            segs.sort(key=lambda s: s[0])
            cur = {"x": x, "ymin": segs[0][0], "ymax": segs[0][1], "segments": [segs[0][2]]}
            for y1, y2, e in segs[1:]:
                if y1 - cur["ymax"] <= self.MAX_MERGE_Y_GAP:
                    cur["ymin"] = min(cur["ymin"], y1)
                    cur["ymax"] = max(cur["ymax"], y2)
                    cur["segments"].append(e)
                else:
                    self.logical_lines.append(cur)
                    cur = {"x": x, "ymin": y1, "ymax": y2, "segments": [e]}
            self.logical_lines.append(cur)

        print(f"Found {len(self.logical_lines)} logical vertical lines on BEAM SEC (elevations).")

    # ------------------------------------------------------------------
    # Step 2 - Find beam labels (text containing BEAM and a bxh dimension)
    # ------------------------------------------------------------------

    def find_beam_labels(self):
        """
        A beam label is any TEXT/MTEXT containing the word "BEAM" together
        with a "(bxh)" pattern, e.g. "LONGITUDINAL SECTION OF FLOOR
        BEAM-FB1(300x600): ALONG GRID-1". The label used for the beam is the
        *entire* cleaned MTEXT (not just the "FB1" mark), as that's the only
        thing in the drawing that uniquely identifies this particular
        elevation; "id"/"b"/"h" are pulled out of it for convenience.
        """
        for e in self.msp:
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            raw = e.dxf.text
            if "beam" not in raw.lower():
                continue
            match = BEAM_LABEL_RE.search(raw)
            if not match:
                continue
            beam_id, b, h = match.group(1), int(match.group(2)), int(match.group(3))
            x, y, _ = e.dxf.insert
            self.beam_labels.append({
                "entity": e, "x": x, "y": y,
                "label": strip_mtext_codes(raw), "id": beam_id, "b": b, "h": h,
            })

        print(f"Found {len(self.beam_labels)} beam elevation labels.")

    # ------------------------------------------------------------------
    # Step 3 - Cluster logical lines into per-drawing blocks and match labels
    # ------------------------------------------------------------------

    def build_blocks(self):
        """
        Group logical lines that share the same row (close ymin) and are
        contiguous in x (gap <= BLOCK_GAP_THRESHOLD) into one block per
        physical beam drawing - two side-by-side drawings can sit on the
        exact same row. Each block is then matched to the beam label that is
        below it and whose (left-justified) x is closest to the block's
        leftmost line.
        """
        rows = defaultdict(list)
        for idx, line in enumerate(self.logical_lines):
            rows[round(line["ymin"] / self.ROW_TOL)].append(idx)

        raw_blocks = []
        for idxs in rows.values():
            idxs.sort(key=lambda i: self.logical_lines[i]["x"])
            cluster = [idxs[0]]
            for i in idxs[1:]:
                if self.logical_lines[i]["x"] - self.logical_lines[cluster[-1]]["x"] > self.BLOCK_GAP_THRESHOLD:
                    raw_blocks.append(cluster)
                    cluster = []
                cluster.append(i)
            raw_blocks.append(cluster)

        for line_idxs in raw_blocks:
            leftmost_x = self.logical_lines[line_idxs[0]]["x"]
            ymin_block = self.logical_lines[line_idxs[0]]["ymin"]

            best_lbl, best_ygap = None, None
            for lbl in self.beam_labels:
                if lbl["y"] >= ymin_block:
                    continue   # label must lie below the block
                if abs(lbl["x"] - leftmost_x) > self.LABEL_X_TOL:
                    continue
                ygap = ymin_block - lbl["y"]
                if best_ygap is None or ygap < best_ygap:
                    best_ygap, best_lbl = ygap, lbl

            if best_lbl is not None:
                self.blocks.append({"beam_label": best_lbl, "line_idxs": line_idxs})

        print(f"Found {len(self.blocks)} beam elevation drawings.")
        for block in self.blocks:
            lbl = block["beam_label"]
            print(f"  {lbl['id']} ({lbl['b']}x{lbl['h']}) @ ({lbl['x']:.1f},{lbl['y']:.1f}): "
                  f"{len(block['line_idxs'])} support lines.")

    # ------------------------------------------------------------------
    # Step 4 - Extract every property for one beam block
    # ------------------------------------------------------------------

    def _find_beam_edges(self, line, h):
        """
        Return (bottom_y, top_y) of the beam at this support line: the gap
        between two of its segments whose size matches h (the column is
        hidden behind the beam over that span, splitting the drawn line).
        None if no such gap exists on this particular line.
        """
        pts = sorted((get_line_points(seg)[0][1], get_line_points(seg)[1][1]) for seg in line["segments"])
        for i in range(len(pts) - 1):
            bottom_of_lower, top_of_upper = pts[i][1], pts[i + 1][0]
            if abs((top_of_upper - bottom_of_lower) - h) <= self.EDGE_GAP_TOL:
                return bottom_of_lower, top_of_upper
        return None, None

    def _column_faces(self, lines, h):
        """
        Return the sorted x positions of the genuine support/column faces:
        the vertical lines that carry a beam-hiding gap of size h. A bar
        cut-off line drawn inside the beam has no such gap, so this filters
        the rebar lines out of the raw support-line set.
        """
        return sorted(line["x"] for line in lines if self._find_beam_edges(line, h)[0] is not None)

    def _spans_from_faces(self, faces):
        """Face-to-face intervals wide enough to be a span (not a column)."""
        return [(faces[i], faces[i + 1]) for i in range(len(faces) - 1)
                if faces[i + 1] - faces[i] >= self.MIN_SPAN_WIDTH]

    def _collect_stirrup_xs(self, minx, maxx, bottom_y, top_y):
        """
        x positions of the individual stirrups - the vertical marks on the
        'Dim Line' layer family - that fall within the beam's span and depth.
        """
        ylo, yhi = min(bottom_y, top_y) - 400.0, max(bottom_y, top_y) + 400.0
        xs = []
        for e in self.msp:
            if not is_dim_line_layer(getattr(e.dxf, "layer", None)):
                continue
            t = e.dxftype()
            if t == "LINE":
                pp = get_line_points(e)
                if pp is None:
                    continue
                (x1, y1), (x2, y2) = pp
                if abs(x1 - x2) > 1.0:
                    continue   # not a vertical stirrup mark
                x, ymid = x1, (y1 + y2) / 2
            elif t == "INSERT":
                x, ymid = e.dxf.insert.x, e.dxf.insert.y
            else:
                continue
            if minx - 5.0 <= x <= maxx + 5.0 and ylo <= ymid <= yhi:
                xs.append(x)
        return sorted(xs)

    def _check_first_stirrups(self, spans, stirrup_xs):
        """
        Per span, verify the first stirrup from each bounding support face is
        at least FIRST_STIRRUP_MIN away. Column footprints contain no stirrups
        and so are skipped automatically.
        """
        checks = []
        for x0, x1 in spans:
            inside = [x for x in stirrup_xs if x0 - 0.5 <= x <= x1 + 0.5]
            if not inside:
                continue
            gap_left = min(inside) - x0
            gap_right = x1 - max(inside)
            limit = self.FIRST_STIRRUP_MIN - self.FIRST_STIRRUP_TOL
            checks.append({
                "span_start": x0, "span_end": x1,
                "gap_left": gap_left, "gap_right": gap_right,
                "passed": gap_left >= limit and gap_right >= limit,
            })
        return checks

    def _collect_horizontal_dims(self, minx, maxx, bottom_y, depth):
        """Return [(y, xlo, xhi, length, text)] for DIMENSION objects under the beam's span."""
        out = []
        for e in self.msp:
            if e.dxftype() != "DIMENSION":
                continue
            p2, p3 = e.dxf.defpoint2, e.dxf.defpoint3
            if abs(p2[1] - p3[1]) > 0.5:
                continue   # not horizontal
            y = p2[1]
            if not (bottom_y - depth <= y <= bottom_y + self.ROW_TOL):
                continue
            xlo, xhi = min(p2[0], p3[0]), max(p2[0], p3[0])
            if not (minx - self.ROW_TOL <= xlo and xhi <= maxx + self.ROW_TOL):
                continue
            out.append((y, xlo, xhi, e.get_measurement(), strip_mtext_codes(e.dxf.text)))
        return out

    def _collect_longitudinal_bars(self, minx, maxx, bottom_y, top_y):
        """Return [{position, bars, text, x, y}] for MULTILEADER callouts pointing into the beam's bbox."""
        m = self.LEADER_BBOX_MARGIN
        bars = []
        for e in self.msp:
            if e.dxftype() != "MULTILEADER":
                continue
            tips = multileader_tip_points(e)
            tips_in_bbox = [p for p in tips
                             if minx - m <= p[0] <= maxx + m and bottom_y - m <= p[1] <= top_y + m]
            if not tips_in_bbox:
                continue
            try:
                text = strip_mtext_codes(e.get_mtext_content())
            except Exception:
                continue
            pairs = parse_count_diameter_pairs(text)
            if not pairs:
                continue

            low = text.lower()
            position = next((pos for kw, pos in LONGITUDINAL_POSITION_KEYWORDS if kw in low), None)
            if position is None:
                # No keyword in the callout text - fall back to where the
                # leader points: split the beam depth into thirds.
                tip_y = tips_in_bbox[0][1]
                third = (top_y - bottom_y) / 3
                if tip_y >= top_y - third:
                    position = "top"
                elif tip_y <= bottom_y + third:
                    position = "bottom"
                else:
                    position = "middle"

            bars.append({
                "position": position,
                "bars": [{"count": c, "diameter": d} for c, d in pairs],
                "text": text,
                "x": tips_in_bbox[0][0], "y": tips_in_bbox[0][1],
            })
        return bars

    def extract_beam(self, block):
        lbl = block["beam_label"]
        line_idxs = block["line_idxs"]
        lines = [self.logical_lines[i] for i in line_idxs]
        h = lbl["h"]

        bottom_y = top_y = None
        for line in lines:
            by, ty = self._find_beam_edges(line, h)
            if by is not None:
                bottom_y, top_y = by, ty
                break

        if bottom_y is None:
            self.errors.append({"beam": lbl["label"], "issue": "Could not locate beam top/bottom edge"})
            return None

        minx = min(line["x"] for line in lines)
        maxx = max(line["x"] for line in lines)
        support_positions = sorted(line["x"] for line in lines)

        # True column faces (h-gap lines) and the spans between them, used both
        # for the first-stirrup check here and for locating cross-section cuts.
        column_faces = self._column_faces(lines, h)
        spans = self._spans_from_faces(column_faces)
        stirrup_xs = self._collect_stirrup_xs(minx, maxx, bottom_y, top_y)
        first_stirrup_checks = self._check_first_stirrups(spans, stirrup_xs)
        for chk in first_stirrup_checks:
            if not chk["passed"]:
                self.errors.append({
                    "beam": lbl["label"],
                    "issue": "First stirrup closer than 50mm to a support face",
                    "details": (f"span x=[{chk['span_start']:.0f},{chk['span_end']:.0f}] "
                                f"left gap={chk['gap_left']:.0f}mm, right gap={chk['gap_right']:.0f}mm"),
                })

        depth = max(3 * h, 2000.0)
        dims = self._collect_horizontal_dims(minx, maxx, bottom_y, depth)

        stirrup_zones = []
        seen = set()
        for _y, xlo, xhi, length, text in dims:
            dia_m, sp_m = DIAMETER_RE.search(text), SPACING_RE.search(text)
            if not (dia_m and sp_m):
                continue
            key = (round(xlo), round(xhi))
            if key in seen:
                continue
            seen.add(key)
            stirrup_zones.append({
                "start_x": xlo, "end_x": xhi, "length": length,
                "stirrup_diameter": int(dia_m.group(1)), "stirrup_spacing": int(sp_m.group(1)),
            })
        stirrup_zones.sort(key=lambda z: z["start_x"])

        span_dimensions = []
        if dims:
            outer_y = min(y for y, *_ in dims)   # farthest row below the beam
            span_dimensions = sorted(
                ({"start_x": xlo, "end_x": xhi, "length": length}
                 for y, xlo, xhi, length, _ in dims if abs(y - outer_y) < self.ROW_TOL),
                key=lambda d: d["start_x"],
            )

        longitudinal_bars = self._collect_longitudinal_bars(minx, maxx, bottom_y, top_y)

        return {
            "label": lbl["label"], "id": lbl["id"],
            "section_width": lbl["b"], "section_depth": h,
            "support_positions": support_positions,
            "column_faces": column_faces,
            "spans": spans,
            "y_bottom": bottom_y, "y_top": top_y,
            "stirrup_zones": stirrup_zones,
            "span_dimensions": span_dimensions,
            "longitudinal_bars": longitudinal_bars,
            "first_stirrup_checks": first_stirrup_checks,
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        self.collect_vertical_lines()
        self.find_beam_labels()
        self.build_blocks()
        for block in self.blocks:
            data = self.extract_beam(block)
            if data is not None:
                self.beams.append(data)
        return self.beams


# ---------------------------------------------------------------------------
# Main checker class
# ---------------------------------------------------------------------------

class BeamStirrupChecker:
    """
    Parses beam cross-section drawings (like SECTION 1a-1a / SECTION 2a-2a)
    from a DXF file and checks the stirrup spacing rule:

        s <= min(d/4, 8 * smallest longitudinal bar diameter,
                  24 * hoop bar diameter, 300mm)

    Pipeline (called via run()):
      1. load()                 - read DXF
      2. collect_section_lines() - gather LINE/LWPOLYLINE entities on the
                                   BEAM SEC layer
      3. find_section_labels()  - locate "SECTION <id>" MTEXT labels
      4. group_sections()       - assign each BEAM SEC entity to the nearest
                                   section label below it
      5. extract_sections()     - for every section: compute h, clear cover,
                                   stirrup diameter+spacing, main bar diameter
                                   and the smallest longitudinal bar diameter
      6. check_sections()       - compute d and compare s against the rule

    run() also parses every longitudinal beam elevation drawing (BEAM-xx
    labels) found on the same BEAM SEC layer via BeamElevationParser,
    storing the result in self.elevations - see that class for details.
    """

    CUT_BUBBLE_X_MARGIN = 500.0
    CUT_BUBBLE_Y_MARGIN_MIN = 400.0
    FLEXURAL_SPAN_X_TOL = 100.0
    FLEXURAL_END_REGION_RATIO = 0.30
    FLEXURAL_END_REGION_MIN = 300.0
    FLEXURAL_END_REGION_MAX = 1200.0
    ROW_BAR_Y_TOL = 25.0

    def __init__(self, dxf_file):
        self.dxf_file = dxf_file
        self.doc = None
        self.msp = None

        self.beam_sec_entities = []   # [entity, ...] on BEAM SEC layer
        self.section_labels = []      # [{entity, x, y, name}]
        self.sections = defaultdict(list)   # label_idx -> [entity, ...]

        self.section_data = []   # one dict per section with all computed values
        self.elevations = []     # one dict per longitudinal beam elevation drawing
        self._cut_bubbles = {}   # cut-id -> [(x, y)] section-cut end bubbles
        self.errors = []
        self.results = []

    # ------------------------------------------------------------------
    # Step 1 - Load DXF
    # ------------------------------------------------------------------

    def load(self):
        self.doc = ezdxf.readfile(self.dxf_file)
        self.msp = self.doc.modelspace()
        print("DXF loaded.")

    # ------------------------------------------------------------------
    # Step 2 - Collect BEAM SEC entities
    # ------------------------------------------------------------------

    def collect_section_lines(self):
        for e in self.msp:
            if getattr(e.dxf, "layer", None) != "BEAM SEC":
                continue
            if e.dxftype() not in ("LINE", "LWPOLYLINE"):
                continue
            self.beam_sec_entities.append(e)
        print(f"Found {len(self.beam_sec_entities)} entities on BEAM SEC.")

    # ------------------------------------------------------------------
    # Step 3 - Find "SECTION <id>" labels
    # ------------------------------------------------------------------

    def find_section_labels(self):
        for e in self.msp:
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            cleaned = strip_mtext_codes(e.dxf.text).strip().lstrip("\\L").strip()
            match = SECTION_LABEL_RE.match(cleaned)
            if not match:
                continue
            x, y, _ = e.dxf.insert
            self.section_labels.append({"entity": e, "x": x, "y": y, "name": match.group(1)})

        print(f"Found {len(self.section_labels)} section labels.")
        for lbl in self.section_labels:
            print(f"  SECTION {lbl['name']} at ({lbl['x']:.1f}, {lbl['y']:.1f})")

    # ------------------------------------------------------------------
    # Step 4 - Assign BEAM SEC entities to the nearest label below them
    # ------------------------------------------------------------------

    def group_sections(self):
        for entity in self.beam_sec_entities:
            pts = entity_points(entity)
            if not pts:
                continue
            ymin = min(p[1] for p in pts)
            bottom_x = sum(p[0] for p in pts) / len(pts)

            best_idx, best_dist = -1, None
            for lbl_idx, lbl in enumerate(self.section_labels):
                if lbl["y"] >= ymin:
                    continue   # label must lie below the entity
                d = distance((bottom_x, ymin), (lbl["x"], lbl["y"]))
                if best_dist is None or d < best_dist:
                    best_dist, best_idx = d, lbl_idx

            if best_idx != -1 and best_dist <= MAX_LABEL_DIST:
                self.sections[best_idx].append(entity)

        for lbl_idx, entities in self.sections.items():
            lbl = self.section_labels[lbl_idx]
            print(f"  Section {lbl['name']}: {len(entities)} BEAM SEC entities.")

    # ------------------------------------------------------------------
    # Step 5 - Extract geometry/annotations for every section
    # ------------------------------------------------------------------

    def _section_bbox(self, entities):
        xs, ys = [], []
        for e in entities:
            for x, y in entity_points(e):
                xs.append(x)
                ys.append(y)
        return min(xs), min(ys), max(xs), max(ys)

    def _horizontal_extents(self, entities):
        """
        Return (top_y, bottom_y) of the section. bottom_y is the y of the
        lowest horizontal segment found among the BEAM SEC entities; top_y is
        the y of the highest horizontal segment if one exists distinctly above
        the bottom one, otherwise the topmost point of any BEAM SEC entity
        (covers drawings where the top edge is only implied by a hook detail).
        """
        horiz_ys = set()
        all_ys = []
        for e in entities:
            pts = entity_points(e)
            all_ys.extend(p[1] for p in pts)
            closed = e.dxftype() == "LWPOLYLINE" and e.closed
            for a, b in polyline_edges(pts, closed):
                if abs(a[1] - b[1]) <= LINE_TOL:
                    horiz_ys.add(round(a[1], 1))

        bottom_y = min(horiz_ys) if horiz_ys else min(all_ys)
        higher = [y for y in horiz_ys if y > bottom_y + LINE_TOL]
        top_y = max(higher) if higher else max(all_ys)
        return top_y, bottom_y

    def _find_cage_polyline(self, bbox):
        """Find the closed LWPOLYLINE (the stirrup/tie cage) inscribed within the section bbox."""
        minx, miny, maxx, maxy = bbox
        best = None
        for e in self.msp:
            if e.dxftype() != "LWPOLYLINE" or not e.closed:
                continue
            pts = get_vertices(e)
            if len(pts) <= 4:
                continue
            exminx, exmaxx = min(p[0] for p in pts), max(p[0] for p in pts)
            exminy, exmaxy = min(p[1] for p in pts), max(p[1] for p in pts)
            if exminx > minx and exminy > miny and exmaxx < maxx and exmaxy < maxy:
                if best is None or len(pts) > len(get_vertices(best)):
                    best = e
        return best

    def _find_circles(self, bbox):
        minx, miny, maxx, maxy = bbox
        circles = []
        for e in self.msp:
            if e.dxftype() != "CIRCLE":
                continue
            c = e.dxf.center
            if minx <= c.x <= maxx and miny <= c.y <= maxy:
                circles.append(e)
        return circles

    def _find_multileaders(self, bbox):
        minx, miny, maxx, maxy = bbox
        minx, miny = minx - BBOX_MARGIN, miny - BBOX_MARGIN
        maxx, maxy = maxx + BBOX_MARGIN, maxy + BBOX_MARGIN

        leaders = []
        for e in self.msp:
            if e.dxftype() != "MULTILEADER":
                continue
            tips = multileader_tip_points(e)
            tips_in_bbox = [p for p in tips if minx <= p[0] <= maxx and miny <= p[1] <= maxy]
            if tips_in_bbox:
                try:
                    text = strip_mtext_codes(e.get_mtext_content())
                except Exception:
                    continue
                leaders.append({"entity": e, "tips": tips_in_bbox, "text": text})
        return leaders

    def _touches_circle(self, point, circle):
        return distance(point, (circle.dxf.center.x, circle.dxf.center.y)) <= circle.dxf.radius + POINT_TOL

    def _touches_polyline(self, point, poly):
        pts = get_vertices(poly)
        return any(point_to_segment_distance(point, a, b) <= POINT_TOL
                   for a, b in polyline_edges(pts, poly.closed))

    def _main_bar_diameter(self, row_circles, leaders):
        """
        Find the leader touching one of the row's circles and return the
        diameter tied to the smallest bar count (the single middle bar, as
        opposed to the paired corner bars), e.g. '2-Ø20mm+1-Ø16mm' -> 16.
        """
        for ld in leaders:
            touches_row = any(self._touches_circle(p, c) for p in ld["tips"] for c in row_circles)
            if not touches_row:
                continue
            pairs = parse_count_diameter_pairs(ld["text"])
            if not pairs:
                continue
            return min(pairs, key=lambda p: p[0])[1]
        return None

    def _circle_rows(self, circles):
        """Cluster rebar circles into horizontal rows with drafting tolerance."""
        rows = []
        for circle in sorted(circles, key=lambda c: c.dxf.center.y):
            cy = circle.dxf.center.y
            if rows and abs(cy - rows[-1]["y"]) <= self.ROW_BAR_Y_TOL:
                rows[-1]["circles"].append(circle)
                rows[-1]["y"] = sum(c.dxf.center.y for c in rows[-1]["circles"]) / len(rows[-1]["circles"])
            else:
                rows.append({"y": cy, "circles": [circle]})
        return rows

    def _row_bar_groups(self, row_circles, leaders):
        """
        Return [{'count', 'diameter', 'area'}] from the leader(s) that describe
        a top or bottom bar row in a section drawing.
        """
        groups = []
        seen = set()
        for ld in leaders:
            touches_row = any(self._touches_circle(p, c) for p in ld["tips"] for c in row_circles)
            if not touches_row:
                continue
            pairs = parse_count_diameter_pairs(ld["text"])
            if not pairs:
                continue
            key = (ld["text"], tuple(pairs))
            if key in seen:
                continue
            seen.add(key)
            for count, dia in pairs:
                groups.append({
                    "count": count,
                    "diameter": dia,
                    "area": count * bar_area_mm2(dia),
                    "source": "leader",
                })
        return groups

    @staticmethod
    def _bar_area_total(groups):
        return sum(g["area"] for g in groups)

    def _fallback_row_bar_groups(self, row_count, diameter):
        if diameter is None:
            return []
        return [{
            "count": row_count,
            "diameter": diameter,
            "area": row_count * bar_area_mm2(diameter),
            "source": "inferred from circle count",
        }]

    def extract_sections(self):
        for lbl_idx, entities in self.sections.items():
            lbl = self.section_labels[lbl_idx]
            name = lbl["name"]

            top_y, bottom_y = self._horizontal_extents(entities)
            h = top_y - bottom_y

            minx, _, maxx, _ = self._section_bbox(entities)
            bbox = (minx, bottom_y, maxx, top_y)

            cage = self._find_cage_polyline(bbox)
            if cage is None:
                self.errors.append({"section": name, "issue": "No stirrup/tie polyline found"})
                continue

            cage_pts = get_vertices(cage)
            cage_horiz_ys = {round(a[1], 1) for a, b in polyline_edges(cage_pts, cage.closed)
                              if abs(a[1] - b[1]) <= LINE_TOL}
            if not cage_horiz_ys:
                self.errors.append({"section": name, "issue": "Stirrup polyline has no horizontal edge"})
                continue
            cage_bottom_y = min(cage_horiz_ys)
            clear_cover = cage_bottom_y - bottom_y

            circles = self._find_circles(bbox)
            if not circles:
                self.errors.append({"section": name, "issue": "No rebar circles found"})
                continue
            leaders = self._find_multileaders(bbox)

            # Count bars in the top-most and bottom-most rows (>= 2 required).
            circle_rows = self._circle_rows(circles)
            bottom_row_circles = circle_rows[0]["circles"]
            top_row_circles = circle_rows[-1]["circles"]
            bottom_bars = len(bottom_row_circles)
            top_bars = len(top_row_circles)

            # --- stirrup diameter & spacing: leader touches the cage but not a circle ---
            stirrup_dia, stirrup_spacing = None, None
            for ld in leaders:
                touches_cage = any(self._touches_polyline(p, cage) for p in ld["tips"])
                touches_circle = any(self._touches_circle(p, c) for p in ld["tips"] for c in circles)
                if touches_cage and not touches_circle:
                    dia_m = DIAMETER_RE.search(ld["text"])
                    sp_m = SPACING_RE.search(ld["text"])
                    if dia_m and sp_m:
                        stirrup_dia, stirrup_spacing = int(dia_m.group(1)), int(sp_m.group(1))
                        break

            if stirrup_dia is None or stirrup_spacing is None:
                self.errors.append({"section": name, "issue": "No stirrup diameter/spacing leader found"})
                continue

            # --- main bar diameter: leader touching a circle of the bottom-most row,
            #     falling back to the top-most row if the bottom row has no leader ---
            main_bar_dia = self._main_bar_diameter(bottom_row_circles, leaders)

            if main_bar_dia is None:
                main_bar_dia = self._main_bar_diameter(top_row_circles, leaders)

            if main_bar_dia is None:
                self.errors.append({"section": name, "issue": "No main bar diameter leader found"})
                continue

            top_bar_groups = self._row_bar_groups(top_row_circles, leaders)
            bottom_bar_groups = self._row_bar_groups(bottom_row_circles, leaders)
            if not top_bar_groups:
                top_bar_groups = self._fallback_row_bar_groups(top_bars, main_bar_dia)
            if not bottom_bar_groups:
                bottom_bar_groups = self._fallback_row_bar_groups(bottom_bars, main_bar_dia)

            # --- smallest longitudinal bar diameter: every leader touching any circle ---
            all_diameters = []
            for ld in leaders:
                touches_any_circle = any(self._touches_circle(p, c) for p in ld["tips"] for c in circles)
                if not touches_any_circle:
                    continue
                for _, dia in parse_count_diameter_pairs(ld["text"]):
                    all_diameters.append(dia)

            if not all_diameters:
                self.errors.append({"section": name, "issue": "No longitudinal bar diameters found"})
                continue
            smallest_long_dia = min(all_diameters)

            self.section_data.append({
                "name": name, "h": h, "clear_cover": clear_cover,
                "stirrup_dia": stirrup_dia, "s": stirrup_spacing,
                "main_bar_dia": main_bar_dia, "smallest_long_dia": smallest_long_dia,
                "top_bars": top_bars, "bottom_bars": bottom_bars,
                "top_bar_groups": top_bar_groups, "bottom_bar_groups": bottom_bar_groups,
                "top_bar_area": self._bar_area_total(top_bar_groups),
                "bottom_bar_area": self._bar_area_total(bottom_bar_groups),
            })

    # ------------------------------------------------------------------
    # Step 6 - Run the drawn cross-section checks (support/end sections)
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_depth(data):
        return data["h"] - data["clear_cover"] - data["stirrup_dia"] - data["main_bar_dia"] / 2

    def check_sections(self):
        """
        The separately-drawn cross-sections (e.g. SECTION 1a-1a, 2a-2a) are the
        support/end sections - they exist precisely because they must satisfy
        the full confinement spacing rule. Mid-span sections have no separate
        drawing and are handled by check_midspan().
        """
        for data in self.section_data:
            d = self._effective_depth(data)
            s_max = min(d / 4, 8 * data["smallest_long_dia"], 24 * data["stirrup_dia"], 300)

            spacing_passed = data["s"] <= s_max
            if not spacing_passed:
                self.errors.append({
                    "section": data["name"],
                    "issue": "Stirrup spacing exceeds maximum allowed (support/end section)",
                    "details": f"s={data['s']}mm, max={s_max:.1f}mm",
                })

            # At least two bars top and two bars bottom.
            bars_passed = data["top_bars"] >= 2 and data["bottom_bars"] >= 2
            if not bars_passed:
                self.errors.append({
                    "section": data["name"],
                    "issue": "Fewer than 2 bars top and/or bottom",
                    "details": f"top={data['top_bars']}, bottom={data['bottom_bars']}",
                })

            self.results.append({
                "section": data["name"], "h": data["h"], "d": d,
                "clear_cover": data["clear_cover"],
                "stirrup_dia": data["stirrup_dia"], "s": data["s"],
                "main_bar_dia": data["main_bar_dia"],
                "smallest_long_dia": data["smallest_long_dia"],
                "s_max": s_max, "spacing_passed": spacing_passed,
                "top_bars": data["top_bars"], "bottom_bars": data["bottom_bars"],
                "bars_passed": bars_passed,
                "passed": spacing_passed and bars_passed,
            })

        return self.results

    # ------------------------------------------------------------------
    # Step 7 - Mid-span spacing check (s <= d/2), located from the elevation
    # ------------------------------------------------------------------

    def _find_cut_bubbles(self):
        """
        cut-id -> [(x, y)] for every section-cut label bubble (e.g. '1a', '1b')
        drawn on the 'LINE' layer family in a longitudinal elevation.
        """
        bubbles = defaultdict(list)
        for e in self.msp:
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            if not is_line_layer(getattr(e.dxf, "layer", None)):
                continue
            cleaned = strip_mtext_codes(e.dxf.text or "")
            m = CUT_BUBBLE_RE.match(cleaned.strip())
            if not m:
                continue
            try:
                x, y, _ = e.dxf.insert
            except Exception:
                continue
            bubbles[m.group(1)].append((x, y))
        return bubbles

    def _bubbles_on_beam(self, beam):
        """[(cut_id, x)] for cut bubbles whose position lies on this beam."""
        faces = beam.get("column_faces") or []
        if not faces:
            return []
        xmin, xmax = min(faces), max(faces)
        y_margin = max(self.CUT_BUBBLE_Y_MARGIN_MIN, beam.get("section_depth", 0))
        ylo = min(beam["y_bottom"], beam["y_top"]) - y_margin
        yhi = max(beam["y_bottom"], beam["y_top"]) + y_margin
        out = []
        for cut_id, positions in self._cut_bubbles.items():
            for bx, by in positions:
                if (xmin - self.CUT_BUBBLE_X_MARGIN <= bx <= xmax + self.CUT_BUBBLE_X_MARGIN
                        and ylo <= by <= yhi):
                    out.append((cut_id, bx))
        return out

    def _unique_bubbles_on_beam(self, beam):
        """Dedup the two end bubbles drawn on the same cut line into one marker."""
        markers = []
        seen = set()
        for cut_id, x in sorted(self._bubbles_on_beam(beam), key=lambda item: item[1]):
            key = (self._normalize_cut_id(cut_id), round(x, 1))
            if key in seen:
                continue
            seen.add(key)
            markers.append({"cut_id": cut_id, "x": x})
        return markers

    @staticmethod
    def _normalize_cut_id(cut_id):
        return re.sub(r'[^0-9a-z]', '', str(cut_id).lower())

    @classmethod
    def _section_cut_id(cls, section_name):
        return cls._normalize_cut_id(section_name.split("-")[0].strip())

    def _section_by_cut_id(self, cut_id):
        target = self._normalize_cut_id(cut_id)
        for data in self.section_data:
            if self._section_cut_id(data["name"]) == target:
                return data
        return None

    def _flexural_end_region_width(self, span_width):
        if span_width <= 0:
            return 0
        return max(
            min(span_width * self.FLEXURAL_END_REGION_RATIO, self.FLEXURAL_END_REGION_MAX),
            min(self.FLEXURAL_END_REGION_MIN, span_width / 2),
        )

    def _span_region_markers(self, markers, x0, x1):
        """Markers inside one span, divided into left/middle/right zones."""
        width = x1 - x0
        if width <= 0:
            return {"left": [], "middle": [], "right": []}

        end_region = self._flexural_end_region_width(width)
        regions = {"left": [], "middle": [], "right": []}
        for marker in markers:
            x = marker["x"]
            if not (x0 - self.FLEXURAL_SPAN_X_TOL <= x <= x1 + self.FLEXURAL_SPAN_X_TOL):
                continue
            if x <= x0 + end_region:
                regions["left"].append(marker)
            elif x >= x1 - end_region:
                regions["right"].append(marker)
            else:
                regions["middle"].append(marker)
        return regions

    def _direct_section_for_region(self, region_markers, target_x):
        if not region_markers:
            return None, None
        for marker in sorted(region_markers, key=lambda item: abs(item["x"] - target_x)):
            section = self._section_by_cut_id(marker["cut_id"])
            if section is not None:
                return section, marker
        return None, min(region_markers, key=lambda item: abs(item["x"] - target_x))

    @staticmethod
    def _empty_flexural_source(note):
        return {
            "section": None,
            "cut_id": None,
            "top_area": None,
            "bottom_area": None,
            "top_groups": [],
            "bottom_groups": [],
            "note": note,
        }

    def _flexural_source_from_section(self, section, marker, note):
        return {
            "section": section["name"],
            "cut_id": marker["cut_id"] if marker else self._section_cut_id(section["name"]),
            "top_area": section["top_bar_area"],
            "bottom_area": section["bottom_bar_area"],
            "top_groups": section["top_bar_groups"],
            "bottom_groups": section["bottom_bar_groups"],
            "note": note,
        }

    def check_flexural_span_areas(self):
        """
        For each elevation span, resolve the section drawing used for Mn,l,
        Mn,r, and mid-span Mn. Negative uses top steel area; positive uses
        bottom steel area. This intentionally reports reinforcement area
        proxies, because material strengths/strain block inputs are not parsed.
        """
        for beam in self.elevations:
            markers = self._unique_bubbles_on_beam(beam)
            previous_left = None
            previous_middle = None
            summaries = []

            for index, (x0, x1) in enumerate(beam.get("spans") or [], start=1):
                regions = self._span_region_markers(markers, x0, x1)
                mid_x = (x0 + x1) / 2

                left_section, left_marker = self._direct_section_for_region(regions["left"], x0)
                if left_section is not None:
                    left = self._flexural_source_from_section(left_section, left_marker, "from span left cut")
                    previous_left = left
                elif previous_left is not None:
                    left = {**previous_left, "note": "fallback from previous span left section"}
                elif left_marker is not None:
                    left = self._empty_flexural_source(
                        f"left cut {left_marker['cut_id']} has no extracted section drawing"
                    )
                else:
                    left = self._empty_flexural_source("no left section cut found")

                right_section, right_marker = self._direct_section_for_region(regions["right"], x1)
                if right_section is not None:
                    right = self._flexural_source_from_section(right_section, right_marker, "from span right cut")
                elif left["section"] is not None:
                    right = {**left, "note": "fallback from span left section"}
                elif right_marker is not None:
                    right = self._empty_flexural_source(
                        f"right cut {right_marker['cut_id']} has no extracted section drawing"
                    )
                else:
                    right = self._empty_flexural_source("no right section cut found and no left fallback")

                middle_section, middle_marker = self._direct_section_for_region(regions["middle"], mid_x)
                if middle_section is not None:
                    middle = self._flexural_source_from_section(middle_section, middle_marker, "from span middle cut")
                    previous_middle = middle
                elif previous_middle is not None:
                    middle = {**previous_middle, "note": "fallback from previous span middle section"}
                elif middle_marker is not None:
                    middle = self._empty_flexural_source(
                        f"middle cut {middle_marker['cut_id']} has no extracted section drawing"
                    )
                else:
                    middle = self._empty_flexural_source("no middle section cut found")

                summaries.append({
                    "span_index": index,
                    "span_start": x0,
                    "span_end": x1,
                    "left": left,
                    "right": right,
                    "middle": middle,
                    "mn_l_minus_area": left["top_area"],
                    "mn_l_plus_area": left["bottom_area"],
                    "mn_r_minus_area": right["top_area"],
                    "mn_r_plus_area": right["bottom_area"],
                    "mn_mid_minus_area": middle["top_area"],
                    "mn_mid_plus_area": middle["bottom_area"],
                })

            beam["flexural_span_areas"] = summaries

    def _beam_effective_depth(self, beam):
        """
        d for a beam, taken from the drawn support section whose cut bubble
        sits on this beam (all sections of a beam share the same d). Returns
        (d, section_name) or (None, None).
        """
        on_beam = {cid for cid, _ in self._bubbles_on_beam(beam)}
        for data in self.section_data:
            if data["name"].split("-")[0].strip() in on_beam:
                return self._effective_depth(data), data["name"]
        return None, None

    def _stirrup_spacing_at(self, beam, x):
        """Stirrup spacing of the zone containing x, else the nearest zone."""
        zones = beam.get("stirrup_zones") or []
        if not zones:
            return None
        inside = [z for z in zones if z["start_x"] <= x <= z["end_x"]]
        z = (inside[0] if inside
             else min(zones, key=lambda z: abs((z["start_x"] + z["end_x"]) / 2 - x)))
        return z["stirrup_spacing"]

    def check_midspan(self):
        """
        For every beam, identify the mid-span section from the elevation: the
        'LINE'-layer cut label closest to a span mid-point (e.g. '1b'), read the
        stirrup spacing there, and require s <= d/2 (nothing else). Mid-span
        sections carry no separate cross-section drawing. Results are attached
        to each beam as beam["midspan_check"].
        """
        for beam in self.elevations:
            spans = beam.get("spans") or []
            midpoints = [(x0 + x1) / 2 for x0, x1 in spans]
            markers = self._bubbles_on_beam(beam)
            if not midpoints or not markers:
                beam["midspan_check"] = None
                continue

            # The cut label nearest any span mid-point is the mid-span section.
            cut_id, mx = min(markers, key=lambda m: min(abs(m[1] - mp) for mp in midpoints))
            s = self._stirrup_spacing_at(beam, mx)
            d, section_name = self._beam_effective_depth(beam)

            check = {"section": f"{cut_id}-{cut_id}", "x": mx, "s": s,
                     "d": d, "d_from": section_name, "s_max": None,
                     "passed": None, "note": ""}
            if d is None:
                check["note"] = "no drawn section on this beam to take d from"
            elif s is None:
                check["note"] = "no stirrup spacing found at mid-span"
            else:
                check["s_max"] = d / 2
                check["passed"] = s <= d / 2
                if not check["passed"]:
                    self.errors.append({
                        "beam": beam["label"],
                        "issue": f"Mid-span stirrup spacing exceeds d/2 (section {cut_id}-{cut_id})",
                        "details": f"s={s}mm, d/2={d/2:.1f}mm",
                    })
            beam["midspan_check"] = check
        return [b["midspan_check"] for b in self.elevations if b.get("midspan_check")]

    def check_clear_span_depth_ratio(self):
        """
        For every clear span in each longitudinal elevation, require
        ln >= 4d, where ln is the face-to-face span between column/support
        faces and d is taken from the drawn beam section linked to that
        elevation.
        """
        checks_by_beam = []
        for beam in self.elevations:
            d, section_name = self._beam_effective_depth(beam)
            checks = []
            for x0, x1 in beam.get("spans") or []:
                ln = x1 - x0
                check = {
                    "span_start": x0,
                    "span_end": x1,
                    "ln": ln,
                    "d": d,
                    "d_from": section_name,
                    "min_ln": None,
                    "passed": None,
                    "note": "",
                }
                if d is None:
                    check["note"] = "no drawn section on this beam to take d from"
                else:
                    check["min_ln"] = 4 * d
                    check["passed"] = ln >= check["min_ln"]
                    if not check["passed"]:
                        self.errors.append({
                            "beam": beam["label"],
                            "issue": "Clear span is less than 4d",
                            "details": (f"span x=[{x0:.0f},{x1:.0f}] ln={ln:.0f}mm, "
                                        f"4d={check['min_ln']:.1f}mm"),
                        })
                checks.append(check)
            beam["ln_depth_checks"] = checks
            checks_by_beam.extend(checks)
        return checks_by_beam

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse_elevations(self):
        """Parse every longitudinal beam elevation drawing on BEAM SEC into self.elevations."""
        parser = BeamElevationParser(self.doc, self.msp)
        self.elevations = parser.run()
        self.errors.extend(parser.errors)

    def run(self):
        self.load()
        self.collect_section_lines()
        self.find_section_labels()
        self.group_sections()
        self.extract_sections()
        # Elevations are parsed before the checks: the mid-span check reads the
        # spans/stirrup zones from them, and takes d from the drawn sections.
        self.parse_elevations()
        self._cut_bubbles = self._find_cut_bubbles()
        results = self.check_sections()
        self.check_clear_span_depth_ratio()
        self.check_flexural_span_areas()
        self.check_midspan()
        return results


if __name__ == "__main__":
    checker = BeamStirrupChecker("Structural Drawing.dxf")
    results = checker.run()

    print("\nResults:")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  SECTION {r['section']} (support/end): h={r['h']:.0f}mm d={r['d']:.1f}mm "
              f"cover={r['clear_cover']:.1f}mm stirrup=Ø{r['stirrup_dia']}mm@{r['s']}mm "
              f"main_bar=Ø{r['main_bar_dia']}mm smallest_bar=Ø{r['smallest_long_dia']}mm")
        print(f"      spacing: s={r['s']}mm <= s_max={r['s_max']:.1f}mm -> "
              f"{'PASS' if r['spacing_passed'] else 'FAIL'}")
        print(f"      bars: top={r['top_bars']} bottom={r['bottom_bars']} (>=2 each) -> "
              f"{'PASS' if r['bars_passed'] else 'FAIL'}  =>  SECTION {status}")

    print("\nLongitudinal beam elevations:")
    for beam in checker.elevations:
        print(f"  {beam['label']} ({beam['section_width']}x{beam['section_depth']}): supports at "
              f"{[round(x, 1) for x in beam['support_positions']]}")
        for z in beam["stirrup_zones"]:
            print(f"    stirrup zone x=[{z['start_x']:.1f}, {z['end_x']:.1f}] "
                  f"len={z['length']:.1f}mm Ø{z['stirrup_diameter']}mm@{z['stirrup_spacing']}mm c/c")
        for d in beam["span_dimensions"]:
            print(f"    span x=[{d['start_x']:.1f}, {d['end_x']:.1f}] length={d['length']:.1f}mm")
        for bar in beam["longitudinal_bars"]:
            bars_str = "+".join(f"{b['count']}-Ø{b['diameter']}mm" for b in bar["bars"])
            print(f"    {bar['position']}: {bars_str}  (\"{bar['text']}\")")
        for chk in beam["first_stirrup_checks"]:
            print(f"    first stirrup: span x=[{chk['span_start']:.0f},{chk['span_end']:.0f}] "
                  f"left={chk['gap_left']:.0f}mm right={chk['gap_right']:.0f}mm -> "
                  f"{'PASS' if chk['passed'] else 'FAIL'}")
        for chk in beam.get("ln_depth_checks", []):
            if chk["passed"] is None:
                print(f"    ln >= 4d: span x=[{chk['span_start']:.0f},{chk['span_end']:.0f}] "
                      f"ln={chk['ln']:.0f}mm -> {chk['note']}")
            else:
                print(f"    ln >= 4d: span x=[{chk['span_start']:.0f},{chk['span_end']:.0f}] "
                      f"ln={chk['ln']:.0f}mm >= 4d={chk['min_ln']:.1f}mm "
                      f"(d from {chk['d_from']}) -> {'PASS' if chk['passed'] else 'FAIL'}")
        for flex in beam.get("flexural_span_areas", []):
            def area_text(value):
                return "N/A" if value is None else f"{value:.0f}mm²"
            print(f"    flexural span {flex['span_index']}: "
                  f"Mn,l-/+={area_text(flex['mn_l_minus_area'])}/{area_text(flex['mn_l_plus_area'])} "
                  f"from {flex['left']['section'] or flex['left']['note']}; "
                  f"Mn,r-/+={area_text(flex['mn_r_minus_area'])}/{area_text(flex['mn_r_plus_area'])} "
                  f"from {flex['right']['section'] or flex['right']['note']}; "
                  f"Mn-/+={area_text(flex['mn_mid_minus_area'])}/{area_text(flex['mn_mid_plus_area'])} "
                  f"from {flex['middle']['section'] or flex['middle']['note']}")
        mc = beam.get("midspan_check")
        if mc:
            if mc["passed"] is None:
                print(f"    mid-span section {mc['section']}: {mc['note']}")
            else:
                print(f"    mid-span section {mc['section']} @x={mc['x']:.0f}: "
                      f"s={mc['s']}mm <= d/2={mc['s_max']:.1f}mm (d from {mc['d_from']}) -> "
                      f"{'PASS' if mc['passed'] else 'FAIL'}")

    if checker.errors:
        print("\nErrors:")
        for err in checker.errors:
            print(f"  {err}")
