"""Markdown and PDF reports: headings, tables, working links and references."""

import io
import re

from pypdf import PdfReader

from app.agent.graph import run_research
from app.schemas import ComparisonRow, ComparisonTable, DraftFinding, ReportDraft, Statement
from app.services.pdf_service import markdown_to_html, markdown_to_pdf


def comparison_report(_prompt):
    return ReportDraft(
        title="Free LLM APIs — compared",
        summary="Gemini allows 15 RPM [S01]; Groq allows 30 RPM [S02].",
        findings=[DraftFinding(heading="Rate limits", statements=[
            Statement(text="Gemini Flash allows 15 requests per minute.", kind="fact", source_ids=["S01"]),
            Statement(text="Groq provides 30 requests per minute.", kind="fact", source_ids=["S02"]),
        ])],
        comparison=ComparisonTable(title="Comparison", columns=["Option", "Capabilities", "Limitations"], rows=[
            ComparisonRow(cells=["Gemini", "Tool calling", "15 RPM"], source_ids=["S01"]),
            ComparisonRow(cells=["Groq", "Fast inference", "30 RPM"], source_ids=["S02"]),
        ]),
        conclusions=[], limitations=["Limits change often."],
    )


async def make_report(services):
    services.llm.handlers["ReportDraft"] = comparison_report
    state = await run_research("Compare free LLM APIs", depth="quick")
    return state["report"]


async def test_markdown_has_the_expected_structure(services):
    markdown = (await make_report(services))["markdown"]
    headings = re.findall(r"^#{1,3} (.+)$", markdown, flags=re.M)
    assert headings[0] == "Free LLM APIs — compared"
    for section in ("Executive summary", "Methodology and scope", "Detailed findings", "Comparison",
                    "Limitations and unresolved questions", "References"):
        assert section in headings
    assert "| Option | Capabilities | Limitations | Sources |" in markdown
    references = re.findall(r"^- \[(S\d+) — (.+?)\]\((https?://\S+)\)", markdown, flags=re.M)
    assert [r[0] for r in references] == ["S01", "S02"]
    assert "Published:" in markdown and "Retrieved:" in markdown


async def test_pdf_keeps_headings_tables_links_and_references(services):
    report = await make_report(services)
    pdf = markdown_to_pdf(report["markdown"], report["title"])
    assert pdf.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join(page.extract_text() for page in reader.pages)
    for expected in ("Free LLM APIs", "Executive summary", "Comparison", "Fast inference", "References", "Page 1 of"):
        assert expected in text, expected

    annotations = [a.get_object() for page in reader.pages for a in (page.get("/Annots") or [])]
    external = {a["/A"]["/URI"] for a in annotations if "/A" in a and "/URI" in a["/A"]}
    internal = [a for a in annotations if "/Dest" in a or ("/A" in a and a["/A"].get("/S") == "/GoTo")]
    assert {r["url"] for r in report["sources"]} <= external  # references are clickable
    assert len(internal) >= 4  # [S01]/[S02] citations jump to their references

    fonts = {str(f) for page in reader.pages for f in page["/Resources"]["/Font"].values()}
    font_names = {str(page["/Resources"]["/Font"][k].get_object()["/BaseFont"]) for page in reader.pages for k in page["/Resources"]["/Font"]}
    assert any("Vera" in name for name in font_names), font_names
    assert fonts


def test_pdf_never_fetches_resources_from_report_text(caplog):
    # An image or stylesheet in model-written text must not make the server fetch a URL.
    markdown = "# T\n\n![x](http://169.254.169.254/latest/meta-data)\n\n<img src='http://10.0.0.1/x'>\n\nText [S01]"
    html = markdown_to_html(markdown)
    assert "<img src='http" not in html and "&lt;img" in html
    pdf = markdown_to_pdf(markdown, "T")
    assert pdf.startswith(b"%PDF")


def test_raw_html_and_autolinks():
    html = markdown_to_html("Visit <https://example.com/docs> <script>alert(1)</script>")
    assert '<a href="https://example.com/docs">' in html
    assert "<script>" not in html
