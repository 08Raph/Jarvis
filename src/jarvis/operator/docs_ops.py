"""Documents, spreadsheets and PDFs. Every edit writes a NEW file next to the original (the original is never changed) and the result is reopened to check it."""
from __future__ import annotations

import csv
import html as _html
import json
import re
import time
from pathlib import Path

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap
from jarvis.operator.files_ops import _unique

SCRIPT_EXT = {".py", ".ps1", ".bat", ".cmd"}
MAX_READ = 60_000


def _need(mod: str, what: str):
    try:
        return __import__(mod)
    except ImportError:
        raise Unavailable(f"{what} needs the \"{mod}\" component, which isn't installed in this copy of Jarvis.") from None


def read_text_of(path: Path, limit: int = MAX_READ) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".csv", ".json", ".log", ".py", ".html", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".js", ".ps1", ".bat"):
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    if ext == ".docx":
        d = _need("docx", "Reading Word files").Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for r in t.rows:
                parts.append(" | ".join(c.text for c in r.cells))
        return "\n".join(parts)[:limit]
    if ext in (".xlsx", ".xlsm"):
        wb = _need("openpyxl", "Reading Excel files").load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets[:5]:
            out.append(f"[{ws.title}]")
            for row in ws.iter_rows(max_row=500, values_only=True):
                out.append(" | ".join("" if c is None else str(c) for c in row))
        return "\n".join(out)[:limit]
    if ext == ".pdf":
        r = _need("pypdf", "Reading PDFs").PdfReader(str(path))
        txt = "\n".join((pg.extract_text() or "") for pg in r.pages[:80])
        if not txt.strip():
            raise Unavailable("That PDF has no text layer (it looks scanned). Reading it needs a local OCR engine, which isn't installed.")
        return txt[:limit]
    raise OperatorError(f"I can't read {ext or 'that'} files yet. I read text, Word, Excel and PDF.")


@cap("docs.read", "files_read", "Read a document's text (for summaries, proofreading, questions)", LOW, ("path",), False, "25,27")
def read_doc(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    t = read_text_of(p)
    return Outcome("info", f"{p.name}: {len(t)} characters read.", t)


# ------------------------------------------------------------------------------------------------ creating files
def _simple_yaml(v, ind=0) -> str:
    pad = "  " * ind
    if isinstance(v, dict):
        return "\n".join(f"{pad}{k}:" + (" " + _scalar(x) if not isinstance(x, (dict, list)) else "\n" + _simple_yaml(x, ind + 1)) for k, x in v.items())
    if isinstance(v, list):
        return "\n".join(f"{pad}- " + (_scalar(x) if not isinstance(x, (dict, list)) else "\n" + _simple_yaml(x, ind + 1)) for x in v)
    return pad + _scalar(v)


def _scalar(x) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if x is None:
        return "null"
    if isinstance(x, (int, float)):
        return str(x)
    return json.dumps(str(x), ensure_ascii=False)


def _simple_toml(d: dict) -> str:
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            continue
        lines.append(f"{k} = {json.dumps(v, ensure_ascii=False) if not isinstance(v, bool) else str(v).lower()}")
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"\n[{k}]")
            lines += [f"{a} = {json.dumps(b, ensure_ascii=False) if not isinstance(b, bool) else str(b).lower()}" for a, b in v.items()]
    return "\n".join(lines) + "\n"


def _kind_risk(a: dict) -> int:
    ext = Path(str(a.get("path", ""))).suffix.lower()
    return HIGH if ext in SCRIPT_EXT else MEDIUM


@cap("docs.create", "documents", "Create a file (txt, md, csv, json, yaml, toml, html, docx, xlsx, pdf, or a script)", _kind_risk, ("path", "title", "paragraphs", "rows", "text"), False, "25,26,27,50")
def create(op, path: str = "", title: str = "", paragraphs=None, rows=None, text: str = "", data=None) -> Outcome:
    s = path.strip().strip('"')
    p_in = Path(s)
    target = op.check_path(p_in if p_in.is_absolute() else (op.roots()[0] / s if op.roots() else p_in), must_exist=False, write=True)
    ext = target.suffix.lower()
    target = _unique(target)
    paras = [str(x) for x in (paragraphs or [])]
    if not paras and text:
        paras = [t for t in text.split("\n")]
    target.parent.mkdir(parents=True, exist_ok=True)
    if ext in (".txt", ".md", ".log"):
        body = (f"# {title}\n\n" if title and ext == ".md" else (title + "\n\n" if title else "")) + "\n".join(paras)
        target.write_text(body, encoding="utf-8")
        ok = target.read_text(encoding="utf-8") == body
    elif ext == ".csv":
        with open(target, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows or [[p] for p in paras])
        ok = len(list(csv.reader(open(target, encoding="utf-8")))) == len(rows or paras)
    elif ext == ".json":
        target.write_text(json.dumps(data if data is not None else {"title": title, "text": paras}, indent=2, ensure_ascii=False), encoding="utf-8")
        ok = bool(json.loads(target.read_text(encoding="utf-8")) is not None)
    elif ext in (".yaml", ".yml"):
        target.write_text(_simple_yaml(data if data is not None else {"title": title, "text": paras}) + "\n", encoding="utf-8")
        ok = target.stat().st_size > 0
    elif ext == ".toml":
        target.write_text(_simple_toml(data if isinstance(data, dict) else {"title": title}), encoding="utf-8")
        ok = target.stat().st_size > 0
    elif ext in (".html", ".htm"):
        body = "".join(f"<p>{_html.escape(x)}</p>\n" for x in paras)
        target.write_text(f"<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>{_html.escape(title)}</title></head>\n<body>\n<h1>{_html.escape(title)}</h1>\n{body}</body></html>\n", encoding="utf-8")
        ok = "</html>" in target.read_text(encoding="utf-8")
    elif ext == ".docx":
        docx = _need("docx", "Making Word files")
        d = docx.Document()
        if title:
            d.add_heading(title, 0)
        for x in paras:
            if x.startswith("# "):
                d.add_heading(x[2:], 1)
            elif x.startswith("- "):
                d.add_paragraph(x[2:], style="List Bullet")
            else:
                d.add_paragraph(x)
        if rows:
            t = d.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            t.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, c in enumerate(r):
                    t.cell(i, j).text = str(c)
        d.save(str(target))
        ok = len(docx.Document(str(target)).paragraphs) >= len(paras)
    elif ext == ".xlsx":
        xl = _need("openpyxl", "Making Excel files")
        wb = xl.Workbook()
        ws = wb.active
        for r in (rows or [[p] for p in paras]):
            ws.append(list(r))
        wb.save(str(target))
        ok = xl.load_workbook(str(target)).active.max_row == len(rows or paras)
    elif ext == ".pdf":
        _need("reportlab", "Making PDFs")
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        st = getSampleStyleSheet()
        story = ([Paragraph(_html.escape(title), st["Title"]), Spacer(1, 12)] if title else []) + [Paragraph(_html.escape(x) or "&nbsp;", st["BodyText"]) for x in paras]
        SimpleDocTemplate(str(target), pagesize=A4).build(story or [Paragraph(" ", st["BodyText"])])
        ok = len(_need("pypdf", "Checking PDFs").PdfReader(str(target)).pages) >= 1
    elif ext in SCRIPT_EXT:
        target.write_text("\n".join(paras), encoding="utf-8")
        ok = target.exists()
        return Outcome("verified" if ok else "failed", f"Wrote {target.name}. I did NOT run it and never run scripts on my own; read it before you do.", str(target))
    else:
        raise OperatorError(f"I can't make {ext or 'that kind of'} files. Try txt, md, csv, json, yaml, toml, html, docx, xlsx or pdf.")
    return Outcome("verified" if ok and target.exists() else "failed", f"Created {target.name} and opened it again to check it." if ok else f"I wrote {target.name} but it did not read back correctly.", str(target))


@cap("docs.proofread_prepare", "files_read", "Get a document's text ready for the AI to proofread, rewrite, translate or summarize", LOW, ("path",), False, "25")
def prepare(op, path: str = "") -> Outcome:
    return read_doc(op, path)


@cap("docs.compare", "files_read", "Compare two documents' text", LOW, ("a", "b"), False, "25,27")
def compare_docs(op, a: str = "", b: str = "") -> Outcome:
    import difflib
    pa, pb = op.check_path(a), op.check_path(b)
    ta, tb = read_text_of(pa).splitlines(), read_text_of(pb).splitlines()
    diff = [d for d in difflib.unified_diff(ta, tb, lineterm="", n=0) if d[:1] in "+-" and not d.startswith(("+++", "---"))]
    if not diff:
        return Outcome("info", "The text of the two documents is the same.")
    return Outcome("info", f"{sum(1 for d in diff if d[0] == '+')} line(s) added and {sum(1 for d in diff if d[0] == '-')} removed. " + "; ".join(d[:70] for d in diff[:6]), diff)


# ------------------------------------------------------------------------------------------------ PDFs
def _pages(spec: str, n: int) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise OperatorError("Pages look like 1-3,5.")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b > n or a > b:
            raise OperatorError(f"That PDF has {n} pages.")
        out += list(range(a - 1, b))
    return out


def _pdf_out(op, src: Path, suffix: str) -> Path:
    return _unique(src.with_name(f"{src.stem} - {suffix}.pdf"))


@cap("pdf.merge", "documents", "Merge PDFs into a new PDF", MEDIUM, ("files",), False, "27")
def pdf_merge(op, files=None) -> Outcome:
    pypdf = _need("pypdf", "PDF tools")
    srcs = [op.check_path(f) for f in (files or [])]
    if len(srcs) < 2:
        raise OperatorError("Give me at least two PDFs.")
    out = _pdf_out(op, srcs[0], "merged")
    w = pypdf.PdfWriter()
    for s in srcs:
        for pg in pypdf.PdfReader(str(s)).pages:
            w.add_page(pg)
    w.write(str(out))
    n = len(pypdf.PdfReader(str(out)).pages)
    want = sum(len(pypdf.PdfReader(str(s)).pages) for s in srcs)
    return Outcome("verified" if n == want else "failed", f"Merged {len(srcs)} PDFs into {out.name} ({n} pages).", str(out))


@cap("pdf.extract_pages", "documents", "Extract, reorder or rotate pages into a new PDF", MEDIUM, ("path", "pages", "rotate"), False, "27")
def pdf_pages(op, path: str = "", pages: str = "1", rotate: int = 0) -> Outcome:
    pypdf = _need("pypdf", "PDF tools")
    p = op.check_path(path)
    r = pypdf.PdfReader(str(p))
    idx = _pages(pages, len(r.pages))
    if rotate not in (0, 90, 180, 270):
        raise OperatorError("Rotate by 90, 180 or 270.")
    out = _pdf_out(op, p, "pages " + pages.replace(" ", ""))
    w = pypdf.PdfWriter()
    for i in idx:
        pg = r.pages[i]
        if rotate:
            pg.rotate(rotate)
        w.add_page(pg)
    w.write(str(out))
    ok = len(pypdf.PdfReader(str(out)).pages) == len(idx)
    return Outcome("verified" if ok else "failed", f"Wrote {out.name} with {len(idx)} page(s)." , str(out))


@cap("pdf.split", "documents", "Split a PDF into one file per page", MEDIUM, ("path",), False, "27")
def pdf_split(op, path: str = "") -> Outcome:
    pypdf = _need("pypdf", "PDF tools")
    p = op.check_path(path)
    r = pypdf.PdfReader(str(p))
    if len(r.pages) > 200:
        raise OperatorError("That PDF has more than 200 pages; extract the pages you want instead.")
    outs = []
    for i, pg in enumerate(r.pages, 1):
        w = pypdf.PdfWriter()
        w.add_page(pg)
        o = _unique(p.with_name(f"{p.stem} - page {i}.pdf"))
        w.write(str(o))
        outs.append(o)
    return Outcome("verified" if all(o.exists() for o in outs) else "failed", f"Split into {len(outs)} one-page PDFs next to the original.", [str(o) for o in outs])


@cap("pdf.search", "files_read", "Search the text of a PDF", LOW, ("path", "query"), False, "27")
def pdf_search(op, path: str = "", query: str = "") -> Outcome:
    pypdf = _need("pypdf", "PDF tools")
    p = op.check_path(path)
    hits = []
    for i, pg in enumerate(pypdf.PdfReader(str(p)).pages, 1):
        t = pg.extract_text() or ""
        if query.lower() in t.lower():
            hits.append(i)
    return Outcome("info", f"\"{query}\" appears on page(s) {', '.join(map(str, hits[:20]))}." if hits else f"I didn't find \"{query}\" in the text of {p.name} (scanned pages have no text I can search).", hits)


# ------------------------------------------------------------------------------------------------ spreadsheets
def _table(p: Path) -> list[list]:
    if p.suffix.lower() == ".csv":
        with open(p, newline="", encoding="utf-8-sig") as f:
            return [r for r in csv.reader(f)]
    wb = _need("openpyxl", "Excel files").load_workbook(str(p), data_only=True)
    return [list(r) for r in wb.active.iter_rows(values_only=True)]


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _col(rows, col) -> int:
    head = [str(c).lower() for c in rows[0]]
    if str(col).isdigit():
        return int(col) - 1
    if str(col).lower() in head:
        return head.index(str(col).lower())
    raise OperatorError("I can't find that column. The columns are: " + ", ".join(map(str, rows[0])))


def _write_table(src: Path, rows: list[list], suffix: str) -> Path:
    out = _unique(src.with_name(f"{src.stem} - {suffix}{src.suffix}"))
    if src.suffix.lower() == ".csv":
        with open(out, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    else:
        xl = _need("openpyxl", "Excel files")
        wb = xl.Workbook()
        for r in rows:
            wb.active.append(list(r))
        wb.save(str(out))
    return out


@cap("sheet.summary", "files_read", "Total, average and range of a spreadsheet column", LOW, ("path", "column"), False, "26")
def sheet_summary(op, path: str = "", column: str = "1") -> Outcome:
    p = op.check_path(path)
    rows = _table(p)
    if not rows:
        return Outcome("info", "That sheet is empty.")
    c = _col(rows, column)
    vals = [v for v in (_num(r[c]) for r in rows[1:] if len(r) > c) if v is not None]
    if not vals:
        return Outcome("info", f"Column {rows[0][c]} has no numbers.")
    return Outcome("info", f"{rows[0][c]}: {len(vals)} numbers, total {sum(vals):g}, average {sum(vals) / len(vals):g}, smallest {min(vals):g}, largest {max(vals):g}.", vals)


@cap("sheet.sort", "documents", "Sort a spreadsheet into a new file", MEDIUM, ("path", "column", "descending"), False, "26")
def sheet_sort(op, path: str = "", column: str = "1", descending: bool = False) -> Outcome:
    p = op.check_path(path)
    rows = _table(p)
    c = _col(rows, column)
    body = sorted(rows[1:], key=lambda r: (_num(r[c]) is None, _num(r[c]) if _num(r[c]) is not None else str(r[c]).lower()), reverse=descending)
    out = _write_table(p, [rows[0]] + body, "sorted")
    return Outcome("verified" if len(_table(out)) == len(rows) else "failed", f"Saved the sorted copy as {out.name}; the original is unchanged.", str(out))


@cap("sheet.dedupe", "documents", "Remove duplicate rows into a new file", MEDIUM, ("path",), False, "26")
def sheet_dedupe(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    rows = _table(p)
    seen, keep = set(), []
    for r in rows[1:]:
        k = tuple(map(str, r))
        if k not in seen:
            seen.add(k)
            keep.append(r)
    out = _write_table(p, [rows[0]] + keep, "no duplicates")
    return Outcome("verified" if len(_table(out)) == len(keep) + 1 else "failed", f"Removed {len(rows) - 1 - len(keep)} duplicate row(s); saved {out.name}. The original is unchanged.", str(out))


@cap("sheet.chart", "documents", "Add a chart to a copy of an Excel file", MEDIUM, ("path", "label_column", "value_column", "kind"), False, "26")
def sheet_chart(op, path: str = "", label_column: str = "1", value_column: str = "2", kind: str = "bar") -> Outcome:
    xl = _need("openpyxl", "Charts")
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    p = op.check_path(path)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise OperatorError("Charts go into Excel (.xlsx) files.")
    out = _unique(p.with_name(f"{p.stem} - chart{p.suffix}"))
    wb = xl.load_workbook(str(p))
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    lc, vc = _col(rows, label_column) + 1, _col(rows, value_column) + 1
    ch = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(kind)
    if ch is None:
        raise OperatorError("Chart kinds: bar, line, pie.")
    chart = ch()
    chart.add_data(Reference(ws, min_col=vc, min_row=1, max_row=ws.max_row), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=lc, min_row=2, max_row=ws.max_row))
    ws.add_chart(chart, f"{xl.utils.get_column_letter(ws.max_column + 2)}2")
    wb.save(str(out))
    ok = len(xl.load_workbook(str(out)).active._charts) == 1
    return Outcome("verified" if ok else "failed", f"Saved {out.name} with a {kind} chart." if ok else "The chart did not save.", str(out))


@cap("sheet.compare", "files_read", "Compare two spreadsheets", LOW, ("a", "b"), False, "26")
def sheet_compare(op, a: str = "", b: str = "") -> Outcome:
    ra, rb = _table(op.check_path(a)), _table(op.check_path(b))
    ka, kb = {tuple(map(str, r)) for r in ra}, {tuple(map(str, r)) for r in rb}
    return Outcome("info", f"{len(ra)} vs {len(rb)} rows; {len(ka - kb)} row(s) only in the first, {len(kb - ka)} only in the second.", {"only_a": len(ka - kb), "only_b": len(kb - ka)})
