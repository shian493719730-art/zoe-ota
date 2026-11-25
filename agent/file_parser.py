"""文件解析工具：支持用户拖拽或上传 PDF, Word, PPT, Excel, CSV, Markdown, TXT
提取文本并自动注入上下文，并落盘到 outputs/ 或 uploads/ 目录可追溯留痕。
"""
from __future__ import annotations
import io
import os
import csv
from pathlib import Path
import pypdf
import pptx
import openpyxl
import docx

def parse_uploaded_file(filename: str, raw_bytes: bytes, save_dir: Path) -> dict:
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    
    filepath = save_dir / filename
    # 避免覆盖同名文件
    if filepath.exists():
        stem, ext = os.path.splitext(filename)
        import uuid
        filepath = save_dir / f"{stem}_{uuid.uuid4().hex[:4]}{ext}"
    
    filepath.write_bytes(raw_bytes)
    ext = filepath.suffix.lower()
    text = ""
    preview = ""

    try:
        if ext == ".pdf":
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            pages = [p.extract_text() or "" for p in reader.pages]
            text = "\n\n".join(pages)
            preview = f"PDF文档共 {len(reader.pages)} 页，已解析正文约 {len(text)} 字"

        elif ext in (".docx", ".doc"):
            doc = docx.Document(io.BytesIO(raw_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n".join(paragraphs)
            preview = f"Word文档共 {len(paragraphs)} 个段落，约 {len(text)} 字"

        elif ext in (".pptx", ".ppt"):
            prs = pptx.Presentation(io.BytesIO(raw_bytes))
            slides_txt = []
            for i, slide in enumerate(prs.slides, 1):
                words = [shape.text.strip() for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
                if words:
                    slides_txt.append(f"【第 {i} 页幻灯片】\n" + "\n".join(words))
            text = "\n\n".join(slides_txt)
            preview = f"PPT演示文稿共 {len(prs.slides)} 页，已提取幻灯片核心文字"

        elif ext in (".xlsx", ".xls"):
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
            sheets_txt = []
            total_rows = 0
            for name in wb.sheetnames[:5]:
                ws = wb[name]
                rows = list(ws.iter_rows(values_only=True))
                total_rows += len(rows)
                rows_str = []
                for r in rows[:60]:
                    cells = [str(c) if c is not None else "" for c in r]
                    if any(cells):
                        rows_str.append(" | ".join(cells))
                if rows_str:
                    sheets_txt.append(f"【表格工作表：{name}】\n" + "\n".join(rows_str))
            text = "\n\n".join(sheets_txt)
            preview = f"Excel工作簿共 {len(wb.sheetnames)} 个Sheet，累计约 {total_rows} 行数据"

        elif ext == ".csv":
            content_str = raw_bytes.decode("utf-8", errors="ignore")
            reader = csv.reader(io.StringIO(content_str))
            rows = list(reader)
            text = "\n".join([" | ".join(r) for r in rows[:100]])
            preview = f"CSV数据表共 {len(rows)} 行"

        elif ext in (".txt", ".md", ".json", ".yaml", ".yml"):
            text = raw_bytes.decode("utf-8", errors="ignore")
            preview = f"文本材料共 {len(text)} 字符"

        else:
            text = f"【非结构化附件文件】：{filename}，大小 {len(raw_bytes)/1024:.1f} KB"
            preview = f"已接收文件 {filename}"

    except Exception as e:
        text = f"【文件解析警告】：{filename} 解析正文时异常: {e}"
        preview = f"文件上传成功，正文提取遇异常: {e}"

    return {
        "filename": filename,
        "saved_path": str(filepath),
        "ext": ext,
        "size_kb": f"{len(raw_bytes) / 1024:.1f} KB",
        "preview": preview,
        "text": text
    }
