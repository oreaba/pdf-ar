"""Document output formats: plain text, Markdown with tables, and structured JSON.

Markdown keeps the page layout the rebuild knows: ruled tables become Markdown
tables (columns ordered right to left for Arabic tables, as they read), table
cells that stand alone and drawn boxes become paragraphs, and every page starts
with an HTML comment ``<!-- page N -->`` so text can be cited to its page.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from pdf_ar.layout import Block

FORMATS = ("txt", "md", "json")
EDGE_TOLERANCE = 2.0


@dataclass
class PageOutput:
    number: int
    status: str
    reasons: list[str]
    text: str
    blocks: list[Block]


def blocks_from_text(text: str) -> list[Block]:
    """Blocks for engines without layout (OCR): one paragraph block per paragraph."""
    return [Block("paragraph", p, p.split("\n"), (0.0, 0.0, 0.0, 0.0),
                  any("\u0600" <= c <= "\u06ff" for c in p))
            for p in text.split("\n\n") if p.strip()]


# Tables ---------------------------------------------------------------------


def _touch(a: Block, b: Block) -> bool:
    """Cells of one table share an edge: side by side or one above the other."""
    ax0, ay0, ax1, ay1 = a.bbox
    bx0, by0, bx1, by1 = b.bbox
    tol = EDGE_TOLERANCE
    vertical_overlap = min(ay1, by1) - max(ay0, by0) > tol
    horizontal_overlap = min(ax1, bx1) - max(ax0, bx0) > tol
    side = abs(ax1 - bx0) < tol or abs(bx1 - ax0) < tol
    stacked = abs(ay1 - by0) < tol or abs(by1 - ay0) < tol
    return (side and vertical_overlap) or (stacked and horizontal_overlap)


def _tables(blocks: list[Block]) -> list[list[int]]:
    """Indices of cell blocks grouped into tables (cells connected by shared edges)."""
    cells = [i for i, b in enumerate(blocks) if b.kind == "cell"]
    groups: list[list[int]] = []
    seen: set[int] = set()
    for start in cells:
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            i = stack.pop()
            group.append(i)
            for j in cells:
                if j not in seen and _touch(blocks[i], blocks[j]):
                    seen.add(j)
                    stack.append(j)
        groups.append(sorted(group))
    return groups


def _boundaries(values: list[float]) -> list[float]:
    out: list[float] = []
    for value in sorted(values):
        if not out or value - out[-1] > EDGE_TOLERANCE:
            out.append(value)
    return out


def _index(boundaries: list[float], value: float) -> int:
    return min(range(len(boundaries)), key=lambda i: abs(boundaries[i] - value))


def _cell_text(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def markdown_table(cells: list[Block]) -> str:
    """A Markdown table from ruled cells; a merged cell fills its first row and column."""
    columns = _boundaries([c.bbox[0] for c in cells])
    rows = _boundaries([c.bbox[1] for c in cells])
    rtl = sum(c.rtl for c in cells) * 2 >= len(cells)
    grid = [["" for _ in columns] for _ in rows]
    for cell in cells:
        row, column = _index(rows, cell.bbox[1]), _index(columns, cell.bbox[0])
        joined = " ".join(filter(None, [grid[row][column], _cell_text(cell.text)]))
        grid[row][column] = joined
    if rtl:
        grid = [list(reversed(r)) for r in grid]
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * len(columns)]
    lines += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(lines)


# Documents ------------------------------------------------------------------


def to_markdown(pages: list[PageOutput]) -> str:
    out: list[str] = []
    for page in pages:
        out.append(f"<!-- page {page.number} -->")
        tables = {group[0]: group for group in _tables(page.blocks) if len(group) > 1}
        in_table = {i for group in tables.values() for i in group}
        for index, block in enumerate(page.blocks):
            if index in tables:
                out.append(markdown_table([page.blocks[i] for i in tables[index]]))
            elif index not in in_table:
                out.append(block.text)
    return "\n\n".join(out) + "\n"


def to_json(document: dict, pages: list[PageOutput]) -> dict:
    return {**document, "pages": [{
        "page": page.number, "status": page.status, "reasons": page.reasons,
        "text": page.text,
        "blocks": [{**asdict(block), "bbox": list(block.bbox)} for block in page.blocks],
    } for page in pages]}
