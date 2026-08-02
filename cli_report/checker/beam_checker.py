import re
import math
import ezdxf
from collections import defaultdict

try:
    from .parser import (
        DIAMETER_TO_AREA_MM,
        strip_mtext_codes,
        get_vertices,
        get_line_points,
        distance,
    )
except ImportError:
    from parser import (
        DIAMETER_TO_AREA_MM,
        strip_mtext_codes,
        get_vertices,
        get_line_points,
        distance,
    )


SECTION_LABEL_RE = re.compile(r'SECTION\s+([0-9A-Za-z]+-[0-9A-Za-z]+)', re.IGNORECASE)
BEAM_LABEL_RE = re.compile(
    r'([A-Za-z]+-?\d+[A-Za-z]?)\s*\(\s*'
    r"(\d+)\s*(?:''|\"|mm)?\s*"
    r'[xX]\s*'
    r"(\d+)\s*(?:''|\"|mm)?\s*\)",
)
IMPERIAL_LABEL_RE = re.compile(r"\d+\s*(?:''|\")\s*[xX]\s*\d+\s*(?:''|\")")

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
# "10mm @ 100mm c/c" / "#3 @ 4'' c/c" -> stirrup diameter (bare, before @)
STIRRUP_DIA_RE = re.compile(r'(\d+)\s*mm\s*@', re.IGNORECASE)
STIRRUP_DIA_US_RE = re.compile(r'#\s*(\d+)\s*@', re.IGNORECASE)
# "@ 75mm" / "@ 4''" / "@ 6in" / "@ 200 c/c" -> spacing (unit optional, defaults mm)
SPACING_RE = re.compile(r"@\s*(\d+)\s*(?:mm|''|\"|in(?:ch)?(?:es)?)?", re.IGNORECASE)
SPACING_IMPERIAL_RE = re.compile(r"@\s*\d+\s*(?:''|\"|in(?:ch)?(?:es)?)", re.IGNORECASE)

# Keywords used to classify a longitudinal-bar callout by its position in the
# beam depth. Checked in this order against the lower-cased callout text;
# "bottom"/"top" also match abbreviations like "St.Top" or "Ext.Bottom".
LONGITUDINAL_POSITION_KEYWORDS = (("bottom", "bottom"), ("top", "top"), ("mid", "middle"))

# Section-cut end bubbles are short tokens like "1a" / "2b" drawn at both ends
# of a cut line. A detail titled "SECTION 1a-1a" is cut at the vertical line
# whose end bubbles both read "1a"; locating those bubbles tells us where along
# the beam the section is taken (support zone vs mid-span).
CUT_BUBBLE_RE = re.compile(r'^\s*([0-9A-Za-z]+(?:[._-]?[0-9A-Za-z]+)?)\s*$')


US_BAR_SIZE_TO_DIAMETER = {
    2: 6, 3: 10, 4: 12, 5: 16, 6: 20, 7: 22, 8: 25,
    9: 29, 10: 32, 11: 36, 12: 38, 14: 43, 16: 50, 18: 57,
}

MM_PER_INCH = 25.4

INSUNITS_TO_MM = {
    0: 1.0,     # unitless — assume mm
    1: 25.4,    # inches
    2: 304.8,   # feet
    4: 1.0,     # mm
    5: 10.0,    # cm
    6: 1000.0,  # meters
}


def detect_drawing_scale(doc):
    """Return mm-per-drawing-unit from the DXF $INSUNITS header.
    Falls back to 1.0 (mm) when the header is absent or unrecognised."""
    try:
        insunits = doc.header["$INSUNITS"]
    except (KeyError, AttributeError):
        return 1.0
    return INSUNITS_TO_MM.get(insunits, 1.0)


def detect_label_units(raw_text):
    """Return 'imperial' if the beam label dimensions have inch marks, else 'metric'."""
    if IMPERIAL_LABEL_RE.search(raw_text):
        return "imperial"
    return "metric"


def label_dim_to_mm(value, unit_system):
    """Convert a parsed label dimension to mm."""
    if unit_system == "imperial":
        return int(round(value * MM_PER_INCH))
    return value


def parse_spacing_mm(text):
    """Parse stirrup spacing from text and return the value in mm.
    Returns (spacing_mm, raw_value) or (None, None).
    """
    m = SPACING_RE.search(text)
    if not m:
        return None, None
    raw_value = int(m.group(1))
    if SPACING_IMPERIAL_RE.search(text):
        return int(round(raw_value * MM_PER_INCH)), raw_value
    return raw_value, raw_value


def parse_stirrup_dia(text):
    """Extract stirrup diameter in mm from callout text.
    Tries Ø10mm, then bare 10mm@, then #3@."""
    m = DIAMETER_RE.search(text)
    if m:
        return int(m.group(1))
    m = STIRRUP_DIA_RE.search(text)
    if m:
        return int(m.group(1))
    m = STIRRUP_DIA_US_RE.search(text)
    if m:
        return US_BAR_SIZE_TO_DIAMETER.get(int(m.group(1)))
    return None


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
    """Return rebar area in mm² from parser.py's approved lookup table."""
    try:
        return DIAMETER_TO_AREA_MM[int(diameter)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"No bar area is defined for diameter {diameter} mm"
        ) from exc


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
    MAX_MERGE_Y_GAP = 900.0    # mm; merge gap for a support line split by the beam body (must exceed beam depth, typically 600-700mm, but not bridge separate beam drawings)
    ROW_TOL = 5.0              # mm tolerance for grouping dimension lines into the same row
    BLOCK_GAP_THRESHOLD = 5000.0   # mm; x-gap beyond which same-row lines belong to a different drawing
    LABEL_X_TOL = 1200.0       # mm; how close a label's x must be to a block's leftmost line
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

        self.unit_scale = detect_drawing_scale(doc)
        self._label_unit_system = "metric"

    def _scaled_tol(self, mm_value):
        """Convert a mm tolerance constant to drawing units."""
        return mm_value / self.unit_scale

    # ------------------------------------------------------------------
    # Step 1 - Collect vertical support lines on BEAM SEC
    # ------------------------------------------------------------------

    def collect_vertical_lines(self):
        """
        Gather every vertical LINE/LWPOLYLINE-edge on BEAM SEC. For
        multi-point LWPOLYLINE entities (rectangles, column stubs), each
        vertical edge is extracted individually. Segments are first grouped
        by x, then within each x-group merged into a logical line only while
        consecutive segments are within MAX_MERGE_Y_GAP of each other.
        """
        raw = []
        for e in self.msp:
            if getattr(e.dxf, "layer", None) != "BEAM SEC":
                continue
            t = e.dxftype()
            if t == "LINE":
                pp = get_line_points(e)
                if pp is None:
                    continue
                (x1, y1), (x2, y2) = pp
                if abs(x1 - x2) > 0.01:
                    continue
                raw.append((x1, y1, y2, e))
            elif t == "LWPOLYLINE":
                pts = list(e.get_points(format="xy"))
                if len(pts) == 2:
                    pp = get_line_points(e)
                    if pp is None:
                        continue
                    (x1, y1), (x2, y2) = pp
                    if abs(x1 - x2) > 0.01:
                        continue
                    raw.append((x1, y1, y2, e))
                else:
                    n = len(pts)
                    edges = list(zip(pts, pts[1:]))
                    if e.close:
                        edges.append((pts[-1], pts[0]))
                    for (x1, y1), (x2, y2) in edges:
                        if abs(x1 - x2) > 0.01:
                            continue
                        ylo, yhi = min(y1, y2), max(y1, y2)
                        if yhi - ylo < 200.0:
                            continue
                        raw.append((x1, ylo, yhi, e))

        xgroups = defaultdict(list)
        for x, y1, y2, e in raw:
            key = next((k for k in xgroups if abs(k - x) <= self.LINE_MERGE_X_TOL), x)
            ylo, yhi = min(y1, y2), max(y1, y2)
            xgroups[key].append((ylo, yhi))

        for x, segs in xgroups.items():
            segs.sort(key=lambda s: s[0])
            cur = {"x": x, "ymin": segs[0][0], "ymax": segs[0][1], "segments": [segs[0]]}
            for ylo, yhi in segs[1:]:
                if ylo - cur["ymax"] <= self.MAX_MERGE_Y_GAP:
                    cur["ymin"] = min(cur["ymin"], ylo)
                    cur["ymax"] = max(cur["ymax"], yhi)
                    cur["segments"].append((ylo, yhi))
                else:
                    self.logical_lines.append(cur)
                    cur = {"x": x, "ymin": ylo, "ymax": yhi, "segments": [(ylo, yhi)]}
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
            beam_id = match.group(1)
            raw_b, raw_h = int(match.group(2)), int(match.group(3))
            unit_sys = detect_label_units(raw)
            b_mm = label_dim_to_mm(raw_b, unit_sys)
            h_mm = label_dim_to_mm(raw_h, unit_sys)
            x, y, _ = e.dxf.insert
            self.beam_labels.append({
                "entity": e, "x": x, "y": y,
                "label": strip_mtext_codes(raw), "id": beam_id,
                "b": raw_b, "h": raw_h,
                "b_mm": b_mm, "h_mm": h_mm,
                "unit_system": unit_sys,
            })
            if unit_sys == "imperial":
                self._label_unit_system = "imperial"

        print(f"Found {len(self.beam_labels)} beam elevation labels "
              f"(labels: {self._label_unit_system}, drawing coords: "
              f"{'mm' if self.unit_scale == 1.0 else f'{self.unit_scale}mm/unit'}).")

    # ------------------------------------------------------------------
    # Step 3 - Cluster logical lines into per-drawing blocks and match labels
    # ------------------------------------------------------------------

    LABEL_Y_MAX = 12000.0

    def build_blocks(self):
        """
        Group logical lines that share the same row (close ymin) and are
        contiguous in x (gap <= BLOCK_GAP_THRESHOLD) into one block per
        physical beam drawing.  Then assign each label to its nearest block
        (by x, with vertical distance as tiebreaker).  A block may serve
        multiple labels when stacked beam drawings share support lines.
        """
        row_tol = self._scaled_tol(self.ROW_TOL)
        block_gap = self._scaled_tol(self.BLOCK_GAP_THRESHOLD)
        rows = defaultdict(list)
        for idx, line in enumerate(self.logical_lines):
            rows[round(line["ymin"] / row_tol)].append(idx)

        raw_blocks = []
        for idxs in rows.values():
            idxs.sort(key=lambda i: self.logical_lines[i]["x"])
            cluster = [idxs[0]]
            for i in idxs[1:]:
                if self.logical_lines[i]["x"] - self.logical_lines[cluster[-1]]["x"] > block_gap:
                    raw_blocks.append(cluster)
                    cluster = []
                cluster.append(i)
            raw_blocks.append(cluster)

        base_x_tol = self._scaled_tol(self.LABEL_X_TOL)
        y_max = self._scaled_tol(self.LABEL_Y_MAX)
        for lbl in self.beam_labels:
            best_block, best_dist = None, None
            for line_idxs in raw_blocks:
                xs = [self.logical_lines[i]["x"] for i in line_idxs]
                leftmost_x = min(xs)
                rightmost_x = max(xs)
                block_span = rightmost_x - leftmost_x
                x_tol = max(base_x_tol, block_span * 0.6)
                ymin_block = self.logical_lines[line_idxs[0]]["ymin"]
                if lbl["x"] < leftmost_x:
                    x_diff = leftmost_x - lbl["x"]
                elif lbl["x"] > rightmost_x:
                    x_diff = lbl["x"] - rightmost_x
                else:
                    x_diff = 0.0
                y_diff = abs(lbl["y"] - ymin_block)
                if x_diff > x_tol or y_diff > y_max:
                    continue
                dist = y_diff + x_diff * 0.1
                if best_dist is None or dist < best_dist:
                    best_dist, best_block = dist, line_idxs
            if best_block is not None:
                self.blocks.append({"beam_label": lbl, "line_idxs": best_block})

        self._split_shared_blocks()

        print(f"Found {len(self.blocks)} beam elevation drawings.")
        for block in self.blocks:
            lbl = block["beam_label"]
            print(f"  {lbl['id']} ({lbl['b']}x{lbl['h']}) @ ({lbl['x']:.1f},{lbl['y']:.1f}): "
                  f"{len(block['line_idxs'])} support lines.")

    # ------------------------------------------------------------------
    # Step 3b - Split blocks shared by multiple labels at the same y
    # ------------------------------------------------------------------

    def _split_shared_blocks(self):
        groups = defaultdict(list)
        for block in self.blocks:
            groups[id(block["line_idxs"])].append(block)

        for blk_id, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda b: b["beam_label"]["x"])
            h0 = members[0]["beam_label"]["h_mm"] / self.unit_scale
            same_y = []
            for b in members:
                if same_y and abs(b["beam_label"]["y"] - same_y[0]["beam_label"]["y"]) > h0 * 3:
                    self._partition_by_x(same_y)
                    same_y = [b]
                else:
                    same_y.append(b)
            if len(same_y) >= 2:
                self._partition_by_x(same_y)

    def _partition_by_x(self, members):
        if len(members) < 2:
            return
        line_idxs = members[0]["line_idxs"]
        h = members[0]["beam_label"]["h_mm"] / self.unit_scale
        label_y = members[0]["beam_label"]["y"]

        face_idxs = set()
        for idx in line_idxs:
            ll = self.logical_lines[idx]
            by, ty = self._find_beam_edges(ll, h, prefer_y=label_y)
            if by is not None:
                face_idxs.add(idx)

        frame_xs = sorted(
            self.logical_lines[idx]["x"]
            for idx in line_idxs if idx not in face_idxs
        )

        sorted_members = sorted(members, key=lambda b: b["beam_label"]["x"])

        face_x_set = sorted(self.logical_lines[idx]["x"] for idx in face_idxs)

        cuts = []
        for i in range(len(sorted_members) - 1):
            left_lx = sorted_members[i]["beam_label"]["x"]
            right_lx = sorted_members[i + 1]["beam_label"]["x"]
            between = [fx for fx in frame_xs if left_lx < fx < right_lx]
            valid = [fx for fx in between
                     if any(f < fx for f in face_x_set) and any(f > fx for f in face_x_set)]
            if valid:
                cuts.append(max(valid))
            else:
                cuts.append(None)

        if not any(c is not None for c in cuts):
            return

        margin = self._scaled_tol(50.0)
        for mi, member in enumerate(sorted_members):
            left_bound = cuts[mi - 1] if mi > 0 and cuts[mi - 1] is not None else -float("inf")
            right_bound = cuts[mi] if mi < len(cuts) and cuts[mi] is not None else float("inf")
            my_idxs = [
                idx for idx in line_idxs
                if left_bound - margin <= self.logical_lines[idx]["x"] <= right_bound + margin
            ]
            if my_idxs:
                member["line_idxs"] = my_idxs

    # ------------------------------------------------------------------
    # Step 4 - Extract every property for one beam block
    # ------------------------------------------------------------------

    EDGE_GAP_RATIO = 0.15

    def _find_beam_edges(self, line, h, prefer_y=None):
        """
        Return (bottom_y, top_y) of the beam at this support line: the gap
        between two of its segments whose size is close to h.

        When *prefer_y* is given, the gap whose centre is closest to that y
        wins among ties (multiple stacked beams on the same column).

        Tolerance is the larger of EDGE_GAP_TOL (absolute) and
        EDGE_GAP_RATIO * h (proportional), because some drawings use a
        standardised column-gap width that doesn't exactly match h.
        """
        tol = max(self._scaled_tol(self.EDGE_GAP_TOL), h * self.EDGE_GAP_RATIO)
        pts = sorted(seg for seg in line["segments"])
        candidates = []
        for i in range(len(pts) - 1):
            bottom_of_lower, top_of_upper = pts[i][1], pts[i + 1][0]
            gap = top_of_upper - bottom_of_lower
            if gap <= 0:
                continue
            diff = abs(gap - h)
            if diff <= tol:
                candidates.append((bottom_of_lower, top_of_upper, diff))
        if not candidates:
            return (None, None)
        if prefer_y is not None:
            candidates.sort(key=lambda c: abs((c[0] + c[1]) / 2 - prefer_y))
        else:
            candidates.sort(key=lambda c: c[2])
        return (candidates[0][0], candidates[0][1])

    def _column_faces(self, lines, h, prefer_y=None, beam_y=None):
        """
        Return the sorted x positions of the genuine support/column faces:
        the vertical lines that carry a beam-hiding gap of size h. A bar
        cut-off line drawn inside the beam has no such gap, so this filters
        the rebar lines out of the raw support-line set.

        Also recognises column-stub lines: single-segment short verticals
        whose y-range matches the beam gap (bottom_y … top_y).
        """
        tol = max(self._scaled_tol(self.EDGE_GAP_TOL), h * self.EDGE_GAP_RATIO)
        faces = []
        for line in lines:
            if self._find_beam_edges(line, h, prefer_y=prefer_y)[0] is not None:
                faces.append(line["x"])
            elif beam_y is not None:
                bottom_y, top_y = beam_y
                for seg in line["segments"]:
                    seg_h = seg[1] - seg[0]
                    gap_h = top_y - bottom_y
                    if abs(seg_h - gap_h) <= tol and abs(seg[0] - bottom_y) <= tol:
                        faces.append(line["x"])
                        break
        return sorted(faces)

    def _has_beam_sec_above(self, left_x, right_x, top_y):
        """True when BEAM SEC entities exist above top_y between two faces.

        A column stub drawn above the beam body produces horizontal or
        near-horizontal BEAM SEC entities between the two face lines and
        above the beam's top edge.  A real span has no such entities.
        """
        margin_x = self._scaled_tol(10.0)
        above_min = top_y + self._scaled_tol(5.0)
        above_max = top_y + self._scaled_tol(500.0)
        for e in self.msp:
            layer = getattr(e.dxf, "layer", "")
            if "BEAM SEC" not in layer.upper():
                continue
            t = e.dxftype()
            if t == "LWPOLYLINE":
                pts = list(e.get_points(format="xy"))
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if (min(xs) >= left_x - margin_x and max(xs) <= right_x + margin_x
                        and min(ys) >= above_min and max(ys) <= above_max):
                    return True
            elif t == "LINE":
                pp = get_line_points(e)
                if pp is None:
                    continue
                (x1, y1), (x2, y2) = pp
                if abs(y1 - y2) > abs(x1 - x2):
                    continue
                xmin, xmax = min(x1, x2), max(x1, x2)
                ymin = min(y1, y2)
                if (xmin >= left_x - margin_x and xmax <= right_x + margin_x
                        and ymin >= above_min and ymin <= above_max):
                    return True
        return False

    def _spans_from_faces(self, faces, top_y=None):
        """Face-to-face intervals that are spans, not column widths.

        A gap is a column width (not a span) when there are BEAM SEC
        entities directly above the beam top between the two faces.
        Falls back to a minimum-width threshold when top_y is unavailable.
        """
        min_w = self._scaled_tol(self.MIN_SPAN_WIDTH)
        spans = []
        for i in range(len(faces) - 1):
            gap = faces[i + 1] - faces[i]
            if gap < min_w:
                continue
            if top_y is not None and self._has_beam_sec_above(faces[i], faces[i + 1], top_y):
                continue
            spans.append((faces[i], faces[i + 1]))
        return spans

    def _collect_stirrup_xs(self, minx, maxx, bottom_y, top_y):
        """
        x positions of the individual stirrups - the vertical marks on the
        'Dim Line' layer family - that fall within the beam's span and depth.
        """
        margin = self._scaled_tol(400.0)
        ylo, yhi = min(bottom_y, top_y) - margin, max(bottom_y, top_y) + margin
        x_margin = self._scaled_tol(5.0)
        vert_tol = self._scaled_tol(1.0)
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
                if abs(x1 - x2) > vert_tol:
                    continue
                x, ymid = x1, (y1 + y2) / 2
            elif t == "INSERT":
                x, ymid = e.dxf.insert.x, e.dxf.insert.y
            else:
                continue
            if minx - x_margin <= x <= maxx + x_margin and ylo <= ymid <= yhi:
                xs.append(x)
        return sorted(xs)

    def _check_first_stirrups(self, spans, stirrup_zones, gap_dims=None):
        """
        Per span, the first stirrup must be >= FIRST_STIRRUP_MIN from each
        support face.

        Prefer explicit gap dimensions (short dims adjacent to support faces)
        when available; fall back to zone-boundary heuristic otherwise.
        """
        checks = []
        tol = self._scaled_tol(1.0)
        face_tol = self._scaled_tol(5.0)
        first_stir = self._scaled_tol(self.FIRST_STIRRUP_MIN)
        near_tol = self._scaled_tol(10.0)
        if gap_dims is None:
            gap_dims = []
        for x0, x1 in spans:
            gap_left = gap_right = None

            for gxlo, gxhi, glen in gap_dims:
                if gap_left is None and abs(gxlo - x0) <= face_tol:
                    gap_left = glen * self.unit_scale
                if gap_right is None and abs(gxhi - x1) <= face_tol:
                    gap_right = glen * self.unit_scale

            span_zones = [z for z in stirrup_zones
                          if z["end_x"] > x0 + tol and z["start_x"] < x1 - tol]
            if span_zones:
                span_zones.sort(key=lambda z: z["start_x"])
                if gap_left is None:
                    raw_left = span_zones[0]["start_x"] - x0
                    if raw_left <= near_tol:
                        gap_left = first_stir * self.unit_scale
                    else:
                        gap_left = raw_left * self.unit_scale
                if gap_right is None:
                    raw_right = x1 - span_zones[-1]["end_x"]
                    if raw_right <= near_tol:
                        gap_right = first_stir * self.unit_scale
                    else:
                        gap_right = raw_right * self.unit_scale

            if gap_left is None or gap_right is None:
                continue

            limit = self.FIRST_STIRRUP_MIN - self.FIRST_STIRRUP_TOL
            checks.append({
                "span_start": x0, "span_end": x1,
                "gap_left": gap_left,
                "gap_right": gap_right,
                "passed": gap_left >= limit and gap_right >= limit,
            })
        return checks

    def _check_stirrup_zone_lengths(self, spans, stirrup_zones, h_mm):
        """Check that left/right stirrup zones of each span are >= 2h."""
        min_len = 2 * h_mm
        tol = self._scaled_tol(1.0)
        checks = []
        for x0, x1 in spans:
            span_zones = [z for z in stirrup_zones
                          if z["end_x"] > x0 + tol and z["start_x"] < x1 - tol]
            if not span_zones:
                checks.append({
                    "span_start": x0, "span_end": x1,
                    "min_length": min_len,
                    "left_zone_length": None, "right_zone_length": None,
                    "passed": None,
                })
                continue
            span_zones.sort(key=lambda z: z["start_x"])
            if len(span_zones) == 1:
                zlen = span_zones[0]["length"]
                checks.append({
                    "span_start": x0, "span_end": x1,
                    "min_length": min_len,
                    "left_zone_length": zlen, "right_zone_length": None,
                    "passed": zlen >= min_len - tol,
                })
            else:
                left_len = span_zones[0]["length"]
                right_len = span_zones[-1]["length"]
                checks.append({
                    "span_start": x0, "span_end": x1,
                    "min_length": min_len,
                    "left_zone_length": left_len, "right_zone_length": right_len,
                    "passed": left_len >= min_len - tol and right_len >= min_len - tol,
                })
        return checks

    def _collect_horizontal_dims(self, minx, maxx, bottom_y, depth):
        """Return [(y, xlo, xhi, length, text)] for DIMENSION objects under the beam's span."""
        row_tol = self._scaled_tol(self.ROW_TOL)
        out = []
        for e in self.msp:
            if e.dxftype() != "DIMENSION":
                continue
            p2, p3 = e.dxf.defpoint2, e.dxf.defpoint3
            if abs(p2[1] - p3[1]) > self._scaled_tol(0.5):
                continue   # not horizontal
            y = p2[1]
            if not (bottom_y - depth <= y <= bottom_y + row_tol):
                continue
            xlo, xhi = min(p2[0], p3[0]), max(p2[0], p3[0])
            if xhi < minx - row_tol or xlo > maxx + row_tol:
                continue
            out.append((y, xlo, xhi, e.get_measurement(), strip_mtext_codes(e.dxf.text)))
        return out

    def _collect_longitudinal_bars(self, minx, maxx, bottom_y, top_y):
        """Return [{position, bars, text, x, y}] for MULTILEADER callouts pointing into the beam's bbox."""
        m = self._scaled_tol(self.LEADER_BBOX_MARGIN)
        line_tol = self._scaled_tol(LINE_TOL)
        point_tol = self._scaled_tol(POINT_TOL)
        bars = []
        horizontal_segments = []
        for target in self.msp:
            if target.dxftype() not in ("LINE", "LWPOLYLINE"):
                continue
            pts = entity_points(target)
            closed = target.dxftype() == "LWPOLYLINE" and target.closed
            for a, b in polyline_edges(pts, closed):
                if abs(a[1] - b[1]) > line_tol:
                    continue
                xlo, xhi = min(a[0], b[0]), max(a[0], b[0])
                if (xhi >= minx - m and xlo <= maxx + m and
                        bottom_y - m <= a[1] <= top_y + m):
                    horizontal_segments.append((a, b))

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
                tip_y = tips_in_bbox[0][1]
                third = (top_y - bottom_y) / 3
                if tip_y >= top_y - third:
                    position = "top"
                elif tip_y <= bottom_y + third:
                    position = "bottom"
                else:
                    position = "middle"

            touched_segments = []
            for a, b in horizontal_segments:
                for tip in tips_in_bbox:
                    gap = point_to_segment_distance(tip, a, b)
                    if gap <= point_tol:
                        touched_segments.append((gap, -abs(b[0] - a[0]),
                                                 min(a[0], b[0]), max(a[0], b[0])))
            extent = min(touched_segments) if touched_segments else None

            bars.append({
                "position": position,
                "bars": [{"count": c, "diameter": d} for c, d in pairs],
                "text": text,
                "x": tips_in_bbox[0][0], "y": tips_in_bbox[0][1],
                "start_x": extent[2] if extent else None,
                "end_x": extent[3] if extent else None,
            })
        return bars

    def extract_beam(self, block):
        lbl = block["beam_label"]
        line_idxs = block["line_idxs"]
        lines = [self.logical_lines[i] for i in line_idxs]
        h = lbl["h_mm"] / self.unit_scale

        label_y = lbl["y"]
        bottom_y = top_y = None
        for line in lines:
            by, ty = self._find_beam_edges(line, h, prefer_y=label_y)
            if by is not None:
                bottom_y, top_y = by, ty
                break

        if bottom_y is None:
            tol = max(self._scaled_tol(self.EDGE_GAP_TOL), h * self.EDGE_GAP_RATIO)
            for line in lines:
                for seg in line["segments"]:
                    seg_h = seg[1] - seg[0]
                    if abs(seg_h - h) <= tol:
                        bottom_y, top_y = seg[0], seg[1]
                        break
                if bottom_y is not None:
                    break

        if bottom_y is None:
            self.errors.append({"beam": lbl["label"], "issue": "Could not locate beam top/bottom edge"})
            return None

        beam_mid_y = (bottom_y + top_y) / 2
        y_tol = h * 1.5
        lines = [line for line in lines
                 if any(seg[0] <= beam_mid_y + y_tol
                        and seg[1] >= beam_mid_y - y_tol
                        for seg in line["segments"])]
        if not lines:
            self.errors.append({"beam": lbl["label"], "issue": "No vertical lines found at beam y-level"})
            return None

        block_idx_set = set(line_idxs)
        minx = min(line["x"] for line in lines)
        maxx = max(line["x"] for line in lines)
        margin = self._scaled_tol(500.0)
        search_left = min(minx, lbl["x"]) - margin
        search_right = max(maxx, lbl["x"]) + margin
        for i, ll in enumerate(self.logical_lines):
            if i in block_idx_set:
                continue
            if ll["x"] < search_left or ll["x"] > search_right:
                continue
            if minx - margin <= ll["x"] <= maxx + margin:
                continue
            if not any(seg[0] <= top_y and seg[1] >= bottom_y
                       for seg in ll["segments"]):
                continue
            lines.append(ll)

        minx = min(line["x"] for line in lines)
        maxx = max(line["x"] for line in lines)
        support_positions = sorted(line["x"] for line in lines)

        column_faces = self._column_faces(lines, h, prefer_y=label_y, beam_y=(bottom_y, top_y))
        spans = self._spans_from_faces(column_faces, top_y=top_y)
        if not spans and len(support_positions) >= 2:
            column_faces = support_positions
            spans = self._spans_from_faces(column_faces)

        depth = max(3 * h, self._scaled_tol(2000.0))
        dims = self._collect_horizontal_dims(minx, maxx, bottom_y, depth)

        stirrup_zones = []
        seen = set()
        us = self.unit_scale
        for _y, xlo, xhi, length, text in dims:
            parsed_dia = parse_stirrup_dia(text)
            sp_mm, sp_raw = parse_spacing_mm(text)
            if not (parsed_dia is not None and sp_mm is not None):
                continue
            key = (round(xlo), round(xhi))
            if key in seen:
                continue
            seen.add(key)
            stirrup_zones.append({
                "start_x": xlo, "end_x": xhi, "length": length * us,
                "stirrup_diameter": parsed_dia, "stirrup_spacing": sp_mm,
            })
        stirrup_zones.sort(key=lambda z: z["start_x"])

        gap_dims = []
        if stirrup_zones and dims:
            zone_ys = [y for y, xlo, xhi, length, text in dims
                       if any(abs(xlo - z["start_x"]) < 1 and abs(xhi - z["end_x"]) < 1
                              for z in stirrup_zones)]
            if zone_ys:
                zone_y = zone_ys[0]
                row_tol = self._scaled_tol(self.ROW_TOL)
                gap_dims = [(xlo, xhi, length)
                            for y, xlo, xhi, length, text in dims
                            if abs(y - zone_y) < row_tol
                            and length * us <= 75
                            and not text.strip()]

        first_stirrup_checks = self._check_first_stirrups(spans, stirrup_zones, gap_dims)
        for chk in first_stirrup_checks:
            if not chk["passed"]:
                self.errors.append({
                    "beam": lbl["label"],
                    "issue": "First stirrup closer than 50mm to a support face",
                    "details": (f"span x=[{chk['span_start']:.0f},{chk['span_end']:.0f}] "
                                f"left gap={chk['gap_left']:.0f}mm, right gap={chk['gap_right']:.0f}mm"),
                })

        span_dimensions = []
        if dims:
            outer_y = min(y for y, *_ in dims)   # farthest row below the beam
            span_dimensions = sorted(
                ({"start_x": xlo, "end_x": xhi, "length": length * us}
                 for y, xlo, xhi, length, _ in dims if abs(y - outer_y) < self._scaled_tol(self.ROW_TOL)),
                key=lambda d: d["start_x"],
            )

        longitudinal_bars = self._collect_longitudinal_bars(minx, maxx, bottom_y, top_y)

        zone_length_checks = self._check_stirrup_zone_lengths(spans, stirrup_zones, lbl["h_mm"])
        for chk in zone_length_checks:
            if chk["passed"] is False:
                parts = []
                if chk["left_zone_length"] is not None:
                    parts.append(f"left={chk['left_zone_length']:.0f}mm")
                if chk["right_zone_length"] is not None:
                    parts.append(f"right={chk['right_zone_length']:.0f}mm")
                self.errors.append({
                    "beam": lbl["label"],
                    "issue": f"Stirrup zone length < 2h ({chk['min_length']:.0f}mm)",
                    "details": (f"span x=[{chk['span_start']:.0f},{chk['span_end']:.0f}] "
                                + ", ".join(parts)),
                })

        return {
            "label": lbl["label"], "id": lbl["id"],
            "section_width": lbl["b_mm"], "section_depth": lbl["h_mm"],
            "support_positions": support_positions,
            "column_faces": column_faces,
            "spans": spans,
            "y_bottom": bottom_y, "y_top": top_y,
            "stirrup_zones": stirrup_zones,
            "span_dimensions": span_dimensions,
            "longitudinal_bars": longitudinal_bars,
            "first_stirrup_checks": first_stirrup_checks,
            "zone_length_checks": zone_length_checks,
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

    It also checks at least two continuous bars at each face and the top and
    bottom gross-section reinforcement ratios:

        rho = As/(b*h)
        max(0.25*sqrt(f'c)/fy, 1.4/fy) <= rho <= 0.025

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
      6. check_sections()       - check spacing, bar count, and top/bottom rho
      7. check_flexural_span_areas() - calculate nominal moments and check the
                                   left/right one-half and span one-quarter rules

    run() also parses every longitudinal beam elevation drawing (BEAM-xx
    labels) found on the same BEAM SEC layer via BeamElevationParser,
    storing the result in self.elevations - see that class for details.
    """

    CUT_BUBBLE_X_MARGIN = 500.0
    CUT_BUBBLE_Y_MARGIN_MIN = 400.0
    CUT_BUBBLE_Y_MAX = 2000.0
    FLEXURAL_SPAN_X_TOL = 1500.0
    FLEXURAL_END_REGION_RATIO = 0.30
    FLEXURAL_END_REGION_MIN = 300.0
    FLEXURAL_END_REGION_MAX = 1200.0
    ROW_BAR_Y_TOL = 25.0
    RHO_COMPARISON_TOL = 1e-8

    def __init__(self, dxf_file, fc_mpa=None, fy_mpa=None, frame_system="smrf"):
        self.dxf_file = dxf_file
        self.fc_mpa = float(fc_mpa) if fc_mpa is not None else None
        self.fy_mpa = float(fy_mpa) if fy_mpa is not None else None
        self.frame_system = frame_system.lower()
        if (
            self.fc_mpa is not None
            and (not math.isfinite(self.fc_mpa) or self.fc_mpa <= 0)
        ):
            raise ValueError("Concrete strength f'c must be greater than zero")
        if (
            self.fy_mpa is not None
            and (not math.isfinite(self.fy_mpa) or self.fy_mpa <= 0)
        ):
            raise ValueError("Steel yield strength fy must be greater than zero")
        self.doc = None
        self.msp = None

        self.beam_sec_entities = []   # [entity, ...] on BEAM SEC layer
        self.section_labels = []      # [{entity, x, y, name}]
        self.sections = defaultdict(list)   # label_idx -> [entity, ...]

        self.section_data = []   # one dict per section with all computed values
        self.elevations = []     # one dict per longitudinal beam elevation drawing
        self._cut_bubbles = {}   # cut-id -> [(x, y)] section-cut end bubbles
        self._bubble_assignments = {}  # beam_index -> [(cut_id, x)]
        self.errors = []
        self.results = []

        self.unit_scale = 1.0

    def _scaled_tol(self, mm_value):
        """Convert a mm tolerance constant to drawing units."""
        return mm_value / self.unit_scale

    # ------------------------------------------------------------------
    # Step 1 - Load DXF
    # ------------------------------------------------------------------

    def load(self):
        self.doc = ezdxf.readfile(self.dxf_file)
        self.msp = self.doc.modelspace()
        self.unit_scale = detect_drawing_scale(self.doc)
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
        max_dist = self._scaled_tol(MAX_LABEL_DIST)
        for entity in self.beam_sec_entities:
            pts = entity_points(entity)
            if not pts:
                continue
            ymin = min(p[1] for p in pts)
            bottom_x = sum(p[0] for p in pts) / len(pts)

            best_idx, best_dist = -1, None
            for lbl_idx, lbl in enumerate(self.section_labels):
                if lbl["y"] >= ymin:
                    continue
                d = distance((bottom_x, ymin), (lbl["x"], lbl["y"]))
                if best_dist is None or d < best_dist:
                    best_dist, best_idx = d, lbl_idx

            if best_idx != -1 and best_dist <= max_dist:
                self.sections[best_idx].append(entity)

        fallback_labels = set(i for i in range(len(self.section_labels)) if i not in self.sections)
        if fallback_labels:
            unmatched = list(fallback_labels)
            for entity in self.msp:
                if entity.dxftype() not in ("LINE", "LWPOLYLINE"):
                    continue
                if getattr(entity.dxf, "layer", "") == "BEAM SEC":
                    continue
                pts = entity_points(entity)
                if not pts:
                    continue
                ymin = min(p[1] for p in pts)
                bottom_x = sum(p[0] for p in pts) / len(pts)

                best_idx, best_dist = -1, None
                for lbl_idx in unmatched:
                    lbl = self.section_labels[lbl_idx]
                    if lbl["y"] >= ymin:
                        continue
                    d = distance((bottom_x, ymin), (lbl["x"], lbl["y"]))
                    if best_dist is None or d < best_dist:
                        best_dist, best_idx = d, lbl_idx

                if best_idx != -1 and best_dist <= max_dist:
                    self.sections[best_idx].append(entity)

        for lbl_idx, entities in self.sections.items():
            lbl = self.section_labels[lbl_idx]
            src = "entities (fallback)" if lbl_idx in fallback_labels else "BEAM SEC entities"
            print(f"  Section {lbl['name']}: {len(entities)} {src}.")

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
        line_tol = self._scaled_tol(LINE_TOL)
        horiz_ys = set()
        all_ys = []
        for e in entities:
            pts = entity_points(e)
            all_ys.extend(p[1] for p in pts)
            closed = e.dxftype() == "LWPOLYLINE" and e.closed
            for a, b in polyline_edges(pts, closed):
                if abs(a[1] - b[1]) <= line_tol:
                    horiz_ys.add(round(a[1], 1))

        bottom_y = min(horiz_ys) if horiz_ys else min(all_ys)
        higher = [y for y in horiz_ys if y > bottom_y + line_tol]
        top_y = max(higher) if higher else max(all_ys)
        return top_y, bottom_y

    def _section_width(self, entities):
        """Beam width b from the longest horizontal BEAM SEC edge."""
        line_tol = self._scaled_tol(LINE_TOL)
        widths = []
        for e in entities:
            pts = entity_points(e)
            closed = e.dxftype() == "LWPOLYLINE" and e.closed
            for a, b in polyline_edges(pts, closed):
                if abs(a[1] - b[1]) <= line_tol:
                    widths.append(abs(b[0] - a[0]))
        return max(widths) if widths else None

    CAGE_BOUNDARY_MARGIN_RATIO = 0.05

    def _find_cage_polyline(self, bbox):
        """Find the closed LWPOLYLINE (the stirrup/tie cage) inscribed within
        the section bbox. Accepts polylines with >= 4 points. When multiple
        candidates exist, prefers the one with the most points. Rejects any
        candidate whose bbox nearly matches the section bbox (that is the
        outer boundary, not the cage)."""
        minx, miny, maxx, maxy = bbox
        section_w = maxx - minx
        section_h = maxy - miny
        if section_w <= 0 or section_h <= 0:
            return None
        margin_x = self.CAGE_BOUNDARY_MARGIN_RATIO * section_w
        margin_y = self.CAGE_BOUNDARY_MARGIN_RATIO * section_h
        candidates = []
        for e in self.msp:
            if e.dxftype() != "LWPOLYLINE" or not e.closed:
                continue
            pts = get_vertices(e)
            if len(pts) < 4:
                continue
            exminx = min(p[0] for p in pts)
            exmaxx = max(p[0] for p in pts)
            exminy = min(p[1] for p in pts)
            exmaxy = max(p[1] for p in pts)
            if not (exminx > minx and exminy > miny and exmaxx < maxx and exmaxy < maxy):
                continue
            if (exminx - minx < margin_x and maxx - exmaxx < margin_x and
                    exminy - miny < margin_y and maxy - exmaxy < margin_y):
                continue
            candidates.append(e)
        if not candidates:
            return None
        return max(candidates, key=lambda e: len(get_vertices(e)))

    def _find_cage_from_lines(self, bbox):
        """Fallback: detect a rectangular cage from LINE entities forming a
        rectangle inscribed in the section bbox. Returns a list of 4 corner
        points if found, else None."""
        minx, miny, maxx, maxy = bbox
        section_w = maxx - minx
        section_h = maxy - miny
        if section_w <= 0 or section_h <= 0:
            return None
        h_segs = []
        v_segs = []
        for e in self.msp:
            if e.dxftype() != "LINE":
                continue
            pp = get_line_points(e)
            if pp is None:
                continue
            (x1, y1), (x2, y2) = pp
            if not (min(x1, x2) >= minx and max(x1, x2) <= maxx and
                    min(y1, y2) >= miny and max(y1, y2) <= maxy):
                continue
            line_tol = self._scaled_tol(LINE_TOL)
            if abs(y1 - y2) <= line_tol:
                h_segs.append((y1, min(x1, x2), max(x1, x2)))
            elif abs(x1 - x2) <= line_tol:
                v_segs.append((x1, min(y1, y2), max(y1, y2)))
        if len(h_segs) < 2 or len(v_segs) < 2:
            return None
        h_segs.sort(key=lambda s: s[0])
        bottom_h = h_segs[0]
        top_h = h_segs[-1]
        cage_h = top_h[0] - bottom_h[0]
        if cage_h >= section_h * 0.95 or cage_h < section_h * 0.3:
            return None
        cage_left = min(bottom_h[1], top_h[1])
        cage_right = max(bottom_h[2], top_h[2])
        cage_w = cage_right - cage_left
        if cage_w >= section_w * 0.95 or cage_w < section_w * 0.3:
            return None
        return [
            (cage_left, bottom_h[0]),
            (cage_right, bottom_h[0]),
            (cage_right, top_h[0]),
            (cage_left, top_h[0]),
        ]

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
        m = self._scaled_tol(BBOX_MARGIN)
        minx, miny = minx - m, miny - m
        maxx, maxy = maxx + m, maxy + m

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
        tol = self._scaled_tol(POINT_TOL)
        return distance(point, (circle.dxf.center.x, circle.dxf.center.y)) <= circle.dxf.radius + tol

    def _touches_polyline(self, point, poly):
        tol = self._scaled_tol(POINT_TOL)
        pts = get_vertices(poly)
        return any(point_to_segment_distance(point, a, b) <= tol
                   for a, b in polyline_edges(pts, poly.closed))

    def _touches_edges(self, point, pts):
        """Check if point is within tolerance of any edge of a polygon given as a point list."""
        tol = self._scaled_tol(POINT_TOL)
        n = len(pts)
        return any(point_to_segment_distance(point, pts[i], pts[(i + 1) % n]) <= tol
                   for i in range(n))

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
        tol = self._scaled_tol(self.ROW_BAR_Y_TOL)
        rows = []
        for circle in sorted(circles, key=lambda c: c.dxf.center.y):
            cy = circle.dxf.center.y
            if rows and abs(cy - rows[-1]["y"]) <= tol:
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

    MIN_SECTION_DIM = 50.0

    def extract_sections(self):
        for lbl_idx, entities in self.sections.items():
            lbl = self.section_labels[lbl_idx]
            name = lbl["name"]

            top_y, bottom_y = self._horizontal_extents(entities)
            h = top_y - bottom_y
            section_width = self._section_width(entities)

            min_dim = self._scaled_tol(self.MIN_SECTION_DIM)
            if h is None or section_width is None or h < min_dim or section_width < min_dim:
                continue
            aspect = max(h, section_width) / max(min(h, section_width), 1e-6)
            if aspect > 10:
                continue

            minx, _, maxx, _ = self._section_bbox(entities)
            bbox = (minx, bottom_y, maxx, top_y)

            cage = self._find_cage_polyline(bbox)
            cage_is_synthetic = False
            cage_pts = None
            if cage is None:
                cage_corners = self._find_cage_from_lines(bbox)
                if cage_corners is not None:
                    cage_pts = cage_corners
                    cage_is_synthetic = True
                else:
                    self.errors.append({"section": name, "issue": "No stirrup/tie polyline found"})
                    continue
            else:
                cage_pts = get_vertices(cage)

            cage_horiz_ys = set()
            n_cage = len(cage_pts)
            for i in range(n_cage):
                a = cage_pts[i]
                b = cage_pts[(i + 1) % n_cage]
                if abs(a[1] - b[1]) <= LINE_TOL:
                    cage_horiz_ys.add(round(a[1], 1))
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

            circle_rows = self._circle_rows(circles)
            bottom_row_circles = circle_rows[0]["circles"]
            top_row_circles = circle_rows[-1]["circles"]
            bottom_bars = len(bottom_row_circles)
            top_bars = len(top_row_circles)
            bottom_bar_y = sum(c.dxf.center.y for c in bottom_row_circles) / bottom_bars
            top_bar_y = sum(c.dxf.center.y for c in top_row_circles) / top_bars

            # --- stirrup diameter & spacing: leader touches the cage but not a circle ---
            stirrup_dia, stirrup_spacing = None, None
            for ld in leaders:
                if cage_is_synthetic:
                    touches_cage = any(self._touches_edges(p, cage_pts) for p in ld["tips"])
                else:
                    touches_cage = any(self._touches_polyline(p, cage) for p in ld["tips"])
                touches_circle = any(self._touches_circle(p, c) for p in ld["tips"] for c in circles)
                if touches_cage and not touches_circle:
                    parsed_dia = parse_stirrup_dia(ld["text"])
                    sp_mm, sp_raw = parse_spacing_mm(ld["text"])
                    if parsed_dia is not None and sp_mm is not None:
                        stirrup_dia, stirrup_spacing = parsed_dia, sp_mm
                        break

            if stirrup_dia is None or stirrup_spacing is None:
                self.errors.append({"section": name, "issue": "No stirrup diameter/spacing leader found"})
                continue

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

            us = self.unit_scale
            self.section_data.append({
                "name": name, "b": (section_width or (maxx - minx)) * us,
                "h": h * us, "clear_cover": clear_cover * us,
                "stirrup_dia": stirrup_dia, "s": stirrup_spacing,
                "main_bar_dia": main_bar_dia, "smallest_long_dia": smallest_long_dia,
                "top_bars": top_bars, "bottom_bars": bottom_bars,
                "d_negative": (top_bar_y - bottom_y) * us,
                "d_positive": (top_y - bottom_bar_y) * us,
                "top_bar_groups": top_bar_groups, "bottom_bar_groups": bottom_bar_groups,
                "top_bar_area": self._bar_area_total(top_bar_groups),
                "bottom_bar_area": self._bar_area_total(bottom_bar_groups),
                "label_x": lbl["x"], "label_y": lbl["y"],
            })

    # ------------------------------------------------------------------
    # Step 6 - Run the drawn cross-section checks (support/end sections)
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_depth(data):
        return data["h"] - data["clear_cover"] - data["stirrup_dia"] - data["main_bar_dia"] / 2

    @staticmethod
    def _beta1(fc_mpa):
        if fc_mpa <= 28:
            return 0.85
        if fc_mpa >= 56:
            return 0.65
        return 0.85 - 0.05 * (fc_mpa - 28) / 7

    def _rho_max(self):
        if self.frame_system == "imrf":
            if self.fc_mpa is None or self.fy_mpa is None:
                return None
            beta1 = self._beta1(self.fc_mpa)
            return 0.85 * self.fc_mpa * beta1 / self.fy_mpa * (3.0 / 7.0)
        return 0.025

    def _reinforcement_ratio_state(self, area_mm2, b_mm, h_mm):
        """
        Calculate rho = As/(b*h) and check the gross-section reinforcement
        limits for one face of a beam.

        SMRF: rho_max = 0.025
        IMRF: rho_max = rho at epsilon_s = 0.004
        Both: rho_min = max(0.25*sqrt(f'c)/fy, 1.4/fy)
        """
        area_mm2 = float(area_mm2)
        b_mm = float(b_mm)
        h_mm = float(h_mm)
        rho_max = self._rho_max()
        state = {
            "area_mm2": area_mm2,
            "gross_area_mm2": b_mm * h_mm,
            "rho": None,
            "rho_min": None,
            "rho_max": rho_max,
            "passed": None,
        }
        if b_mm <= 0 or h_mm <= 0:
            return state

        state["rho"] = area_mm2 / state["gross_area_mm2"]
        if self.fc_mpa is None or self.fy_mpa is None:
            return state

        state["rho_min"] = max(
            0.25 * math.sqrt(self.fc_mpa) / self.fy_mpa,
            1.4 / self.fy_mpa,
        )
        if rho_max is not None:
            state["passed"] = (
                state["rho"] + self.RHO_COMPARISON_TOL >= state["rho_min"]
                and state["rho"] <= rho_max + self.RHO_COMPARISON_TOL
            )
        return state

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

            top_rho = self._reinforcement_ratio_state(
                data["top_bar_area"], data["b"], data["h"]
            )
            bottom_rho = self._reinforcement_ratio_state(
                data["bottom_bar_area"], data["b"], data["h"]
            )
            ratio_states = (("top", top_rho), ("bottom", bottom_rho))
            rho_passed = (
                all(state["passed"] for _, state in ratio_states)
                if all(state["passed"] is not None for _, state in ratio_states)
                else None
            )
            for face, state in ratio_states:
                if state["passed"] is False:
                    self.errors.append({
                        "section": data["name"],
                        "issue": f"{face.title()} reinforcement ratio is outside permitted limits",
                        "details": (
                            f"rho={state['rho']:.6f}, "
                            f"rho_min={state['rho_min']:.6f}, "
                            f"rho_max={state['rho_max']:.6f}"
                        ),
                    })

            self.results.append({
                "section": data["name"], "b": data["b"], "h": data["h"], "d": d,
                "clear_cover": data["clear_cover"],
                "stirrup_dia": data["stirrup_dia"], "s": data["s"],
                "main_bar_dia": data["main_bar_dia"],
                "smallest_long_dia": data["smallest_long_dia"],
                "s_max": s_max, "spacing_passed": spacing_passed,
                "top_bars": data["top_bars"], "bottom_bars": data["bottom_bars"],
                "bars_passed": bars_passed,
                "top_bar_area": data["top_bar_area"],
                "bottom_bar_area": data["bottom_bar_area"],
                "top_rho": top_rho,
                "bottom_rho": bottom_rho,
                "rho_passed": rho_passed,
                "passed": (
                    spacing_passed
                    and bars_passed
                    and rho_passed is not False
                ),
            })

        return self.results

    # ------------------------------------------------------------------
    # Step 7 - Mid-span spacing check (s <= d/2), located from the elevation
    # ------------------------------------------------------------------

    def _find_cut_bubbles(self):
        """
        cut-id -> [(x, y)] for every section-cut label bubble (e.g. '1a', '1b').

        Instead of filtering by layer name (varies across drawings), we build
        the set of expected cut IDs from extracted section names and accept any
        TEXT/MTEXT whose content matches.
        """
        known_ids = set()
        for s in self.section_data:
            known_ids.add(self._normalize_cut_id(s["name"].split("-")[0].strip()))

        bubbles = defaultdict(list)
        for e in self.msp:
            if e.dxftype() not in ("TEXT", "MTEXT"):
                continue
            cleaned = strip_mtext_codes(e.dxf.text or "")
            m = CUT_BUBBLE_RE.match(cleaned.strip())
            if not m:
                continue
            cut_id = m.group(1)
            if self._normalize_cut_id(cut_id) not in known_ids:
                continue
            try:
                x, y, _ = e.dxf.insert
            except Exception:
                continue
            bubbles[cut_id].append((x, y))
        return bubbles

    def _assign_bubbles_to_beams(self):
        """Assign each cut-bubble position to the nearest elevation beam (by y)
        that overlaps in x, plus any other beam within 2x that distance.

        This handles the common case where one bubble label serves multiple
        stacked beams — the closest beam gets it, and nearby beams sharing the
        same x column also receive a copy."""
        assignment = defaultdict(list)
        for cut_id, positions in self._cut_bubbles.items():
            for bx, by in positions:
                candidates = []
                for i, beam in enumerate(self.elevations):
                    faces = beam.get("column_faces") or []
                    if not faces:
                        continue
                    xmin, xmax = min(faces), max(faces)
                    if not (xmin - self.CUT_BUBBLE_X_MARGIN <= bx
                            <= xmax + self.CUT_BUBBLE_X_MARGIN):
                        continue
                    y_center = (beam["y_bottom"] + beam["y_top"]) / 2
                    dist = abs(by - y_center)
                    candidates.append((dist, i))
                if not candidates:
                    continue
                candidates.sort()
                best_dist = candidates[0][0]
                threshold = min(
                    max(best_dist * 2, self.CUT_BUBBLE_Y_MARGIN_MIN),
                    self.CUT_BUBBLE_Y_MAX,
                )
                for dist, i in candidates:
                    if dist <= threshold:
                        assignment[i].append((cut_id, bx))
        return assignment

    def _bubbles_on_beam(self, beam):
        """[(cut_id, x)] for cut bubbles whose position lies on this beam."""
        idx = self.elevations.index(beam)
        return self._bubble_assignments.get(idx, [])

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

    def _section_by_cut_id(self, cut_id, beam_xy=None):
        target = self._normalize_cut_id(cut_id)
        matches = [d for d in self.section_data
                   if self._section_cut_id(d["name"]) == target]
        if not matches:
            return None
        if len(matches) == 1 or beam_xy is None:
            return matches[0]
        bx, by = beam_xy
        return min(matches, key=lambda d: (d["label_x"] - bx) ** 2 + (d["label_y"] - by) ** 2)

    def _flexural_end_region_width(self, span_width):
        if span_width <= 0:
            return 0
        return max(
            min(span_width * self.FLEXURAL_END_REGION_RATIO, self.FLEXURAL_END_REGION_MAX),
            min(self.FLEXURAL_END_REGION_MIN, span_width / 2),
        )

    def _span_region_markers(self, markers, x0, x1):
        """Markers inside one span, divided into left/middle/right zones.

        Each marker dict gets an ``in_span`` flag: True when the bubble's x
        sits inside [x0, x1] (the span proper), False when it only falls
        within the extended tolerance band.
        """
        width = x1 - x0
        if width <= 0:
            return {"left": [], "middle": [], "right": []}

        end_region = self._flexural_end_region_width(width)
        regions = {"left": [], "middle": [], "right": []}
        for marker in markers:
            x = marker["x"]
            if not (x0 - self.FLEXURAL_SPAN_X_TOL <= x <= x1 + self.FLEXURAL_SPAN_X_TOL):
                continue
            tagged = {**marker, "in_span": x0 <= x <= x1}
            if x <= x0 + end_region:
                regions["left"].append(tagged)
            elif x >= x1 - end_region:
                regions["right"].append(tagged)
            else:
                regions["middle"].append(tagged)
        return regions

    def _direct_section_for_region(self, region_markers, target_x, beam_xy=None):
        if not region_markers:
            return None, None
        for marker in sorted(region_markers, key=lambda item: abs(item["x"] - target_x)):
            section = self._section_by_cut_id(marker["cut_id"], beam_xy=beam_xy)
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
            "top_bars": None,
            "bottom_bars": None,
            "negative": None,
            "positive": None,
            "note": note,
        }

    def _nominal_moment_state(self, section, sign):
        """
        Return the singly-reinforced rectangular-section nominal moment for
        one bending sign. MPa and mm inputs give Mn in kN-m. Negative bending
        uses the top row as tension steel; positive bending uses the bottom.
        """
        negative = sign == "negative"
        as_mm2 = float(section["top_bar_area"] if negative else section["bottom_bar_area"])
        d = float(section["d_negative"] if negative else section["d_positive"])
        b = float(section["b"])
        bars = int(section["top_bars"] if negative else section["bottom_bars"])
        state = {
            "sign": sign,
            "as_mm2": as_mm2,
            "b": b,
            "d": d,
            "bars": bars,
            "a": None,
            "mn_knm": None,
        }
        if self.fc_mpa is None or self.fy_mpa is None or b <= 0 or d <= 0:
            return state

        a = float(as_mm2 * self.fy_mpa / (0.85 * self.fc_mpa * b))
        state["a"] = a
        # A non-positive lever arm is outside this simple rectangular-section
        # model, so Mn is left unavailable.
        if as_mm2 > 0 and d - a / 2 > 0:
            state["mn_knm"] = float(
                as_mm2 * self.fy_mpa * (d - a / 2) / 1_000_000
            )
        return state

    def _flexural_source_from_section(self, section, marker, note):
        return {
            "section": section["name"],
            "cut_id": marker["cut_id"] if marker else self._section_cut_id(section["name"]),
            "top_area": section["top_bar_area"],
            "bottom_area": section["bottom_bar_area"],
            "top_groups": section["top_bar_groups"],
            "bottom_groups": section["bottom_bar_groups"],
            "top_bars": section["top_bars"],
            "bottom_bars": section["bottom_bars"],
            "negative": self._nominal_moment_state(section, "negative"),
            "positive": self._nominal_moment_state(section, "positive"),
            "note": note,
        }

    def _section_by_name(self, section_name, beam_xy=None):
        if not section_name:
            return None
        matches = [s for s in self.section_data if s["name"] == section_name]
        if not matches:
            return None
        if len(matches) == 1 or beam_xy is None:
            return matches[0]
        bx, by = beam_xy
        return min(matches, key=lambda d: (d["label_x"] - bx) ** 2 + (d["label_y"] - by) ** 2)

    def _elevation_source_at(self, beam, x, geometry_section, note):
        """Build top/bottom steel and Mn at x from elevation bar extents."""
        if geometry_section is None:
            return self._empty_flexural_source("no drawn section available for b and d")

        groups = {"top": [], "bottom": []}
        seen = set()
        for callout in beam.get("longitudinal_bars") or []:
            position = callout.get("position")
            if position not in groups:
                continue
            x0, x1 = callout.get("start_x"), callout.get("end_x")
            if x0 is None or x1 is None or not (x0 - 0.5 <= x <= x1 + 0.5):
                continue
            key = (position, round(x0, 1), round(x1, 1),
                   tuple((p["count"], p["diameter"]) for p in callout["bars"]))
            if key in seen:
                continue
            seen.add(key)
            for pair in callout["bars"]:
                count, dia = int(pair["count"]), int(pair["diameter"])
                groups[position].append({
                    "count": count,
                    "diameter": dia,
                    "area": count * bar_area_mm2(dia),
                    "source": "elevation bar extent",
                })

        if not groups["top"] or not groups["bottom"]:
            missing = []
            if not groups["top"]:
                missing.append("top")
            if not groups["bottom"]:
                missing.append("bottom")
            return self._empty_flexural_source(
                f"no {'/'.join(missing)} elevation bar extent found at x={x:.0f}mm"
            )

        pseudo_section = {
            **geometry_section,
            "top_bar_groups": groups["top"],
            "bottom_bar_groups": groups["bottom"],
            "top_bar_area": self._bar_area_total(groups["top"]),
            "bottom_bar_area": self._bar_area_total(groups["bottom"]),
            "top_bars": sum(g["count"] for g in groups["top"]),
            "bottom_bars": sum(g["count"] for g in groups["bottom"]),
        }
        return {
            "section": f"elevation x={x:.0f}mm",
            "cut_id": None,
            "top_area": pseudo_section["top_bar_area"],
            "bottom_area": pseudo_section["bottom_bar_area"],
            "top_groups": groups["top"],
            "bottom_groups": groups["bottom"],
            "top_bars": pseudo_section["top_bars"],
            "bottom_bars": pseudo_section["bottom_bars"],
            "negative": self._nominal_moment_state(pseudo_section, "negative"),
            "positive": self._nominal_moment_state(pseudo_section, "positive"),
            "note": note,
        }

    def _span_moment_samples(self, beam, x0, x1, geometry_section):
        """
        Evaluate Mn in every interval where the drawn longitudinal-bar set is
        constant. This checks the one-quarter rule along the full clear span,
        not only at its midpoint.
        """
        boundaries = [x0, x1]
        for bar in beam.get("longitudinal_bars") or []:
            if bar.get("position") not in ("top", "bottom"):
                continue
            for x in (bar.get("start_x"), bar.get("end_x")):
                if x is not None and x0 < x < x1:
                    boundaries.append(x)
        boundaries = sorted(set(round(x, 6) for x in boundaries))
        points = [(a + b) / 2 for a, b in zip(boundaries, boundaries[1:]) if b > a]
        if not points:
            points = [(x0 + x1) / 2]
        return [self._elevation_source_at(
            beam, x, geometry_section, "from longitudinal-bar extents"
        ) for x in points]

    @staticmethod
    def _moment_ratio_check(label, provided, required, note=""):
        return {
            "label": label,
            "provided": provided,
            "required": required,
            "passed": (provided >= required
                       if provided is not None and required is not None else None),
            "note": note,
        }

    def check_flexural_span_areas(self):
        """
        For each elevation span, calculate Mn,l and Mn,r from the mapped joint
        sections and Mn along the span from the drawn longitudinal-bar extents,
        then apply the one-half and one-quarter moment-strength rules.
        """
        for beam in self.elevations:
            beam["flexural_materials"] = {
                "fc_mpa": self.fc_mpa,
                "fy_mpa": self.fy_mpa,
            }
            markers = self._unique_bubbles_on_beam(beam)
            previous_left = None
            summaries = []
            faces = beam.get("column_faces") or []
            beam_xy = (
                (sum(faces) / len(faces), (beam["y_bottom"] + beam["y_top"]) / 2)
                if faces else None
            )

            donor_section = None
            if not markers and faces:
                beam_left_x = min(faces)
                candidates = [
                    s for s in self.section_data
                    if s["label_x"] < beam_left_x
                ]
                if candidates:
                    donor_section = min(candidates, key=lambda s: beam_left_x - s["label_x"])

            first_span_left = None
            first_span_right = None
            first_span_middle = None

            for index, (x0, x1) in enumerate(beam.get("spans") or [], start=1):
                regions = self._span_region_markers(markers, x0, x1)
                if index > 1:
                    for key in ("left", "middle", "right"):
                        first = {"left": first_span_left, "middle": first_span_middle, "right": first_span_right}[key]
                        if first is not None:
                            regions[key] = [m for m in regions[key] if m.get("in_span", True)]
                mid_x = (x0 + x1) / 2

                left_section, left_marker = self._direct_section_for_region(regions["left"], x0, beam_xy=beam_xy)
                if left_section is not None:
                    left = self._flexural_source_from_section(left_section, left_marker, "from span left cut")
                    previous_left = left
                elif first_span_left is not None:
                    left = {**first_span_left, "note": "from first span left section"}
                elif previous_left is not None:
                    left = {**previous_left, "note": "fallback from previous span left section"}
                elif donor_section is not None:
                    left = self._flexural_source_from_section(
                        donor_section, None, f"fallback from same-ID beam ({beam['id']})"
                    )
                    previous_left = left
                elif left_marker is not None:
                    left = self._empty_flexural_source(
                        f"left cut {left_marker['cut_id']} has no extracted section drawing"
                    )
                else:
                    left = self._empty_flexural_source("no left section cut found")

                right_section, right_marker = self._direct_section_for_region(regions["right"], x1, beam_xy=beam_xy)
                if right_section is not None:
                    right = self._flexural_source_from_section(right_section, right_marker, "from span right cut")
                elif first_span_right is not None:
                    right = {**first_span_right, "note": "from first span right section"}
                elif left["section"] is not None:
                    right = {**left, "note": "fallback from span left section"}
                elif right_marker is not None:
                    right = self._empty_flexural_source(
                        f"right cut {right_marker['cut_id']} has no extracted section drawing"
                    )
                else:
                    right = self._empty_flexural_source("no right section cut found and no left fallback")

                if left["section"] is None and right["section"] is not None:
                    left = {**right, "note": "fallback from span right section"}
                    previous_left = left

                middle_section, middle_marker = self._direct_section_for_region(regions["middle"], mid_x, beam_xy=beam_xy)
                if middle_section is not None:
                    middle = self._flexural_source_from_section(middle_section, middle_marker, "from span middle cut")
                elif first_span_middle is not None:
                    middle = {**first_span_middle, "note": "from first span middle section"}
                else:
                    geometry_section = (
                        self._section_by_name(left.get("section"), beam_xy=beam_xy) or
                        self._section_by_name(right.get("section"), beam_xy=beam_xy)
                    )
                    middle = self._elevation_source_at(
                        beam, mid_x, geometry_section,
                        "from longitudinal-bar extents at span midpoint",
                    )

                if middle["section"] is not None:
                    if left["section"] is None:
                        left = {**middle, "note": "fallback from span middle section"}
                        previous_left = left
                    if right["section"] is None:
                        right = {**middle, "note": "fallback from span middle section"}
                elif left["section"] is not None and right["section"] is not None:
                    pick = left if (left["top_area"] or 0) <= (right["top_area"] or 0) else right
                    middle = {**pick, "note": "fallback from joint section (min)"}
                elif left["section"] is not None:
                    middle = {**left, "note": "fallback from span left section"}
                elif right["section"] is not None:
                    middle = {**right, "note": "fallback from span right section"}

                if index == 1:
                    if left["section"] is not None:
                        first_span_left = left
                    if right["section"] is not None:
                        first_span_right = right
                    if middle["section"] is not None:
                        first_span_middle = middle

                mn_l_minus = left["top_area"]
                mn_l_plus = left["bottom_area"]
                mn_r_minus = right["top_area"]
                mn_r_plus = right["bottom_area"]
                mn_mid_minus = middle["top_area"]
                mn_mid_plus = middle["bottom_area"]

                joint_values = [v for v in (
                    mn_l_minus, mn_l_plus, mn_r_minus, mn_r_plus
                ) if v is not None]
                max_joint_mn = max(joint_values) if len(joint_values) == 4 else None

                if self.frame_system == "imrf":
                    joint_div = 3
                    span_div = 5
                else:
                    joint_div = 2
                    span_div = 4
                span_frac_mn = max_joint_mn / span_div if max_joint_mn is not None else None

                geometry_section = (
                    self._section_by_name(left.get("section"), beam_xy=beam_xy) or
                    self._section_by_name(right.get("section"), beam_xy=beam_xy)
                )
                span_samples = self._span_moment_samples(
                    beam, x0, x1, geometry_section
                ) if geometry_section is not None else []
                negative_samples = [
                    s["top_area"] for s in span_samples
                    if s.get("top_area") is not None
                ]
                positive_samples = [
                    s["bottom_area"] for s in span_samples
                    if s.get("bottom_area") is not None
                ]
                all_samples_available = bool(span_samples) and all(
                    s.get("top_area") is not None and s.get("bottom_area") is not None
                    for s in span_samples
                )
                min_span_negative = min(negative_samples) if all_samples_available else mn_mid_minus
                min_span_positive = min(positive_samples) if all_samples_available else mn_mid_plus
                moment_checks = [
                    self._moment_ratio_check(
                        f"Mn,l+ >= Mn,l-/{joint_div}", mn_l_plus,
                        mn_l_minus / joint_div if mn_l_minus is not None else None,
                    ),
                    self._moment_ratio_check(
                        f"Mn,r+ >= Mn,r-/{joint_div}", mn_r_plus,
                        mn_r_minus / joint_div if mn_r_minus is not None else None,
                    ),
                    self._moment_ratio_check(
                        f"min Mn- >= max As/{span_div}",
                        min_span_negative, span_frac_mn,
                    ),
                    self._moment_ratio_check(
                        f"min Mn+ >= max As/{span_div}",
                        min_span_positive, span_frac_mn,
                    ),
                ]

                summary = {
                    "span_index": index,
                    "span_start": x0,
                    "span_end": x1,
                    "left": left,
                    "right": right,
                    "middle": middle,
                    "mn_l_minus_area": mn_l_minus,
                    "mn_l_plus_area": mn_l_plus,
                    "mn_r_minus_area": mn_r_minus,
                    "mn_r_plus_area": mn_r_plus,
                    "mn_mid_minus_area": mn_mid_minus,
                    "mn_mid_plus_area": mn_mid_plus,
                    "mn_l_minus": mn_l_minus,
                    "mn_l_plus": mn_l_plus,
                    "mn_r_minus": mn_r_minus,
                    "mn_r_plus": mn_r_plus,
                    "mn_mid_minus": mn_mid_minus,
                    "mn_mid_plus": mn_mid_plus,
                    "max_joint_mn": max_joint_mn,
                    "min_span_negative": min_span_negative,
                    "min_span_positive": min_span_positive,
                    "span_moment_samples": span_samples,
                    "moment_checks": moment_checks,
                }
                summaries.append(summary)

                for check in moment_checks:
                    if check["passed"] is False:
                        self.errors.append({
                            "beam": beam["label"], "span": index,
                            "issue": f"Flexural area rule failed: {check['label']}",
                            "details": (f"provided={check['provided']:.0f}mm², "
                                        f"required={check['required']:.0f}mm²"),
                        })

            beam["flexural_span_areas"] = summaries

    def _beam_effective_depth(self, beam):
        """
        d for a beam, taken from the drawn support section whose cut bubble
        sits on this beam (all sections of a beam share the same d). Returns
        (d, section_name) or (None, None).
        """
        on_beam = {cid for cid, _ in self._bubbles_on_beam(beam)}
        faces = beam.get("column_faces") or []
        beam_xy = (
            (sum(faces) / len(faces), (beam["y_bottom"] + beam["y_top"]) / 2)
            if faces else None
        )
        for cid in on_beam:
            section = self._section_by_cut_id(cid, beam_xy=beam_xy)
            if section is not None:
                return self._effective_depth(section), section["name"]
        if faces:
            beam_left_x = min(faces)
            candidates = [
                s for s in self.section_data
                if s["label_x"] < beam_left_x
            ]
            if candidates:
                nearest = min(candidates, key=lambda s: beam_left_x - s["label_x"])
                return self._effective_depth(nearest), nearest["name"]
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
        # Elevations are parsed early: the beam labels carry the unit system
        # (metric vs imperial) which group_sections and extract_sections need
        # for tolerance scaling and unit conversion.
        self.parse_elevations()
        self.group_sections()
        self.extract_sections()
        self._cut_bubbles = self._find_cut_bubbles()
        self._bubble_assignments = self._assign_bubbles_to_beams()
        self._correct_section_dimensions()
        results = self.check_sections()
        self._associate_sections_to_beams()
        self.check_clear_span_depth_ratio()
        self.check_flexural_span_areas()
        self.check_midspan()
        return results

    def _correct_section_dimensions(self):
        """Override drawn section b/h with the beam label's authoritative values.

        The cross-section drawings often measure slightly differently from the
        label (e.g. drawn h=650 vs label h=660 for a 26" beam).  The label
        dimensions are the engineer's intent; use them for all calculations.
        """
        for i, section in enumerate(self.section_data):
            sec_cut = self._section_cut_id(section["name"])
            best_beam = None
            best_dist = float("inf")
            for beam_idx, bubbles in self._bubble_assignments.items():
                beam = self.elevations[beam_idx]
                for cut_id, bx in bubbles:
                    if self._normalize_cut_id(cut_id) != sec_cut:
                        continue
                    faces = beam.get("column_faces") or []
                    if faces:
                        cx = sum(faces) / len(faces)
                        cy = (beam["y_bottom"] + beam["y_top"]) / 2
                    else:
                        cx, cy = 0, 0
                    dist = ((section["label_x"] - cx) ** 2
                            + (section["label_y"] - cy) ** 2) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_beam = beam
            if best_beam is not None:
                section["h"] = best_beam["section_depth"]
                section["b"] = best_beam["section_width"]

    def _associate_sections_to_beams(self):
        """Attach section check results whose cut IDs appear on the beam."""
        for beam in self.elevations:
            beam["section_checks"] = []
            cut_ids = {self._normalize_cut_id(cid)
                       for cid, _ in self._bubbles_on_beam(beam)}
            if not cut_ids:
                continue
            faces = beam.get("column_faces") or []
            beam_xy = (
                (sum(faces) / len(faces), (beam["y_bottom"] + beam["y_top"]) / 2)
                if faces else None
            )
            best_per_cut: dict[str, tuple[int, float]] = {}
            for i, section in enumerate(self.section_data):
                if i >= len(self.results):
                    continue
                sec_cut = self._section_cut_id(section["name"])
                if sec_cut not in cut_ids:
                    continue
                if beam_xy:
                    dist = ((section["label_x"] - beam_xy[0]) ** 2
                            + (section["label_y"] - beam_xy[1]) ** 2) ** 0.5
                else:
                    dist = 0.0
                prev = best_per_cut.get(sec_cut)
                if prev is None or dist < prev[1]:
                    best_per_cut[sec_cut] = (i, dist)
            for sec_cut in sorted(best_per_cut):
                idx = best_per_cut[sec_cut][0]
                beam["section_checks"].append(self.results[idx])


if __name__ == "__main__":
    checker = BeamStirrupChecker("Structural Drawing_ SNIGDHOTARA.dxf")
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
        print(f"      rho: top={r['top_rho']['rho']:.6f} "
              f"bottom={r['bottom_rho']['rho']:.6f} "
              f"limits=[{r['top_rho']['rho_min']}, {r['top_rho']['rho_max']}] -> "
              f"{'N/A' if r['rho_passed'] is None else ('PASS' if r['rho_passed'] else 'FAIL')}")

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
