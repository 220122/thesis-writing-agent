# -*- coding: utf-8 -*-
"""
Word（.docx）导出模块
================================================================================

把论文 Markdown 转成可直接交作业的 Word 文档，中文排版按本科毕业论文习惯设置：
    · 正文  宋体 / 小四(12pt) / 1.5 倍行距 / 首行缩进 2 字符
    · 标题  黑体
    · 标题层级 → Word 的「标题 1/2/3」样式（可在 Word 里直接生成目录、用导航窗格跳转）

两条实现路径：
    1) 安装了 python-docx  → 用它生成，样式最规范（推荐，requirements.txt 已包含）
    2) 没装                → 用内置的最小 OOXML 生成器手写 .docx（只依赖标准库 zipfile），
                             Word / WPS 一样能正常打开，保证这个功能在任何环境都不会失效
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["markdown_to_docx", "has_python_docx"]

# (kind, level, text) ；kind ∈ h / p / li / oli / quote / code / hr
Block = Tuple[str, int, str]


def has_python_docx() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


# =============================================================================
# 一、Markdown → 结构化块
# =============================================================================

_INLINE_RE = re.compile(r"(\*\*.+?\*\*|__.+?__|`[^`]+`|\*[^*\n]+?\*|_[^_\n]+?_)")


def _split_runs(text: str) -> List[Tuple[str, bool, bool, bool]]:
    """把一行文本拆成 (内容, 粗体, 斜体, 等宽) 片段。"""
    runs: List[Tuple[str, bool, bool, bool]] = []
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if (part.startswith("**") and part.endswith("**")) or \
           (part.startswith("__") and part.endswith("__")):
            runs.append((part[2:-2], True, False, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append((part[1:-1], False, False, True))
        elif (part.startswith("*") and part.endswith("*") and len(part) > 2) or \
             (part.startswith("_") and part.endswith("_") and len(part) > 2):
            runs.append((part[1:-1], False, True, False))
        else:
            runs.append((part, False, False, False))
    return runs


def _strip_inline(text: str) -> str:
    return "".join(r[0] for r in _split_runs(text))


def parse_markdown_blocks(md_text: str) -> List[Block]:
    blocks: List[Block] = []
    lines = (md_text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()

        if not s:
            i += 1
            continue

        # 代码块
        if s.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", 0, "\n".join(buf)))
            continue

        # 分隔线
        if re.fullmatch(r"[-*_]{3,}", s):
            blocks.append(("hr", 0, ""))
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            blocks.append(("h", len(m.group(1)), _strip_inline(m.group(2))))
            i += 1
            continue

        # 引用
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", 0, _strip_inline(" ".join(buf))))
            continue

        # 参考文献条目（[1] [2] …）：必须独占一段，绝不能与上下行合并
        if re.match(r"^\[\d+\]\s*\S", s):
            blocks.append(("ref", 0, s))
            i += 1
            continue

        # 有序 / 无序列表
        m = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m:
            blocks.append(("oli", 0, m.group(1)))
            i += 1
            continue
        m = re.match(r"^[-*+]\s+(.*)$", s)
        if m:
            blocks.append(("li", 0, m.group(1)))
            i += 1
            continue

        # 普通段落：连续非空行合并为一段
        buf = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("#") or nxt.startswith("```")
                    or nxt.startswith(">") or re.match(r"^[-*+]\s+", nxt)
                    or re.match(r"^\d+[.)]\s+", nxt) or re.fullmatch(r"[-*_]{3,}", nxt)
                    or re.match(r"^\[\d+\]\s*\S", nxt)):
                break
            buf.append(nxt)
            i += 1
        blocks.append(("p", 0, " ".join(buf)))
    return blocks


# =============================================================================
# 二、方案一：python-docx（样式最规范）
# =============================================================================

def _docx_via_python_docx(md_text: str, title: str, meta: Dict[str, Any]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    doc = Document()

    # 全局字体：中文用宋体，西文用 Times New Roman
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(0)

    for i, (name, size) in enumerate((("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 13)), start=1):
        try:
            st = doc.styles[name]
            st.font.name = "Times New Roman"
            st.font.size = Pt(size)
            st.font.bold = True
            st.font.color.rgb = RGBColor(0, 0, 0)
            st.element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
        except (KeyError, AttributeError):
            pass

    if title:
        h = doc.add_heading(title, level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if meta.get("major") or meta.get("student"):
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        bits = []
        if meta.get("major"):
            bits.append("专业方向：%s" % meta["major"])
        if meta.get("student"):
            bits.append("学生：%s" % meta["student"])
        if meta.get("advisor"):
            bits.append("指导教师：%s" % meta["advisor"])
        sub.add_run("　".join(bits)).font.size = Pt(10.5)

    for kind, level, text in parse_markdown_blocks(md_text):
        if kind == "h":
            if level == 1 and title and text.strip() == title.strip():
                continue          # 标题已作为文档标题输出，避免重复
            # 论文里 Markdown 的 ## 是「章」，在 Word 里应映射为「标题 1」，便于生成目录
            doc.add_heading(text, level=min(max(level - 1, 1), 4))
        elif kind == "hr":
            doc.add_paragraph("─" * 30)
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(24)
            run = p.add_run(text)
            run.italic = True
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        elif kind in ("li", "oli"):
            p = doc.add_paragraph(style="List Bullet" if kind == "li" else "List Number")
            for chunk, bold, italic, mono in _split_runs(text):
                r = p.add_run(chunk)
                r.bold, r.italic = bold, italic
                if mono:
                    r.font.name = "Consolas"
        elif kind == "ref":
            # 参考文献条目：悬挂缩进（续行与首行文字对齐），论文规范排版
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(24)
            p.paragraph_format.first_line_indent = Pt(-24)
            for chunk, bold, italic, mono in _split_runs(text):
                r = p.add_run(chunk)
                r.bold, r.italic = bold, italic
                if mono:
                    r.font.name = "Consolas"
        elif kind == "code":
            p = doc.add_paragraph()
            r = p.add_run(text)
            r.font.name = "Consolas"
            r.font.size = Pt(10)
        else:
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Pt(24)   # 首行缩进 2 字符
            for chunk, bold, italic, mono in _split_runs(text):
                r = p.add_run(chunk)
                r.bold, r.italic = bold, italic
                if mono:
                    r.font.name = "Consolas"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# =============================================================================
# 三、方案二：内置最小 OOXML 生成器（不依赖任何第三方库）
# =============================================================================

def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _run_fragment(text: str, bold: bool, italic: bool, size_half_pt: int, font: str) -> str:
    props = ['<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="%s"/>' % font]
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    props.append('<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (size_half_pt, size_half_pt))
    return ('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
            % ("".join(props), _xml_escape(text)))


def _para_xml(text: str, size_half_pt: int = 24, font: str = "宋体", indent: bool = False,
              hang: bool = False, align: str = "", bullet: str = "") -> str:
    """生成一个段落：自动处理 **粗体** / *斜体* / `等宽` 内联标记。"""
    ppr = ['<w:spacing w:line="360" w:lineRule="auto"/>']
    if indent:
        ppr.append('<w:ind w:firstLineChars="200" w:firstLine="480"/>')
    if hang:
        ppr.append('<w:ind w:leftChars="200" w:left="480" w:hangingChars="200" w:hanging="480"/>')
    if align:
        ppr.append('<w:jc w:val="%s"/>' % align)
    parts = []
    if bullet:
        parts.append(_run_fragment(bullet, False, False, size_half_pt, font))
    for chunk, bold, italic, mono in _split_runs(text):
        parts.append(_run_fragment(chunk, bold, italic, size_half_pt,
                                   "Consolas" if mono else font))
    return "<w:p><w:pPr>%s</w:pPr>%s</w:p>" % ("".join(ppr), "".join(parts))


def _docx_minimal(md_text: str, title: str, meta: Optional[Dict[str, Any]] = None) -> bytes:
    meta = meta or {}
    body: List[str] = []
    if title:
        body.append(_para_xml(title, size_half_pt=36, font="黑体", align="center"))
    sub = []
    if meta.get("major"):
        sub.append("专业方向：%s" % meta["major"])
    if meta.get("student"):
        sub.append("学生：%s" % meta["student"])
    if meta.get("advisor"):
        sub.append("指导教师：%s" % meta["advisor"])
    if sub:
        body.append(_para_xml("　".join(sub), size_half_pt=21, align="center"))

    for kind, level, text in parse_markdown_blocks(md_text):
        if kind == "h":
            if level == 1 and title and text.strip() == title.strip():
                continue
            size = {1: 32, 2: 28, 3: 26}.get(level, 24)
            body.append(_para_xml(text, size_half_pt=size, font="黑体"))
        elif kind == "hr":
            body.append(_para_xml("─" * 30, size_half_pt=21, align="center"))
        elif kind == "quote":
            body.append(_para_xml(text, size_half_pt=21))
        elif kind == "li":
            body.append(_para_xml(text, size_half_pt=24, bullet="• "))
        elif kind == "oli":
            body.append(_para_xml(text, size_half_pt=24))
        elif kind == "ref":
            body.append(_para_xml(text, size_half_pt=24, hang=True))
        elif kind == "code":
            for line in text.split("\n"):
                body.append(_para_xml(line, size_half_pt=20, font="Consolas"))
        else:
            body.append(_para_xml(text, size_half_pt=24, indent=True))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>%s"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1418" w:bottom="1440" w:left="1418" w:header="851" w:footer="992" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>" % "".join(body)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


# =============================================================================
# 四、对外入口
# =============================================================================

def markdown_to_docx(md_text: str, title: str = "", meta: Optional[Dict[str, Any]] = None) -> bytes:
    """
    把论文 Markdown 转成 .docx 字节流。
    优先用 python-docx（样式规范、带标题层级）；未安装时退回内置生成器，保证一定能导出。
    """
    meta = meta or {}
    if has_python_docx():
        try:
            return _docx_via_python_docx(md_text, title, meta)
        except Exception:
            pass   # 任何异常都退回内置生成器，绝不让学生导不出文件
    return _docx_minimal(md_text, title, meta)
