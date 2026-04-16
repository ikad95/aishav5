"""PDF generation + verbatim text rendering.

Two surfaces:

* ``generate_pdf`` — build from a structured outline (title + sections with
  paragraphs/bullets). For when the model composes new content.
* ``render_text_to_pdf`` — drop raw text onto a PDF verbatim, preserving
  line breaks. For when the user asks "convert this .txt to PDF" and no
  re-composition is wanted. No model turn required, <200ms typical.
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def generate_pdf(
    title: str,
    sections: list[dict],
    *,
    subtitle: str = "",
    out_path: Optional[Path] = None,
) -> Path:
    """Build a .pdf from a structured outline.

    ``sections`` is a list of ``{"heading": str, "paragraphs": [str, ...],
    "bullets": [str, ...]}`` dicts — either paragraphs or bullets (or both)
    per section. Returns the path to the generated file.
    """
    # Lazy imports — reportlab is heavy; don't pay at module load.
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
    )

    if out_path is None:
        name = f"aisha_{uuid.uuid4().hex[:8]}.pdf"
        out_path = Path(tempfile.gettempdir()) / name

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h1_style = styles["Heading1"]
    body_style = styles["BodyText"]
    subtitle_style = ParagraphStyle(
        "Subtitle", parent=styles["Italic"], alignment=1, spaceAfter=18,
    )
    bullet_style = ParagraphStyle("Bullet", parent=body_style, leftIndent=0)

    story: list = [Paragraph(title, title_style)]
    if subtitle:
        story.append(Paragraph(subtitle, subtitle_style))
    story.append(Spacer(1, 0.15 * inch))

    for item in sections:
        heading = (item.get("heading") or "").strip()
        if heading:
            story.append(Paragraph(heading, h1_style))
        for para in item.get("paragraphs") or []:
            text = str(para).strip()
            if text:
                story.append(Paragraph(text, body_style))
                story.append(Spacer(1, 0.05 * inch))
        bullets = [str(b).strip() for b in (item.get("bullets") or []) if str(b).strip()]
        if bullets:
            story.append(ListFlowable(
                [ListItem(Paragraph(b, bullet_style)) for b in bullets],
                bulletType="bullet",
                leftIndent=18,
            ))
            story.append(Spacer(1, 0.1 * inch))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=title,
    )
    doc.build(story)
    log.info("pdf: generated %s (%d sections)", out_path, len(sections))
    return out_path


def render_text_to_pdf(
    text: str,
    *,
    title: str = "",
    out_path: Optional[Path] = None,
) -> Path:
    """Render raw text onto a PDF, preserving line breaks, page-wrapping.

    Pure rendering — no model composition. Used when the user wants literal
    conversion (``convert this .txt to PDF``) rather than a rewritten
    structured document.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Preformatted, Spacer

    if out_path is None:
        name = f"aisha_{uuid.uuid4().hex[:8]}.pdf"
        out_path = Path(tempfile.gettempdir()) / name

    styles = getSampleStyleSheet()
    # Monospace, small, tight leading so faithfully-rendered text stays dense
    # and long files don't balloon into 50 pages.
    pre_style = ParagraphStyle(
        "PreRaw",
        parent=styles["Code"],
        fontSize=9,
        leading=11,
        leftIndent=0,
        rightIndent=0,
        spaceAfter=6,
    )

    story: list = []
    if title:
        story.append(Preformatted(title, styles["Heading1"]))
        story.append(Spacer(1, 0.15 * inch))

    # Split on blank lines so each block is an independent flowable that
    # Platypus can page-break between. Without this, one enormous Preformatted
    # can overflow its frame on very long files.
    blocks = [b for b in text.split("\n\n") if b.strip() or True]
    for block in blocks:
        story.append(Preformatted(block.rstrip("\n"), pre_style))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=title or "converted",
    )
    doc.build(story)
    log.info("pdf: rendered %d chars verbatim to %s", len(text), out_path)
    return out_path
