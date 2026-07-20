import re
import ezdxf
from collections import defaultdict

try:
    from .parser import strip_mtext_codes, get_line_points, distance
except ImportError:
    from parser import strip_mtext_codes, get_line_points, distance


BEAM_LABEL_RE = re.compile(r'([A-Za-z]+-?\d+[A-Za-z]?)\s*\(\s*(\d+)\s*[xX]\s*(\d+)\s*\)')


def is_beam_dim_layer(layer):
    """True for the 'beam dim' layer family (case-insensitive), not a literal 'BEAM DIM' layer."""
    if not layer:
        return False
    name = layer.strip().lower()
    return name == "beam dim" or name.startswith("beam dim @")


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class BeamSectionParser:
    """
    Parses longitudinal beam-section drawings from a DXF file and checks the
    lap-splice length at every support against the rule of thumb (>= 2h).

    Pipeline (called via run()):
      1. load()                  - read DXF
      2. collect_vertical_lines() - gather vertical LINE/LWPOLYLINE segments on the
                                    BEAM SEC layer, merged into logical lines by x position
      3. find_beam_labels()      - locate MTEXT/TEXT containing "BEAM" with a (bxh) pattern
      4. assign_lines_to_beams() - map every logical vertical line to the nearest
                                    beam label below it; lines with no label below are dropped
      5. group_supports()        - order each beam's lines by x and pair them into
                                    supports (columns): lines 1&2 -> support 1, 3&4 -> support 2, ...
      6. compute_lap_splices()   - for every line except the very first one, find the
                                    nearest horizontal "beam dim" entity above its bottom
                                    point and take its length as the lap-splice distance
      7. check_lap_splices()     - compare each lap-splice distance against 2h and record pass/fail
    """

    LINE_MERGE_TOL = 0.5
    LAP_TOLERANCE_RATIO = 0.05   # allowance for "very slightly less than 2h"

    def __init__(self, dxf_file):
        self.dxf_file = dxf_file
        self.doc = None
        self.msp = None

        self.logical_lines = []      # [{x, ymin, ymax, segments}]
        self.beam_labels = []        # [{entity, x, y, text, name, b, h}]
        self.beams = defaultdict(list)     # beam_label_idx -> [logical_line idx], sorted by x
        self.supports = {}           # beam_label_idx -> [[line_idx, line_idx], ...]
        self.lap_splices = {}        # logical_line idx -> lap distance (or None)

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
    # Step 2 - Collect vertical lines on the BEAM SEC layer
    # ------------------------------------------------------------------

    def collect_vertical_lines(self):
        """
        Gather every vertical LINE/2-point-LWPOLYLINE on the BEAM SEC layer and
        merge segments sharing the same x into one logical line (a bar is often
        drawn with a gap to indicate where it is spliced).
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

        raw.sort(key=lambda r: r[0])
        for x, ymin, ymax, entity in raw:
            merged = next((l for l in self.logical_lines if abs(l["x"] - x) <= self.LINE_MERGE_TOL), None)
            if merged is None:
                self.logical_lines.append({"x": x, "ymin": ymin, "ymax": ymax, "segments": [entity]})
            else:
                merged["ymin"] = min(merged["ymin"], ymin)
                merged["ymax"] = max(merged["ymax"], ymax)
                merged["segments"].append(entity)

        print(f"Found {len(self.logical_lines)} logical vertical lines on BEAM SEC.")

    # ------------------------------------------------------------------
    # Step 3 - Find beam labels (text containing BEAM and a bxh dimension)
    # ------------------------------------------------------------------

    def find_beam_labels(self):
        """
        A beam label is any TEXT/MTEXT containing the word "BEAM" together with a
        "(bxh)" pattern, e.g. "LONGITUDINAL SECTION OF FLOOR BEAM-FB1(300x600)".

        Matching is done on the raw entity text rather than the MTEXT-cleaned
        text: stripping formatting codes can merge a trailing font-change code
        with the visible label and swallow it (no protecting ";" in between).
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

            name, b, h = match.group(1), int(match.group(2)), int(match.group(3))
            x, y, _ = e.dxf.insert
            self.beam_labels.append({
                "entity": e, "x": x, "y": y,
                "text": strip_mtext_codes(raw), "name": name, "b": b, "h": h,
            })

        print(f"Found {len(self.beam_labels)} beam labels.")
        for lbl in self.beam_labels:
            print(f"  {lbl['name']} ({lbl['b']}x{lbl['h']}) at ({lbl['x']:.1f}, {lbl['y']:.1f})")

    # ------------------------------------------------------------------
    # Step 4 - Assign vertical lines to the nearest beam label below them
    # ------------------------------------------------------------------

    def assign_lines_to_beams(self):
        """
        For every logical vertical line, pick the nearest beam label whose
        y-position is below the line's bottom point. Lines with no such
        label underneath them are not part of any beam and are dropped.
        """
        for idx, line in enumerate(self.logical_lines):
            best_idx, best_dist = -1, None
            for lbl_idx, lbl in enumerate(self.beam_labels):
                if lbl["y"] >= line["ymin"]:
                    continue   # label must lie below the line's bottom point
                d = distance((line["x"], line["ymin"]), (lbl["x"], lbl["y"]))
                if best_dist is None or d < best_dist:
                    best_dist, best_idx = d, lbl_idx

            if best_idx != -1:
                self.beams[best_idx].append(idx)

        for lbl_idx, line_idxs in self.beams.items():
            self.beams[lbl_idx] = sorted(line_idxs, key=lambda i: self.logical_lines[i]["x"])
            lbl = self.beam_labels[lbl_idx]
            print(f"  Beam {lbl['name']} @ ({lbl['x']:.1f},{lbl['y']:.1f}): "
                  f"{len(self.beams[lbl_idx])} vertical lines.")

    # ------------------------------------------------------------------
    # Step 5 - Group each beam's vertical lines into supports (columns)
    # ------------------------------------------------------------------

    def group_supports(self):
        """
        Within each beam, consecutive pairs of vertical lines (sorted by x)
        mark the two faces of one support: lines 1&2 -> support 1, lines
        3&4 -> support 2, and so on.
        """
        for lbl_idx, line_idxs in self.beams.items():
            self.supports[lbl_idx] = [line_idxs[i:i + 2] for i in range(0, len(line_idxs), 2)]

    # ------------------------------------------------------------------
    # Step 6 - Compute the lap-splice distance for every face-of-support line
    # ------------------------------------------------------------------

    def _collect_beam_dim_horizontals(self):
        """Return [(xmin, xmax, y, length)] for every horizontal entity on the beam dim layer."""
        horizontals = []
        for e in self.msp:
            if not is_beam_dim_layer(getattr(e.dxf, "layer", None)):
                continue

            if e.dxftype() in ("LINE", "LWPOLYLINE"):
                if e.dxftype() == "LWPOLYLINE" and len(e.get_points()) != 2:
                    continue
                pp = get_line_points(e)
                if pp is None:
                    continue
                (x1, y1), (x2, y2) = pp
                if abs(y1 - y2) > 0.01:
                    continue
                horizontals.append((x1, x2, y1, abs(x2 - x1)))

            elif e.dxftype() == "DIMENSION":
                p2, p3 = e.dxf.defpoint2, e.dxf.defpoint3
                if abs(p2[1] - p3[1]) > 0.01:
                    continue
                horizontals.append((min(p2[0], p3[0]), max(p2[0], p3[0]), p2[1], e.get_measurement()))

        return horizontals

    def compute_lap_splices(self):
        """
        For every face-of-support vertical line (every line in a beam except the
        very first one), find the nearest horizontal "beam dim" entity that lies
        above the line's bottom point and spans across its x position. Its
        length is taken as the lap-splice distance.
        """
        horizontals = self._collect_beam_dim_horizontals()

        for lbl_idx, line_idxs in self.beams.items():
            for line_idx in line_idxs[1:]:   # skip the beam's very first line
                line = self.logical_lines[line_idx]
                x, bottom_y = line["x"], line["ymin"]

                best_len, best_y = None, None
                for xmin, xmax, y, length in horizontals:
                    if not (xmin - 0.01 <= x <= xmax + 0.01):
                        continue
                    if y <= bottom_y:
                        continue   # must be above the bottom point
                    if best_y is None or y < best_y:
                        best_y, best_len = y, length

                self.lap_splices[line_idx] = best_len

    # ------------------------------------------------------------------
    # Step 7 - Compare lap-splice distances against 2h
    # ------------------------------------------------------------------

    def check_lap_splices(self):
        """
        Compare each face-of-support lap-splice distance against 2h (h = beam
        depth from the beam label). Passes if the distance is >= 2h, or is
        within LAP_TOLERANCE_RATIO of 2h ("only very slightly less").
        """
        for lbl_idx, line_idxs in self.beams.items():
            lbl = self.beam_labels[lbl_idx]
            required = 2 * lbl["h"]

            for support_no, pair in enumerate(self.supports[lbl_idx], start=1):
                for line_idx in pair:
                    lap = self.lap_splices.get(line_idx)
                    line = self.logical_lines[line_idx]

                    if line_idx not in self.lap_splices:
                        continue   # the beam's very first line is never checked

                    if lap is None:
                        passed = None
                        self.errors.append({
                            "beam": lbl["name"], "support": support_no, "x": line["x"],
                            "issue": "No lap-splice dimension found",
                        })
                    else:
                        passed = lap >= required * (1 - self.LAP_TOLERANCE_RATIO)
                        if not passed:
                            self.errors.append({
                                "beam": lbl["name"], "support": support_no, "x": line["x"],
                                "issue": "Lap splice shorter than 2h",
                                "details": f"lap={lap:.1f}mm, 2h={required}mm",
                            })

                    self.results.append({
                        "beam": lbl["name"], "b": lbl["b"], "h": lbl["h"],
                        "support": support_no, "x": line["x"],
                        "lap_splice": lap, "required_2h": required,
                        "passed": passed,
                    })

        return self.results

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        self.load()
        self.collect_vertical_lines()
        self.find_beam_labels()
        self.assign_lines_to_beams()
        self.group_supports()
        self.compute_lap_splices()
        return self.check_lap_splices()


if __name__ == "__main__":
    beam_parser = BeamSectionParser("Structural Drawing.dxf")
    results = beam_parser.run()
    print("\nResults:")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL" if r["passed"] is False else "N/A"
        lap_str = f"{r['lap_splice']:.1f}mm" if r["lap_splice"] is not None else "N/A"
        print(f"  Beam {r['beam']} support {r['support']} at x={r['x']:.1f}mm: "
              f"lap splice = {lap_str}, required 2h = {r['required_2h']}mm -> {status}")