"""Parse the CT-200 manual PDF into a flat, ordered list of nodes with parent links."""
import hashlib, re, unicodedata
import pdfplumber

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)          # ﬁ -> fi, ﬂ -> fl
    text = text.replace("\u2011", "-").replace("\u2010", "-")
    return " ".join(text.split())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def _is_bold(line) -> bool:
    return all("Bold" in c["fontname"] for c in line["chars"] if c["text"].strip())


def parse_pdf(path: str) -> dict:
    nodes, warnings = [], []
    title_lines = []
    current = None
    by_number = {}

    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            found = [t for t in page.find_tables() if len(t.extract()) >= 3]
            def inside(a, b):   # bbox a fully inside bbox b
                return a != b and a[0] >= b[0]-1 and a[1] >= b[1]-1 and a[2] <= b[2]+1 and a[3] <= b[3]+1
            tables = [t for t in found if not any(inside(t.bbox, o.bbox) for o in found)]  # drop fragments nested in a bigger table
            table_boxes = [t.bbox for t in tables]
            page_start_node, heads_on_page = current, []

            def in_table(l):
                return any(b[0] - 2 <= l["x0"] and l["x1"] <= b[2] + 2 and
                           b[1] - 2 <= l["top"] and l["bottom"] <= b[3] + 2 for b in table_boxes)

            for line in page.extract_text_lines(return_chars=True):
                if in_table(line):
                    continue                               # table text handled separately
                text = line["text"].strip()
                size = round(line["chars"][0]["size"], 1)
                m = HEADING_RE.match(text)
                if m and _is_bold(line):                   # number pattern AND bold (size is NOT reliable)
                    number = m.group(1)
                    level = number.count(".") + 1
                    current = {"number": number, "heading": normalize(m.group(2)), "level": level,
                               "page": pno, "order_index": len(nodes), "body_lines": [],
                               "tables": [], "parent_number": None, "warnings": []}
                    nodes.append(current)
                    heads_on_page.append((line["top"], current))
                    by_number.setdefault(number, current)
                elif current is None:
                    if _is_bold(line) and size >= 20:
                        title_lines.append(text)
                else:
                    current["body_lines"].append(text)
            for t in tables:                               # attach table to the heading ABOVE it, not the last one on the page
                owner = page_start_node
                for top, node in heads_on_page:
                    if top <= t.bbox[1]:
                        owner = node
                if owner is not None:
                    rows = t.extract()
                    last = owner.get("_last_table_page")
                    if owner["tables"] and last is not None and last != pno and rows[0] != owner["tables"][-1][0]:
                        owner["tables"][-1].extend(rows)   # table continued across a page break: merge rows
                        owner["warnings"].append(f"table continued across page break (page {last} -> {pno}); merged")
                    else:
                        owner["tables"].append(rows)
                    owner["_last_table_page"] = pno

    # parent = longest existing numeric prefix (not "previous heading")
    for n in nodes:
        parts = n["number"].split(".")
        for k in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:k])
            if prefix in by_number:
                n["parent_number"] = prefix
                if k != len(parts) - 1:
                    n["warnings"].append(f"skipped level: no section {'.'.join(parts[:-1])}; attached to {prefix}")
                break
    # out-of-order detection among siblings
    last = {}
    for n in nodes:
        key = n["parent_number"]
        num = int(n["number"].split(".")[-1])
        if key in last and num < last[key]:
            n["warnings"].append(f"out of order: {n['number']} appears after a higher sibling")
        last[key] = max(num, last.get(key, 0))

    for n in nodes:
        n.pop("_last_table_page", None)
        n["body"] = normalize(" ".join(n.pop("body_lines")))
        n["content_hash"] = content_hash(n["body"] + repr(n["tables"]))
        for w in n["warnings"]:
            warnings.append({"node": n["number"], "issue": w})
    return {"title": normalize(" ".join(title_lines)), "nodes": nodes, "warnings": warnings}
