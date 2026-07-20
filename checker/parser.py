import ezdxf
from ezdxf.addons import Importer
import math
import re
from shapely.geometry import Polygon, Point
from collections import defaultdict, deque


DIAMETER_TO_AREA_MM = {
    6: 32, 10: 71, 12: 129, 16: 200, 20: 284,
    22: 387, 25: 510, 29: 645, 32: 819, 36: 1006,
    38: 1140, 43: 1452, 50: 2027, 57: 2581, 64: 3167
}

location_category_dict = {
    "Bagerhat": "I", "Bandarban": "III",
    "Barguna": "I", "Barisal": "I",
    "Bhola": "I",
    "Bogra": "III", "Brahmanbaria": "III",
    "Chandpur": "II",
    "Chapainababganj": "I", "Chittagong": "III",
    "Chuadanga": "I",
    "Comilla": "II",
    "Cox's Bazar": "III",
    "Dhaka": "II",
    "Dinajpur": "II",
    "Faridpur": "II",
    "Feni": "II",
    "Gaibandha": "III",
    "Gazipur": "II",
    "Gopalganj": "I",
    "Habiganj": "IV",
    "Jaipurhat": "II",
    "Jamalpur": "IV",
    "Jessore": "I",
    "Jhalokati": "I",
    "Jhenaidah": "I",
    "Khagrachari": "III",
    "Khulna": "I",
    "Kishoreganj": "IV",
    "Kurigram": "IV",
    "Kushtia": "II",
    "Lakshmipur": "II",
    "Lalmanirhat": "III",
    "Madaripur": "II",
    "Magura": "I",
    "Manikganj": "II",
    "Maulvibazar": "IV",
    "Meherpur": "I",
    "Mongla": "I",
    "Munshiganj": "II",
    "Mymensingh": "IV",
    "Narail": "I",
    "Narayanganj": "II",
    "Narsingdi": "III",
    "Natore": "II",
    "Naogaon": "II",
    "Netrakona": "IV",
    "Nilphamari": "I",
    "Noakhali": "II",
    "Pabna": "II",
    "Panchagarh": "II",
    "Patuakhali": "I",
    "Pirojpur": "I",
    "Rajbari": "II",
    "Rajshahi": "I",
    "Rangamati": "III",
    "Rangpur": "III",
    "Satkhira": "I",
    "Shariatpur": "II",
    "Sherpur": "IV",
    "Sirajganj": "III",
    "Srimangal": "IV",
    "Sunamganj": "IV",
    "Sylhet": "IV",
    "Tangail": "III",
    "Thakurgaon": "II",
}

occupancy_dict = {
    # I
    "Agricultural facilities": "I",
    "Temporary facilities": "I",
    "Minor storage facilities": "I",

    # II
    "Buildings and other structures where less than 300 people congregate in one area": "II",

    # III
    "Buildings and other structures where more than 300 people congregate in one area": "III",
    "Buildings and other structures with day care facilities with a capacity greater than 150": "III",
    "Buildings and other structures with elementary school or secondary school facilities with a capacity greater than 250": "III",
    "Buildings and other structures with a capacity greater than 500 for colleges or adult education facilities": "III",
    "Healthcare facilities with a capacity of 50 or more resident patients, but not having surgery or emergency treatment facilities": "III",
    "Jails and detention facilities": "III",
    "Power generating stations": "III",
    "Water treatment facilities": "III",
    "Sewage treatment facilities": "III",
    "Telecommunication centers": "III",

    # IV
    "Hospitals and other healthcare facilities having surgery or emergency treatment facilities": "IV",
    "Fire, rescue, ambulance, and police stations and emergency vehicle garages": "IV",
    "Designated earthquake, hurricane, or other emergency shelters": "IV",
    "Designated emergency preparedness, communication, operation centers and other facilities required for emergency response": "IV",
    "Power generating stations and other public utility facilities required in an emergency": "IV",
    "Ancillary structures including, but not limited to, communication towers, fuel storage tanks, cooling towers": "IV",
    "Electrical substation structures, fire water storage tanks or other structures housing or supporting water, or other fire-suppression material or equipment required for operation of Occupancy Category IV structures during an emergency": "IV",
    "Aviation control towers, air traffic control centers, and emergency aircraft hangars": "IV",
    "Community water storage facilities and pump structures required to maintain water pressure for fire suppression": "IV",
    "Buildings and other structures having critical national defense functions": "IV",
}

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def strip_mtext_codes(raw):
    """Remove AutoCAD MTEXT formatting codes and return plain text."""
    text = re.sub(r'%%.', '', raw)
    text = re.sub(r'\\pi[\d.]+;', '', text)
    text = re.sub(r'\\P', ' ', text)
    # Toggle codes (underline/overline/strike-through/alignment) have no
    # trailing ";" - strip them first, otherwise the catch-all below treats
    # everything up to the *next* unrelated ";" (e.g. a following font code)
    # as part of the toggle and swallows real label text along with it.
    text = re.sub(r'\\[LlOoKk]', '', text)
    text = re.sub(r'\\A\d', '', text)
    text = re.sub(r'\{\\[^;]*;([^}]*)\}', r'\1', text)
    text = re.sub(r'[{}]', '', text)
    text = re.sub(r'\\[a-zA-Z][^;]*;', '', text)
    return text.strip()


def get_text_value(entity):
    """Return the raw string from a TEXT or MTEXT entity."""
    if entity is None:
        return None
    return entity.dxf.text


def find_text_in_rect(msp, minx, miny, maxx, maxy, keyword):
    """
    Return all TEXT/MTEXT entities inside the bbox whose cleaned text
    contains the given keyword (after stripping non-alphanumeric chars).
    """
    results = []
    for e in msp:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        x, y = e.dxf.insert.x, e.dxf.insert.y
        if not (minx <= x <= maxx and miny <= y <= maxy):
            continue
        cleaned = strip_mtext_codes(e.dxf.text)
        normalized = re.sub(r'[^a-z0-9]', '', cleaned.lower())
        if keyword in normalized:
            results.append(e)
    return results


def find_nearest_text(circle, msp):
    """Return the (entity, distance) of the TEXT/MTEXT closest to the circle center."""
    cx, cy = circle.dxf.center.x, circle.dxf.center.y
    nearest, min_dist = None, float("inf")
    for e in msp.query("TEXT MTEXT"):
        d = math.hypot(cx - e.dxf.insert.x, cy - e.dxf.insert.y)
        if d < min_dist:
            min_dist, nearest = d, e
    return nearest, min_dist


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def get_bbox(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def get_vertices(poly):
    return [(x, y) for x, y, *_ in poly.get_points()]


def to_polygon(poly):
    return Polygon([(x, y) for x, y, *_ in poly.get_points()])


def circle_to_shape(circle):
    c = circle.dxf.center
    return Point(c.x, c.y).buffer(circle.dxf.radius)


def distance(p1, p2):
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def pyth(hyp, leg):
    """Return the other leg given hypotenuse and one leg."""
    return math.sqrt(hyp * hyp - leg * leg)


def is_bbox_inside(inner_pts, outer_pts):
    minx1, miny1, maxx1, maxy1 = get_bbox(inner_pts)
    minx2, miny2, maxx2, maxy2 = get_bbox(outer_pts)
    return minx1 > minx2 and miny1 > miny2 and maxx1 < maxx2 and maxy1 < maxy2


def is_inscribed(inner_poly, outer_poly):
    return to_polygon(outer_poly).covers(to_polygon(inner_poly))


def is_circle_inside_rectangle(minx, miny, maxx, maxy, circle):
    cx, cy, r = circle.dxf.center.x, circle.dxf.center.y, circle.dxf.radius
    return cx - r >= minx and cx + r <= maxx and cy - r >= miny and cy + r <= maxy


def is_parallel(u1, u2, v1, v2):
    eps = 0.001
    if abs(u2[0] - u1[0]) <= eps:
        return abs(v2[0] - v1[0]) <= eps
    if abs(v1[0] - v2[0]) <= eps:
        return False
    if abs(u1[1] - u2[1]) <= eps:
        return abs(v1[1] - v2[1]) <= eps
    if abs(v1[1] - v2[1]) <= eps:
        return False
    m1 = (u2[1] - u1[1]) / (u2[0] - u1[0])
    m2 = (v2[1] - v1[1]) / (v2[0] - v1[0])
    return m1 == m2


# ---------------------------------------------------------------------------
# Bounding-box edge endpoint helpers
# ---------------------------------------------------------------------------

def upep(pts):
    minx, miny, maxx, maxy = get_bbox(pts)
    return [(minx, maxy), (maxx, maxy)]

def downep(pts):
    minx, miny, maxx, maxy = get_bbox(pts)
    return [(minx, miny), (maxx, miny)]

def leftep(pts):
    minx, miny, maxx, maxy = get_bbox(pts)
    return [(minx, miny), (minx, maxy)]

def rightep(pts):
    minx, miny, maxx, maxy = get_bbox(pts)
    return [(maxx, miny), (maxx, maxy)]


# ---------------------------------------------------------------------------
# DXF line helpers
# ---------------------------------------------------------------------------

def get_line_points(line):
    """Return the two endpoints of a LINE or 2-point LWPOLYLINE, sorted low to high."""
    if line.dxftype() == "LINE":
        x, y, _ = line.dxf.start
        a, b, _ = line.dxf.end
    elif line.dxftype() == "LWPOLYLINE" and len(line.get_points()) == 2:
        (x, y), (a, b) = get_vertices(line)
    else:
        return None

    v1, v2 = (x, y), (a, b)
    if x != a and x > a:
        v1, v2 = v2, v1
    elif y != b and y > b:
        v1, v2 = v2, v1
    return [v1, v2]


# ---------------------------------------------------------------------------
# Circle sorting helpers
# ---------------------------------------------------------------------------

def sort_circles_by_x(circles):
    return sorted(circles, key=lambda c: (c.dxf.center.x, c.dxf.center.y))

def sort_circles_by_y(circles):
    return sorted(circles, key=lambda c: (c.dxf.center.y, c.dxf.center.x))


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class ColumnScheduleParser:
    """
    Parses a structural column schedule from a DXF file.

    Pipeline (called via run()):
      1.  load()                   - read DXF
      2.  collect_cells()          - gather closed LWPOLYLINE cells on COLUMN SCHEDULE layer
      3.  build_grid()             - find four bounding lines per cell (up/down/left/right)
      4.  group_columns()          - BFS to cluster connected cells into column groups
      5.  find_column_grid()       - identify which group contains the Column ID label
      6.  extract_material_props() - parse fpc and fyt from the strength text in the schedule
      7.  extract_content()        - pull closed polys, ties, circles, dims, outside circles
      8.  assign_circles_to_edges() - map each rod circle to its nearest polygon edge
      9.  build_cell_positions()    - determine (column_id, floor) label for every cell
      10. compute_edge_spacings()   - clear distance between consecutive circles per edge
      11. compute_steel()           - calculate steel area and Ash confinement (ACI 318)
      12. export()                  - write a visualisation DXF
    """

    def __init__(self, dxf_file):
        self.dxf_file = dxf_file
        self.doc = None
        self.msp = None

        self.cells = []       # closed LWPOLYLINE entities (one per schedule cell)
        self.grid = []        # (up, down, left, right) line entities per cell
        self.col_grid = []    # list of cell-index lists, one per column group
        self.target_group_idx = -1
        self.target_bbox = None

        # Material properties parsed from the schedule
        self.fpc = None   # concrete compressive strength (MPa)
        self.fyt = None   # tie steel yield strength (MPa)

        # Per-cell content (indexed by position within the target column group)
        self.closed_polys = []
        self.ties = []
        self.circles = []
        self.dims_per_col = []
        self.circle_outside = []
        self.circle_text = []
        self.textsin = []
        self.errors = []

        # Per-cell rod-to-edge assignment results
        self.l_flst = []   # flist per cell: list-of-lists, one per polygon edge
        self.l_fhv = []    # edge orientation per cell: 'h', 'v', or 'x'

        # Grid position label for every cell across ALL groups, indexed by cell index.
        # Each entry is (column_label, floor_label) e.g. ("C1", "Below Ground").
        # column_label: leftmost MTEXT in the horizontal band of the cell.
        # floor_label:  topmost  MTEXT in the vertical band of the cell.
        self.position = []

    # ------------------------------------------------------------------
    # Step 1 - Load DXF
    # ------------------------------------------------------------------

    def load(self):
        self.doc = ezdxf.readfile(self.dxf_file)
        self.msp = self.doc.modelspace()
        print("DXF loaded.")
        

    def round_to_int(self, x):
        return math.floor(x + 0.5)
        
    # ------------------------------------------------------------------
    # Step 1a - sort the column and floor labels in the schedule based on their names
    # ------------------------------------------------------------------
    
    def clean_text(self, text):
        return re.sub(r'\(.*?\)', '', text.strip())
    def get_max_floor_number(self, floor_name):
        print(f"Extracting floor number from label*******: '{floor_name}'")
        # remove anything inside brackets
        cleaned = re.sub(r'\(.*?\)', '', floor_name)

        # extract numbers
        nums = re.findall(r'\d+', cleaned)

        # if no number found
        if not nums:
            return 0

        return max(map(int, nums))

    # ------------------------------------------------------------------
    # Step 2 - Collect closed schedule cells
    # ------------------------------------------------------------------

    def collect_cells(self):
        """Find all closed LWPOLYLINE entities on the COLUMN SCHEDULE layer."""
        for e in self.msp:
            if getattr(e.dxf, "layer", None) != "COLUMN SCHEDULE":
                continue
            if e.dxftype() != "LWPOLYLINE":
                continue
            pts = e.get_points()
            if e.closed or pts[0] == pts[-1]:
                self.cells.append(e)
        print(f"Found {len(self.cells)} schedule cells.")

    # ------------------------------------------------------------------
    # Step 3 - Build grid (find the four bounding lines for each cell)
    # ------------------------------------------------------------------

    def build_grid(self):
        """
        For each cell find the nearest parallel line that lies beyond each
        of its four edges and fully spans it. Those four lines define the
        grid cell boundary used later for content extraction.
        """
        for cell in self.cells:
            pts = get_vertices(cell)
            up    = self._find_bounding_line(pts, "up")
            down  = self._find_bounding_line(pts, "down")
            left  = self._find_bounding_line(pts, "left")
            right = self._find_bounding_line(pts, "right")
            self.grid.append((up, down, left, right))

        for i, (u, d, l, r) in enumerate(self.grid):
            if u is None: print(f"  [WARN] cell {i}: missing UP line")
            if d is None: print(f"  [WARN] cell {i}: missing DOWN line")
            if l is None: print(f"  [WARN] cell {i}: missing LEFT line")
            if r is None: print(f"  [WARN] cell {i}: missing RIGHT line")

    def _find_bounding_line(self, pts, direction):
        """
        Search all LINE/2-point-LWPOLYLINE entities for the nearest one that is
        parallel to the given edge, lies on the correct side, and fully spans it.
        """
        ref1, ref2 = {
            "up":    upep(pts),
            "down":  downep(pts),
            "left":  leftep(pts),
            "right": rightep(pts),
        }[direction]

        best, best_dist = None, -1

        for line in self.msp:
            if line.dxftype() not in ("LINE", "LWPOLYLINE"):
                continue
            if line.dxftype() == "LWPOLYLINE" and len(line.get_points()) != 2:
                continue

            pp = get_line_points(line)
            if pp is None or not is_parallel(ref1, ref2, pp[0], pp[1]):
                continue

            v1, v2 = pp

            if direction == "up":
                if v1[1] <= ref1[1] or not (v1[0] <= ref1[0] and v2[0] >= ref2[0]):
                    continue
                d = abs(ref1[1] - v1[1])
            elif direction == "down":
                if v1[1] >= ref1[1] or not (v1[0] <= ref1[0] and v2[0] >= ref2[0]):
                    continue
                d = abs(ref1[1] - v1[1])
            elif direction == "left":
                if v1[0] >= ref1[0] or not (v1[1] <= ref1[1] and v2[1] >= ref2[1]):
                    continue
                d = abs(ref1[0] - v1[0])
            else:  # right
                if v1[0] <= ref1[0] or not (v1[1] <= ref1[1] and v2[1] >= ref2[1]):
                    continue
                d = abs(ref1[0] - v1[0])

            if best_dist == -1 or d < best_dist:
                best_dist, best = d, line

        return best

    # ------------------------------------------------------------------
    # Step 4 - Group connected cells via BFS
    # ------------------------------------------------------------------
    
    # cell 0,1,2,3,4
    # grid (u0,d0,l0,r0),(u1,d1,l1,r1),(u2,d2,l2,r2),(u3,d3,l3,r3),(u4,d4,l4,r4)

    def group_columns(self):
        """
        BFS: two cells are in the same column group when one cell's bounding line
        is the same object as another cell's opposite bounding line
        (e.g. cell A's UP line == cell B's DOWN line).
        """
        visited = [False] * len(self.cells)
        for i in range(len(self.cells)):
            if visited[i]:
                continue
            group, visited = self._bfs(i, visited)
            self.col_grid.append(group)
        print(f"Found {len(self.col_grid)} column groups.")

    def _bfs(self, start, visited):
        queue = deque([start])
        visited[start] = True
        group = [start]

        while queue:
            node = queue.popleft()
            node_up, node_down, node_left, node_right = self.grid[node]

            for i in range(len(self.cells)):
                if visited[i]:
                    continue
                i_up, i_down, i_left, i_right = self.grid[i]
                if (node_up == i_down or node_down == i_up or
                        node_left == i_right or node_right == i_left):
                    queue.append(i)
                    visited[i] = True
                    group.append(i)

        return group, visited

    # ------------------------------------------------------------------
    # Step 5 - Identify the column schedule group (contains "Column ID")
    # ------------------------------------------------------------------

    def find_column_grid(self):
        """
        Compute the bounding box of each group and search for a TEXT/MTEXT
        entity containing "columnid". The first match becomes the target group.
        """
        for i, group in enumerate(self.col_grid):
            bbox = self._group_bbox(group)
            minx, miny, maxx, maxy = bbox
            print(f"Group {i}: ({minx}, {miny}) to ({maxx}, {maxy})")

            hits = find_text_in_rect(self.msp, minx - 100, miny - 100,
                                     maxx + 100, maxy + 100, "columnid")
            if hits:
                self.target_group_idx = i
                self.target_bbox = bbox
                print(f"  Column ID label found ({len(hits)} entity/-ies).")
                break
            print("  No column ID label.")

        print(f"Target group index: {self.target_group_idx}")

    def _group_bbox(self, group):
        """Return (minx, miny, maxx, maxy) covering all bounding lines of a group."""
        minx = miny = maxx = maxy = None
        for cell_idx in group:
            for line in self.grid[cell_idx]:
                pp = get_line_points(line)
                if pp is None:
                    continue
                for x, y in pp:
                    minx = x if minx is None else min(minx, x)
                    miny = y if miny is None else min(miny, y)
                    maxx = x if maxx is None else max(maxx, x)
                    maxy = y if maxy is None else max(maxy, y)
        return minx, miny, maxx, maxy

    # ------------------------------------------------------------------
    # Step 6 - Extract material properties (fpc, fyt) from schedule text
    # ------------------------------------------------------------------

    def extract_material_props(self):
        """
        Find an MTEXT entity containing "strength" within the target group bbox
        and parse fyt (MPa) and f'c (MPa) from it using regex.
        """
        minx, miny, maxx, maxy = self.target_bbox
        strength_entities = find_text_in_rect(
            self.msp, minx - 200, miny - 200, maxx + 200, maxy + 200, "strength"
        )

        raw = None
        for e in strength_entities:
            if e.dxftype() == "MTEXT":
                raw = e.dxf.text
                break

        if raw is None:
            print("[WARN] No strength text found; fpc and fyt will be None.")
            return

        fyt_match = re.findall(r"f.*?yt.*?(\d+)\s*MPa", raw, re.IGNORECASE)
        fpc_match = re.findall(r"f'.*?c.*?(\d+)\s*MPa", raw, re.IGNORECASE)

        self.fyt = int(fyt_match[0]) if fyt_match else None
        self.fpc = int(fpc_match[0]) if fpc_match else None
        print(f"Material properties: fpc={self.fpc} MPa, fyt={self.fyt} MPa")

    # ------------------------------------------------------------------
    # Step 7 - Extract per-cell content from the target column group
    # ------------------------------------------------------------------

    def extract_content(self):
        """
        For each cell in the target group collect:
          - closed inner polylines (column outline)
          - CROSS TIE polylines
          - circles inside the column outline (rebar rods)
          - DIMENSION entities inside the grid cell
          - circles outside the column outline but inside the grid cell (rod annotations)
          - nearest text labels for those outside circles
        """
        for cell_idx in self.col_grid[self.target_group_idx]:
            cell = self.cells[cell_idx]
            self.closed_polys.append(self._get_closed_polys_inside(cell))
            self.ties.append(self._get_ties_inside(cell))
            self.circles.append(self._get_circles_inside(cell))
            self.dims_per_col.append(self._get_dims_inside_cell(cell_idx))
            outside = self._get_circles_outside_column(cell_idx)
            self.circle_outside.append(outside)
            texts, values = self._get_circle_labels(outside)
            self.circle_text.append(texts)
            self.textsin.append(values)

    def _get_closed_polys_inside(self, outer_cell):
        """Find the first closed LWPOLYLINE (more than 4 pts) inscribed within the cell."""
        for e in self.msp:
            if e.dxftype() == "LWPOLYLINE" and len(e.get_points()) > 4 and e.closed:
                if is_inscribed(e, outer_cell):
                    return [e]
        return []

    def _get_ties_inside(self, outer_cell):
        """Collect CROSS TIE polylines whose bbox fits inside the outer cell."""
        outer_pts = get_vertices(outer_cell)
        return [
            e for e in self.msp
            if e.dxftype() == "LWPOLYLINE"
            and (e.dxf.layer == "CROSS TIE" or e.dxf.layer == "ROD") and not e.closed
            and is_bbox_inside(get_vertices(e), outer_pts)
        ]

    def _get_circles_inside(self, outer_cell, tolerance=0.001):
        """Collect circles completely contained within the column outline polygon, removing duplicates by center proximity."""
        poly = to_polygon(outer_cell)
        
        circles = [e for e in self.msp
                if e.dxftype() == "CIRCLE" and poly.contains(circle_to_shape(e))]
        
        seen = []
        unique_circles = []
        
        for circle in circles:
            cx, cy = circle.dxf.center.x, circle.dxf.center.y
            is_duplicate = any(
                abs(cx - sx) < tolerance and abs(cy - sy) < tolerance
                for sx, sy in seen
            )
            if not is_duplicate:
                seen.append((cx, cy))
                unique_circles.append(circle)
        
        return unique_circles

    def _get_dims_inside_cell(self, cell_idx):
        """
        Collect DIMENSION entities whose three definition points all fall within
        the bounding rectangle of the grid cell.
        """
        up, down, left, right = self.grid[cell_idx]
        minx = get_line_points(left)[0][0]
        miny = get_line_points(down)[0][1]
        maxx = get_line_points(right)[0][0]
        maxy = get_line_points(up)[0][1]

        result = []
        for e in self.msp:
            if e.dxftype() != "DIMENSION":
                continue
            pt1, pt2, pt3 = e.dxf.defpoint, e.dxf.defpoint2, e.dxf.defpoint3
            if (minx <= pt1[0] <= maxx and miny <= pt1[1] <= maxy and
                    minx <= pt2[0] <= maxx and miny <= pt2[1] <= maxy and
                    minx <= pt3[0] <= maxx and miny <= pt3[1] <= maxy):
                result.append(e)
        return result

    def _get_circles_outside_column(self, cell_idx):
        """
        Collect circles inside the grid cell rectangle but outside the column
        outline polygon. These annotate rod counts (e.g. "4-16mm").
        """
        cell = self.cells[cell_idx]
        up, down, left, right = self.grid[cell_idx]
        minx = get_line_points(left)[0][0]
        miny = get_line_points(down)[0][1]
        maxx = get_line_points(right)[0][0]
        maxy = get_line_points(up)[0][1]
        poly = to_polygon(cell)

        return [
            e for e in self.msp
            if e.dxftype() == "CIRCLE"
            and is_circle_inside_rectangle(minx, miny, maxx, maxy, e)
            and not poly.contains(circle_to_shape(e))
        ]

    def _get_circle_labels(self, circle_list):
        """Return (text_entities, cleaned_text_values) for each annotation circle."""
        entities, values = [], []
        for circle in circle_list:
            nearest, dist = find_nearest_text(circle, self.msp)
            print(f"  Nearest text: dist={dist:.1f}")
            cleaned = strip_mtext_codes(get_text_value(nearest)) if nearest else ""
            entities.append(nearest)
            values.append(cleaned)
        return entities, values

    # ------------------------------------------------------------------
    # Step 8 - Assign rod circles to polygon edges
    # ------------------------------------------------------------------

    def assign_circles_to_edges(self):
        """
        For each cell, classify every polygon edge as horizontal (h), vertical (v),
        or diagonal (x), then assign each rod circle to its nearest edge(s).
        Corner circles touching two edges are added to both.
        """
        for i, cell_idx in enumerate(self.col_grid[self.target_group_idx]):
            clist = self.circles[i]
            poly  = self.closed_polys[i][0]
            vts   = get_vertices(poly)

            # Classify edges
            fhv = []
            for j in range(len(vts)):
                p1, p2 = vts[j], vts[(j + 1) % len(vts)]
                if p1 == p2:
                    continue
                if p1[0] == p2[0]:
                    fhv.append("v")
                elif p1[1] == p2[1]:
                    fhv.append("h")
                else:
                    fhv.append("x")

            # Assign each circle to its nearest edge (or two edges if at a corner)
            flist = [[] for _ in range(len(vts))]
            for circle in clist:
                best_edge, second_edge = -1, -1
                best_dist = -1

                for j in range(len(vts)):
                    p1, p2 = vts[j], vts[(j + 1) % len(vts)]
                    if p1 == p2:
                        continue

                    if p1[0] == p2[0]:       # vertical edge: measure horizontal dist
                        d = abs(circle.dxf.center[0] - p1[0])
                    elif p1[1] == p2[1]:     # horizontal edge: measure vertical dist
                        d = abs(circle.dxf.center[1] - p1[1])
                    else:
                        continue

                    radius = math.ceil(circle.dxf.radius)
                    if best_dist == -1:
                        best_dist, best_edge = d, j
                    elif abs(best_dist - d) <= radius and j != best_edge:
                        # Close enough to be a corner circle: assign to both edges
                        best_dist = max(best_dist, d)
                        second_edge = j
                    elif d < best_dist:
                        best_dist, best_edge, second_edge = d, j, -1

                if best_edge != -1:
                    flist[best_edge].append(circle)
                if second_edge != -1:
                    flist[second_edge].append(circle)

            self.l_flst.append(flist)
            self.l_fhv.append(fhv)

    # ------------------------------------------------------------------
    # Step 9 - Determine grid position label for every cell
    # ------------------------------------------------------------------

    def build_cell_positions(self):
        """
        For every cell in self.grid (across all groups), determine its
        (column_label, floor_label) by reading MTEXT labels from the two
        header bands that surround it:

        column_label  – found in the horizontal band defined by the cell's
                        left/right bounding lines.  We take the MTEXT with
                        the smallest x (leftmost), which is the row header.

        floor_label   – found in the vertical band defined by the cell's
                        up/down bounding lines.  We take the MTEXT with the
                        smallest x (leftmost too, matching original logic),
                        which is the column header at the top of the schedule.

        Result is stored in self.position, one tuple per cell, in the same
        order as self.cells / self.grid.
        """
        for upx, downx, leftx, rightx in self.grid:
            # --- column label: scan the left-right horizontal band ---
            left_pts  = get_line_points(leftx)
            right_pts = get_line_points(rightx)
            minx = min(left_pts[0][0],  left_pts[1][0],
                       right_pts[0][0], right_pts[1][0])
            miny = min(left_pts[0][1],  left_pts[1][1],
                       right_pts[0][1], right_pts[1][1])
            maxx = max(left_pts[0][0],  left_pts[1][0],
                       right_pts[0][0], right_pts[1][0])
            maxy = max(left_pts[0][1],  left_pts[1][1],
                       right_pts[0][1], right_pts[1][1])

            best_y = None
            column_label = None
            for e in self.msp:
                if e.dxftype() != "MTEXT":
                    continue
                x, y, _ = e.dxf.insert
                if minx < x < maxx and miny < y < maxy:
                    if best_y is None or y > best_y:   # topmost text in band
                        best_y = y
                        column_label = strip_mtext_codes(e.dxf.text)

            # --- floor label: scan the up-down vertical band ---
            up_pts   = get_line_points(upx)
            down_pts = get_line_points(downx)
            minx = min(up_pts[0][0],   up_pts[1][0],
                       down_pts[0][0], down_pts[1][0])
            miny = min(up_pts[0][1],   up_pts[1][1],
                       down_pts[0][1], down_pts[1][1])
            maxx = max(up_pts[0][0],   up_pts[1][0],
                       down_pts[0][0], down_pts[1][0])
            maxy = max(up_pts[0][1],   up_pts[1][1],
                       down_pts[0][1], down_pts[1][1])

            best_x = None
            floor_label = None
            for e in self.msp:
                if e.dxftype() != "MTEXT":
                    continue
                x, y, _ = e.dxf.insert
                if minx < x < maxx and miny < y < maxy:
                    if best_x is None or x < best_x:   # leftmost text in band
                        best_x = x
                        floor_label = strip_mtext_codes(e.dxf.text)

            self.position.append((floor_label, column_label))

        # Print positions for the target column group
        target_cells = self.col_grid[self.target_group_idx]
        for i, cell_idx in enumerate(target_cells):
            print(f"  Cell {cell_idx}: {self.position[cell_idx]}")

    # ------------------------------------------------------------------
    # Step 10 - Compute clear spacing between consecutive rod circles per edge
    # ------------------------------------------------------------------

    def compute_edge_spacings(self):
        """
        For each cell and each edge, sort the assigned circles and compute the
        clear distance (centre-to-centre minus both radii) between consecutive ones.

        For axis-aligned pairs the gap is a simple subtraction.
        For diagonal edges (where one axis differs slightly) pyth() is used to
        project the centre-to-centre distance onto the edge axis before
        subtracting the radii components along that axis.

        Results are stored in self.l_edge_spacings:
          list (per cell) of lists (per edge) of float gap values in drawing units.
        """
        self.l_edge_spacings = []

        for i in range(len(self.l_flst)):
            flist = self.l_flst[i]
            fhv   = self.l_fhv[i]
            cell_spacings = []

            for edge_idx, circles_on_edge in enumerate(flist):
                gaps = []
                sorted_circles = sort_circles_by_x(circles_on_edge)

                for j in range(1, len(sorted_circles)):
                    c_prev = sorted_circles[j - 1]
                    c_curr = sorted_circles[j]
                    cx1, cy1 = c_prev.dxf.center[0], c_prev.dxf.center[1]
                    cx2, cy2 = c_curr.dxf.center[0], c_curr.dxf.center[1]
                    r1, r2   = c_prev.dxf.radius,    c_curr.dxf.radius

                    if abs(cy2 - cy1) < 1e-3:
                        # Same y: purely horizontal spacing
                        gap = abs(cx2 - cx1) - r1 - r2

                    elif abs(cx2 - cx1) < 1e-3:
                        # Same x: purely vertical spacing
                        gap = abs(cy2 - cy1) - r1 - r2

                    else:
                        # Diagonal: use edge orientation to project onto the edge axis
                        edge_orient = fhv[edge_idx] if edge_idx < len(fhv) else "x"
                        hyp = distance((cx1, cy1), (cx2, cy2))

                        if edge_orient == "h":
                            # Horizontal edge: circles displaced mainly in x,
                            # small y offset. Project onto x-axis via pyth.
                            if cy1 > cy2:
                                (cx1, cy1), (cx2, cy2) = (cx2, cy2), (cx1, cy1)
                                r1, r2 = r2, r1
                            dy = abs(cy2 - cy1)
                            gap = pyth(hyp, dy) - pyth(r1, dy) - r2

                        elif edge_orient == "v":
                            # Vertical edge: circles displaced mainly in y,
                            # small x offset. Project onto y-axis via pyth.
                            if cx1 > cx2:
                                (cx1, cy1), (cx2, cy2) = (cx2, cy2), (cx1, cy1)
                                r1, r2 = r2, r1
                            dx = abs(cx2 - cx1)
                            gap = pyth(hyp, dx) - pyth(r1, dx) - r2

                        else:
                            # True diagonal edge: fall back to raw centre distance
                            gap = hyp - r1 - r2

                    gaps.append(gap)

                cell_spacings.append(gaps)

            self.l_edge_spacings.append(cell_spacings)

    # ------------------------------------------------------------------
    # Step 11 - Compute steel area and Ash confinement reinforcement
    # ------------------------------------------------------------------

    def compute_steel(self):
        """
        For each cell in the target column group:
          - Sum the steel cross-section area from rod annotation labels.
          - Compute Ash (min confinement reinforcement area) per ACI 318 18.10.6.4.
        Uses fpc and fyt extracted from the schedule in step 6.
        """
        s, rebar = self._extract_tie_spacing()
        
        final_tuple=[]

        for i, cell_idx in enumerate(self.col_grid[self.target_group_idx]):
            print(f"\n=== Column cell index {cell_idx} ===")
            column = self.position[cell_idx][0]  # column label from position
            floor = self.clean_text(self.position[cell_idx][1])  # floor label from position
            dims = self.dims_per_col[i]
            l_dim = self.round_to_int(dims[0].get_measurement()) if len(dims) > 0 else None
            w_dim = self.round_to_int(dims[1].get_measurement()) if len(dims) > 1 else None

            total_steel = self._sum_steel_area(self.textsin[i])
            dim_product = self._multiply_dimensions(self.dims_per_col[i])
            ratio = total_steel*100 / dim_product if dim_product else 0
            print(f"  Steel area={total_steel} mm2  Dim product={dim_product}  Ratio={ratio:.4f}")
            if ratio<0.02 or ratio>0.04:
                self.errors.append({
                    "cell_idx": cell_idx,
                    "cell_position": self.position[cell_idx],
                    "issue": "Steel ratio out of typical bounds (0.02-0.04)",
                    "details": f"Ratio={ratio:.4f} (steel area={total_steel} mm2, dim product={dim_product})"
                })
            if s is not None and self.fpc is not None and self.fyt is not None:
                ash_h, ash_v, vtie, htie, horizontal, vertical = self._compute_ash(i, cell_idx, s, rebar)
                # print("*************",column, "###", floor)
                if ash_h > ash_v:
                    ash_h, ash_v = ash_v, ash_h
                    horizontal, vertical = vertical, horizontal
                final_tuple.append((column, floor, min(l_dim, w_dim), min(l_dim,w_dim)/max(l_dim,w_dim), ratio, ash_h, ash_v, horizontal, vertical))
                
        sorted_data = sorted(
            final_tuple,
            key=lambda x: (
                x[0],                        # column name
                self.get_max_floor_number(x[1])  # floor max digit
            )
        )
        
        grouped = defaultdict(list)

        for item in sorted_data:
            column = item[0]
            grouped[column].append(item)
        return grouped
    def _sum_steel_area(self, text_list):
        """Parse labels like '4-16mm' and return total steel area in mm2."""
        total = 0
        for label in text_list:
            try:
                count_str, diam_str = label.replace("mm", "").split("-")
                total += int(count_str) * DIAMETER_TO_AREA_MM.get(int(diam_str), 0)
            except Exception as exc:
                print(f"  [WARN] Cannot parse rod label '{label}': {exc}")
        return total

    def _multiply_dimensions(self, dims):
        """Return the product of all dimension measurements (column cross-section area)."""
        product = 1
        for d in dims:
            p1, p2 = d.dxf.defpoint2, d.dxf.defpoint3
            product *= max(abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
        return product

    def _extract_tie_spacing(self):
        """
        Find an MTEXT entity containing 'lapping' inside the target bbox and
        extract from patterns like '10mm@125mm':
          s     - smallest spacing value (most critical)
          rebar - largest tie bar diameter

        Returns (s, rebar) — both None if lapping text is not found.
        """
        minx, miny, maxx, maxy = self.target_bbox

        lapping_entity = None
        for e in self.msp:
            if e.dxftype() != "MTEXT":
                continue
            x, y, _ = e.dxf.insert
            if not (minx < x < maxx and miny < y < maxy):
                continue
            if "lapping" in strip_mtext_codes(e.dxf.text).lower():
                lapping_entity = e
                break

        if lapping_entity is None:
            print("[WARN] No lapping text found.")
            return None, None

        raw = strip_mtext_codes(lapping_entity.dxf.text)
        print(f"  Lapping text: {raw}")
        matches = re.findall(r'(?<!lapping=)\b(\d+)mm@(\d+)mm\b', raw.lower())
        print(f"  Lapping matches: {matches}")

        s = None       # smallest spacing (most critical)
        rebar = None   # largest tie bar diameter
        for diam_str, spacing_str in matches:
            spacing = int(spacing_str)
            diam    = int(diam_str)
            if s is None or spacing < s:
                s = spacing
            if rebar is None or diam > rebar:
                rebar = diam

        print(f"  Tie spacing s={s} mm  Tie bar diameter rebar={rebar} mm")
        return s, rebar

    def _count_ties(self, group_pos):
        """
        Count how many tie polylines act in the vertical direction (vtie) and
        how many act in the horizontal direction (htie) for one column cell.

        Strategy:
          1. Split the edge-assigned circles into horizontal-edge lists (hlst)
             and vertical-edge lists (vlst), sorting each by x or y respectively.
          2. zip() the lists to get rod groups: each group is a tuple of circles
             that share the same position along the perpendicular axis
             (i.e. a column of circles or a row of circles).
          3. For every tie polyline, find the rod group whose vertices are
             closest to the tie, then decide the tie direction by checking
             whether the circles in that group are aligned vertically or
             horizontally (compare x spread vs y spread using radius tolerance).
        """
        flist = self.l_flst[group_pos]
        fhv   = self.l_fhv[group_pos]

        # Separate edges into horizontal and vertical, sort circles on each
        hlst = [sort_circles_by_x(flist[j])
                for j, o in enumerate(fhv) if o == "h"]
        vlst = [sort_circles_by_y(flist[j])
                for j, o in enumerate(fhv) if o == "v"]

        # zip produces groups of circles at the same perpendicular position
        hls = list(zip(*hlst)) if hlst else []
        vls = list(zip(*vlst)) if vlst else []
        print(f"  Found {len(hls)} horizontal rod groups and {len(vls)} vertical rod groups.")
        rod_groups = hls + vls   # each element is a tuple of circles

        vtie = htie = 0

        for tie_poly in self.ties[group_pos]:
            tie_vts = get_vertices(tie_poly)

            # Find the rod group whose circles are nearest to this tie's vertices
            best_group     = None
            best_min_dist  = -1
            best_radius    = -1

            for group in rod_groups:
                # Minimum distance from any circle centre to any tie vertex
                group_min_dist = float("inf")
                group_radius   = -1
                for circle in group:
                    cn = (circle.dxf.center[0], circle.dxf.center[1])
                    for pt in tie_vts:
                        d = distance(cn, pt)
                        if d < group_min_dist:
                            group_min_dist = d
                            group_radius   = circle.dxf.radius

                if best_min_dist == -1 or group_min_dist < best_min_dist:
                    best_min_dist = group_min_dist
                    best_group    = group
                    best_radius   = group_radius

            if best_group is None or len(best_group) < 2:
                continue

            # Decide tie direction from the alignment of the nearest rod group
            dx = abs(best_group[0].dxf.center[0] - best_group[1].dxf.center[0])
            dy = abs(best_group[0].dxf.center[1] - best_group[1].dxf.center[1])

            if dx < best_radius:
                # circles are stacked vertically → tie restrains the vertical direction
                vtie += 1
            elif dy < best_radius:
                # circles are side by side horizontally → tie restrains horizontal
                htie += 1
            else:
                print(f"  [WARN] Cannot classify tie direction: "
                      f"c0={best_group[0].dxf.center}, c1={best_group[1].dxf.center}")

        print(f"  vtie={vtie}  htie={htie}")
        return vtie, htie

    def _compute_ash(self, group_pos, cell_idx, s, rebar):
        """
        Compute Ash (minimum confinement reinforcement area) for both axes
        using ACI 318 18.10.6.4:
          Ash = max(0.3 * (s*bc*fpc/fyt) * (Ag/Ach - 1),
                    0.09 * s*bc*fpc/fyt)

        Also counts vtie and htie (number of ties acting in each direction)
        and prints them alongside the Ash values for verification.
        """
        cell     = self.cells[cell_idx]
        pts      = get_vertices(cell)
        poly_pts = get_vertices(self.closed_polys[group_pos][0])

        # Measure cover distances from each bounding line to the column outline edge
        lf = rg = uu = dd = None
        left_x  = leftep(pts)[0][0]
        right_x = rightep(pts)[0][0]
        up_y    = upep(pts)[0][1]
        down_y  = downep(pts)[0][1]

        for j, pt in enumerate(poly_pts):
            pt2 = poly_pts[(j + 1) % len(poly_pts)]
            if abs(pt[0] - pt2[0]) < 0.001:     # vertical edge
                dl, dr = abs(left_x - pt[0]), abs(right_x - pt[0])
                if lf is None or dl < lf: lf = dl
                if rg is None or dr < rg: rg = dr
            elif abs(pt[1] - pt2[1]) < 0.001:   # horizontal edge
                du, db = abs(up_y - pt[1]), abs(down_y - pt[1])
                if uu is None or du < uu: uu = du
                if dd is None or db < dd: dd = db

        # Read column dimensions from DIMENSION entities
        total_width = total_height = None
        for d in self.dims_per_col[group_pos]:
            p1, p2 = d.dxf.defpoint2, d.dxf.defpoint3
            if abs(p1[1] - p2[1]) < 0.001:
                total_width  = d.get_measurement()
            elif abs(p1[0] - p2[0]) < 0.001:
                total_height = d.get_measurement()

        if None in (total_width, total_height, lf, rg, uu, dd):
            print("  [WARN] Insufficient geometry data for Ash calculation.")
            return

        hc  = total_width  - lf - rg
        vc  = total_height - uu  - dd
        ag  = total_width * total_height
        ach = hc * vc

        print(f"  hc={hc}  vc={vc}  Ag={ag}  Ach={ach} {self.fpc} {self.fyt}")
        print(f"  lf={lf}  rg={rg}  uu={uu}  dd={dd}")

        ash_h = max(
            0.3 * (s * hc * self.fpc / self.fyt) * (ag / ach - 1),
            0.09 * s * hc * self.fpc / self.fyt
        )
        ash_v = max(
            0.3 * (s * vc * self.fpc / self.fyt) * (ag / ach - 1),
            0.09 * s * vc * self.fpc / self.fyt
        )
        
        # print("s",s,"hc",hc,"vc",vc,"fpc",self.fpc,"fyt",self.fyt,"Ag",ag,"Ach",ach,"Ash_h",ash_h,"Ash_v",ash_v)
        print(f"rebar: {rebar}")

        # Count ties by direction and compute actual tie steel area provided.
        # ea_we: total area of ties acting in the vertical (east-west) direction.
        # no_so: total area of ties acting in the horizontal (north-south) direction.
        vtie, htie = self._count_ties(group_pos)

        tie_bar_area = DIAMETER_TO_AREA_MM.get(rebar, 0) if rebar is not None else 0
        horizontal = (vtie+2) * tie_bar_area
        vertical = (htie+2) * tie_bar_area

        print(f"  Ash required (horizontal direction) = {ash_h:.2f} mm2  |  vtie={vtie}  Ash provided (horizontal) = {horizontal} mm2")
        print(f"  Ash required (vertical direction)   = {ash_v:.2f} mm2  |  htie={htie}  Ash provided (vertical) = {vertical} mm2")
        return ash_h, ash_v, vtie, htie, horizontal, vertical
    # ------------------------------------------------------------------
    # Step 12 - Export visualisation DXF
    # ------------------------------------------------------------------

    def export(self, output_index=0):
        """Write a new DXF containing all extracted entities for visual review."""
        target_cells = self.col_grid[self.target_group_idx]
        new_doc = ezdxf.new("R2010")
        new_msp = new_doc.modelspace()
        importer = Importer(self.doc, new_doc)

        # Set drawing extents
        all_x = [pt[0] for idx in target_cells for pt in self.cells[idx].get_points()]
        all_y = [pt[1] for idx in target_cells for pt in self.cells[idx].get_points()]
        new_doc.header['$EXTMIN'] = (min(all_x), min(all_y), 0)
        new_doc.header['$EXTMAX'] = (max(all_x), max(all_y), 0)

        # Cell outlines with labels
        for pos, cell_idx in enumerate(target_cells):
            cell = self.cells[cell_idx]
            new_msp.add_lwpolyline(cell.get_points(), close=cell.closed)
            first = list(cell.get_points())[0]
            new_msp.add_text(f"PL_{cell_idx}",
                             dxfattribs={"height": 50, "insert": (first[0], first[1])})

        # Inner column outline polylines
        for group in self.closed_polys:
            for poly in group:
                new_msp.add_lwpolyline(poly.get_points(), close=poly.closed)

        # Tie polylines
        for group in self.ties:
            for poly in group:
                new_msp.add_lwpolyline(poly.get_points(), close=poly.closed)

        # Interior rod circles
        for group in self.circles:
            for c in group:
                new_msp.add_circle(center=(c.dxf.center.x, c.dxf.center.y),
                                   radius=c.dxf.radius)

        # Annotation circles (outside column outline)
        for group in self.circle_outside:
            for c in group:
                new_msp.add_circle(center=(c.dxf.center.x, c.dxf.center.y),
                                   radius=c.dxf.radius)

        # Text labels for annotation circles
        for group in self.circle_text:
            for e in group:
                if e is not None:
                    new_msp.add_text(
                        strip_mtext_codes(get_text_value(e)),
                        dxfattribs={"height": 50, "insert": e.dxf.insert}
                    )

        # Dimension entities (imported to preserve dimension style)
        dim_flat = [d for group in self.dims_per_col for d in group]
        importer.import_entities(dim_flat, new_msp)
        importer.finalize()

        out_path = f"extracted-{output_index}.dxf"
        new_doc.saveas(out_path)
        print(f"Exported: {out_path}")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        self.load()
        self.collect_cells()
        self.build_grid()
        self.group_columns()
        self.find_column_grid()
        self.extract_material_props()
        self.extract_content()
        self.assign_circles_to_edges()
        self.build_cell_positions()
        self.compute_edge_spacings()
        tuples=self.compute_steel()
        self.export(output_index=0)
        
        return tuples


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# if __name__ == "__main__":
#     parser = ColumnScheduleParser(DXF_FILE)
#     parser.run()

class DecisionEngine:
    """
    Handles mapping from (location, occupancy, SPT)
    → final engineering parameters using final_map
    """

    def __init__(self):
        self.location_category_dict = location_category_dict
        self.occupancy_category_dict = occupancy_dict

    def get_result(self, location: str, occupancy: str, spt_value: float):
        """
        Returns matched configuration based on SPT range.
        """

        location = location.strip()
        occupancy = occupancy.strip()

        if location not in self.location_category_dict:
            raise ValueError(f"Location '{location}' not found in map")

        if occupancy not in self.occupancy_category_dict:
            raise ValueError(f"Occupancy '{occupancy}' not found in occupancy categories")

        zone = self.location_category_dict[location]
        occupancy_category = self.occupancy_category_dict[occupancy]
        
        if spt_value < 15:
            if occupancy_category != "IV":
                if zone == "I":
                    return "C"
                else:
                    return "D"
            else:
                return "D"
        elif 15 <= spt_value <= 50 or spt_value > 50:
            if occupancy_category != "IV":
                if zone == "I":
                    return "B"
                elif zone == "II":
                    return "C"
                else:
                    return "D"
            else:
                if zone == "I":
                    return "C"
                else:
                    return "D"

            
        
        # rules = self.final_map[location][occupancy]

        # for min_spt, max_spt, result in rules:
        #     if min_spt <= spt_value < max_spt:
        #         return result

        return None  # no match found

    def get_zone(self, location, occupancy, spt_value):
        """
        Convenience method: return only zone/factor if exists.
        """
        result = self.get_result(location, occupancy, spt_value)

        if result is None:
            return {
                "zone": "UNKNOWN",
                "factor": 1.0
            }

        return result
