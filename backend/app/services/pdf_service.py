"""Markdown report -> PDF, keeping headings, tables, working links and references.

Markdown is converted to HTML (python-markdown), citations like [S01] become links to their
entry in the reference list, and xhtml2pdf renders the HTML. The font is Bitstream Vera
(bundled with ReportLab), which covers Latin text and common symbols; set PDF_FONT_PATH /
PDF_BOLD_FONT_PATH to a TTF such as DejaVu Sans or Noto Sans for wider coverage.
"""

import html
import io
import logging
import re
from pathlib import Path

import markdown
import reportlab
from xhtml2pdf import pisa
from xhtml2pdf.config.resources import ResourceAccessPolicy

from app.config import get_settings

logger = logging.getLogger(__name__)

FONT_DIR = Path(reportlab.__file__).parent / "fonts"
AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
CITATION = re.compile(r"\[(S\d+)\](?!\()")
# A reference list item as rendered from "- [S01 — Title](url) ...".
REFERENCE_ITEM = re.compile(r"<li><a href=\"([^\"]+)\">(S\d+) — ")


class PDFError(Exception):
    pass


def _fonts() -> tuple[Path, Path]:
    settings = get_settings()
    regular = Path(settings.pdf_font_path) if settings.pdf_font_path else FONT_DIR / "Vera.ttf"
    bold = Path(settings.pdf_bold_font_path) if settings.pdf_bold_font_path else (
        regular if settings.pdf_font_path else FONT_DIR / "VeraBd.ttf"
    )
    return regular, bold


def _stylesheet() -> str:
    regular, bold = _fonts()
    return f"""
    @font-face {{ font-family: ReportFont; src: url("{regular.as_posix()}"); }}
    @font-face {{ font-family: ReportFont; src: url("{bold.as_posix()}"); font-weight: bold; }}
    @page {{ size: a4; margin: 2cm 1.8cm 2cm 1.8cm;
             @frame footer {{ -pdf-frame-content: footer; bottom: 1cm; margin-left: 1.8cm; margin-right: 1.8cm; height: 1cm; }} }}
    body {{ font-family: ReportFont; font-size: 10pt; line-height: 1.45; color: #1b1b18; }}
    h1 {{ font-size: 19pt; margin: 0 0 6pt 0; }}
    h2 {{ font-size: 13.5pt; margin: 16pt 0 6pt 0; padding-bottom: 2pt; border-bottom: 0.5pt solid #cfcfc8; }}
    h3 {{ font-size: 11pt; margin: 10pt 0 4pt 0; }}
    p, li {{ margin: 0 0 4pt 0; }}
    a {{ color: #2f4ac0; text-decoration: none; }}
    a.cite {{ font-size: 8pt; }}
    blockquote {{ background-color: #fff5e3; border-left: 3pt solid #a86200; padding: 6pt 8pt; margin: 6pt 0; }}
    table {{ width: 100%; border-collapse: collapse; margin: 6pt 0 10pt 0; -pdf-keep-in-frame-mode: shrink; }}
    th {{ background-color: #f1f1ee; font-weight: bold; text-align: left; }}
    th, td {{ border: 0.5pt solid #cfcfc8; padding: 4pt 5pt; vertical-align: top; font-size: 9pt; }}
    code {{ font-family: Courier; font-size: 9pt; }}
    #footer {{ font-size: 8pt; color: #6b6b63; text-align: right; }}
    """


def markdown_to_html(markdown_text: str) -> str:
    # Report text comes partly from the model, so raw HTML (<img>, <script>, ...) is shown as text,
    # not rendered. <https://...> autolinks are turned into Markdown links first.
    markdown_text = AUTOLINK.sub(r"[\1](\1)", markdown_text).replace("<", "&lt;")
    body = markdown.markdown(markdown_text, extensions=["tables", "sane_lists"], output_format="html")
    # Give each reference an anchor, then point every [S01] citation at it.
    body = REFERENCE_ITEM.sub(lambda m: f'<li><a name="ref-{m.group(2)}"></a><a href="{m.group(1)}">{m.group(2)} — ', body)
    return CITATION.sub(lambda m: f'<a class="cite" href="#ref-{m.group(1)}">[{m.group(1)}]</a>', body)


def markdown_to_pdf(markdown_text: str, title: str) -> bytes:
    document = (
        "<html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_stylesheet()}</style></head><body>"
        f"{markdown_to_html(markdown_text)}"
        "<div id='footer'>Page <pdf:pagenumber> of <pdf:pagecount></div>"
        "</body></html>"
    )
    regular, bold = _fonts()
    # Never fetch anything over the network (an image URL in model-written text would be an SSRF
    # route), and read local files only from the font directories.
    policy = ResourceAccessPolicy.server(
        base_dir=regular.parent, extra_roots=(bold.parent,), allow_remote=False,
    )
    output = io.BytesIO()
    result = pisa.CreatePDF(document, dest=output, encoding="utf-8", resource_policy=policy)
    if result.err:
        logger.error("PDF rendering failed with %d error(s)", result.err)
        raise PDFError("The PDF could not be generated")
    return output.getvalue()
