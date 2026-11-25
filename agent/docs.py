"""读取活动文档：pdf / pptx / docx / xlsx / csv / md / txt → 纯文本（带页码标记）。"""
from __future__ import annotations
import csv
from pathlib import Path

SUPPORTED = {".pdf", ".pptx", ".docx", ".xlsx", ".xlsm", ".csv", ".md", ".txt", ".json"}


def read_any(path: str | Path, max_chars: int = 80000) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            txt = _pdf(p)
        elif ext == ".pptx":
            txt = _pptx(p)
        elif ext == ".docx":
            txt = _docx(p)
        elif ext in (".xlsx", ".xlsm"):
            txt = _xlsx(p)
        elif ext == ".csv":
            txt = _csv(p)
        else:
            txt = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:  # 单个文件读不出来不应拖垮整次运行
        return f"[读取失败 {p.name}: {type(e).__name__}: {e}]"
    return txt[:max_chars]


def _pdf(p: Path) -> str:
    from pypdf import PdfReader
    out = []
    for i, page in enumerate(PdfReader(str(p)).pages, 1):
        t = (page.extract_text() or "").strip()
        out.append(f"--- 第{i}页 ---\n{t}")
    body = "\n".join(out)
    if len(body.replace("-", "").replace("第", "").replace("页", "").strip()) < 50:
        body += "\n[提示：这份 PDF 几乎没有文字层，可能是扫描件，需要先做 OCR]"
    return body


def _pptx(p: Path) -> str:
    from pptx import Presentation
    out = []
    for i, s in enumerate(Presentation(str(p)).slides, 1):
        parts = []
        for sh in s.shapes:
            if sh.has_text_frame:
                parts.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
        out.append(f"--- 第{i}页 ---\n" + "\n".join(x for x in parts if x.strip()))
    return "\n".join(out)


def _docx(p: Path) -> str:
    from docx import Document
    d = Document(str(p))
    out = [para.text for para in d.paragraphs if para.text.strip()]
    for t in d.tables:
        for row in t.rows:
            out.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(out)


def _xlsx(p: Path, max_rows: int = 300) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(p), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"## 工作表: {ws.title}")
        for n, row in enumerate(ws.iter_rows(values_only=True)):
            if n >= max_rows:
                out.append("…(已截断)")
                break
            if any(v is not None for v in row):
                out.append(" | ".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def _csv(p: Path) -> str:
    with open(p, encoding="utf-8-sig", errors="ignore", newline="") as f:
        return "\n".join(" | ".join(r) for r in csv.reader(f))


def read_folder(folder: str | Path, per_file: int = 40000, total: int = 120000):
    """返回 [(文件名, 文本)]，用于抽取指标/生成报告。"""
    folder = Path(folder)
    items, used = [], 0
    if not folder.exists():
        return items
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("~$"):
            t = read_any(p, per_file)
            if used + len(t) > total:
                t = t[: max(0, total - used)]
            if t:
                items.append((p.name, t))
                used += len(t)
            if used >= total:
                break
    return items
