"""
opra_request.py — generates a fillable, signature-ready OPRA bulk-records
request PDF addressed to the Ocean County Clerk's Office.

Output:
  opra_requests/<YYYYMM>_ocean_clerk.pdf       (the request)
  opra_requests/log.csv                        (tracking)

Usage:
  py -3.12 pipeline/opra_request.py [--month YYYY-MM] [--standing]

The default request covers the prior calendar month. With --standing, the
request asks for monthly delivery for the next 12 months (preferred — once
and done).

The request body is operator-friendly: cites N.J.S.A. 47:1A-1, asks for
machine-readable bulk data (not paper or PDF conversions), specifies
electronic delivery to dramatically lower the per-request fee, sets the
7-business-day response window, and requests standing monthly delivery so
the operator does not need to re-submit.
"""
from __future__ import annotations
import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "opra_requests"
LOG = OUT_DIR / "log.csv"

OPERATOR_NAME = "Quentin Flores"
OPERATOR_EMAIL = "infinitygauntletllc@gmail.com"
OPERATOR_ENTITY = "Honestly Nevermind LLC d/b/a Just Jarvis LLC"

CLERK_RECIPIENT = (
    "John P. Kelly, County Clerk\n"
    "Ocean County Clerk's Office\n"
    "118 Washington Street, P.O. Box 2191\n"
    "Toms River, NJ 08754-2191"
)

# Pattern → doc-type list (sourced from RECON.md)
DOC_TYPE_GROUPS = {
    "Foreclosure-firing": ["LISPEN", "NOTLIS", "NTCELIS", "FINJUDGE"],
    "Tax-firing": ["INREM", "MTSC", "TSC", "FEDLIEN"],
    "Lien-firing": [
        "CONLIEN", "MECHLIEN", "MECHNOI", "PHYSLIEN", "BRTYLIEN",
        "WAREXEC", "WRITEXEC", "STOPNOT", "INSTLIEN", "WAGECLM",
    ],
    "Estate-firing": ["TAXWAIVE", "DISCLAIM", "TRUSTAGR"],
    "Transfer-firing (deeds, with sub-type detection)": ["DEED"],
    "Negative / de-escalation (still requested for accuracy)": [
        "DISCHLIS", "DISTSC", "RLFESLEN", "DSCOLIEN", "DSMELIEN",
        "DMECHNOI", "DPHYLIEN", "DSJUDLIEN", "WARSATFN",
    ],
}


def previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def month_window(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def build_pdf(year: int, month: int, standing: bool, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    start, end = month_window(year, month)
    today = date.today()
    response_due = today + timedelta(days=10)  # 7 business days = ~10 calendar days

    styles = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10.5, leading=14, spaceAfter=8)
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=14, leading=18, spaceAfter=10)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11.5, leading=14, spaceBefore=10, spaceAfter=6)
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=9, leading=11, textColor=colors.grey)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=f"OPRA Request — Ocean County Clerk — {year}-{month:02d}",
    )
    story = []

    # Header
    story.append(Paragraph(
        f"<b>{OPERATOR_NAME}</b><br/>"
        f"{OPERATOR_ENTITY}<br/>"
        f"Email: {OPERATOR_EMAIL}<br/>"
        f"Date: {today.isoformat()}",
        body,
    ))
    story.append(Spacer(1, 12))

    story.append(Paragraph(CLERK_RECIPIENT.replace("\n", "<br/>"), body))
    story.append(Spacer(1, 12))

    story.append(Paragraph(
        "<b>RE: OPEN PUBLIC RECORDS ACT REQUEST — BULK ELECTRONIC EXPORT</b><br/>"
        f"Records Period: {start.isoformat()} through {end.isoformat()}",
        h1,
    ))

    # Statutory basis
    story.append(Paragraph("<b>1. Statutory Basis</b>", h2))
    story.append(Paragraph(
        "This is a request under the New Jersey Open Public Records Act, "
        "<i>N.J.S.A.</i> 47:1A-1 et seq., for government records of the Ocean "
        "County Clerk's Office. Custodian: County Clerk John P. Kelly.",
        body,
    ))

    # Records requested
    story.append(Paragraph("<b>2. Records Requested</b>", h2))
    story.append(Paragraph(
        f"All recorded instruments indexed in your office during the period "
        f"<b>{start.isoformat()} through {end.isoformat()}</b> for the document "
        f"types listed in the table below. The Ocean County Clerk's public "
        f"search system (<i>sng.co.ocean.nj.us/publicsearch</i>) confirms these "
        f"document type abbreviations are in active use for indexing.",
        body,
    ))

    # Doc type table
    rows = [["Group", "Doc-type abbreviations"]]
    for group, codes in DOC_TYPE_GROUPS.items():
        rows.append([group, ", ".join(codes)])
    table = Table(rows, colWidths=[2.6 * inch, 4.0 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))

    # Required fields
    story.append(Paragraph("<b>3. Required Fields per Record</b>", h2))
    story.append(Paragraph(
        "For each instrument, please include: instrument number, recording date, "
        "document type code (the abbreviation as indexed), document type description, "
        "all grantor names, all grantee names, consideration amount, book number, "
        "page number, parcel reference (block / lot / qualifier as recorded), "
        "municipality, case number where applicable (e.g. lis pendens, foreclosure), "
        "and any associated lien amount or judgment amount field.",
        body,
    ))
    story.append(Paragraph(
        "If your indexing system has a unique parcel-permanent-identifier (PAMS-PIN), "
        "please include that field as well — this allows us to join records to the "
        "NJ Department of the Treasury's MOD-IV statewide parcel layer for analysis.",
        body,
    ))

    # Format
    story.append(Paragraph("<b>4. Format and Delivery</b>", h2))
    story.append(Paragraph(
        "<b>We are requesting machine-readable bulk data, not paper certified copies "
        "or PDF conversions.</b> Acceptable formats: CSV (UTF-8), Microsoft Excel "
        "(<i>.xlsx</i>), or any flat-file database export (one row per recorded "
        "instrument). One file per document-type group is acceptable, or a single "
        "consolidated file with a doc_type column.",
        body,
    ))
    story.append(Paragraph(
        f"<b>Delivery preference:</b> email to <i>{OPERATOR_EMAIL}</i>. If file size "
        "exceeds ~25 MB, please advise and we will provide an SFTP endpoint or use "
        "a county-provided file-transfer mechanism.",
        body,
    ))
    story.append(Paragraph(
        "Per <i>N.J.A.C.</i> 5:33-2 (Special Service Charge Schedule for Electronic "
        "Records) and <i>N.J.S.A.</i> 47:1A-5(b), we expect the fee to reflect the "
        "<b>actual cost of duplication for electronic delivery</b>, which for native "
        "database exports is typically nominal or zero. We are <b>not</b> requesting "
        "manual conversion of paper records or any reformatting of stored data — "
        "only an export of records already in your indexing database.",
        body,
    ))

    # Standing order
    if standing:
        story.append(Paragraph("<b>5. Standing Monthly Order</b>", h2))
        story.append(Paragraph(
            "Pursuant to the standing-records-request mechanism permitted under NJ "
            "OPRA practice, we request that this same export be delivered "
            "<b>monthly for the next twelve (12) months</b> covering each prior "
            "calendar month, beginning with the period above. This allows your "
            "office to fulfill the request once-per-month rather than processing "
            "twelve separate requests. We will reaffirm the request annually.",
            body,
        ))

    # Response window
    story.append(Paragraph("<b>6. Response Window</b>", h2))
    story.append(Paragraph(
        f"We anticipate a response within <b>seven (7) business days</b> "
        f"(approximately {response_due.isoformat()}) per <i>N.J.S.A.</i> 47:1A-5(i). "
        "If additional time is required, please notify us in writing with an "
        "estimated completion date.",
        body,
    ))

    # Closing
    story.append(Paragraph("<b>7. Contact</b>", h2))
    story.append(Paragraph(
        f"For any clarifying questions, please contact:<br/>"
        f"<b>{OPERATOR_NAME}</b> · {OPERATOR_EMAIL}",
        body,
    ))

    # Signature block
    story.append(Spacer(1, 22))
    story.append(Paragraph("Respectfully submitted,", body))
    story.append(Spacer(1, 36))
    story.append(Paragraph("____________________________________", body))
    story.append(Paragraph(f"{OPERATOR_NAME}, on behalf of {OPERATOR_ENTITY}", body))
    story.append(Paragraph(f"Date: ____________________________", body))

    story.append(Spacer(1, 24))
    story.append(Paragraph(
        f"Generated {today.isoformat()} by Ocean Intel pipeline. "
        f"Tracking: opra_requests/log.csv. Standing order: {'YES' if standing else 'NO'}.",
        small,
    ))

    doc.build(story)
    return out_path


def append_log(year: int, month: int, standing: bool, pdf_path: Path) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    is_new = not LOG.exists()
    today = date.today()
    response_due = today + timedelta(days=10)
    with LOG.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow([
                "generated_date", "covers_year_month", "standing_order",
                "pdf_path", "submitted_date", "response_due", "response_received_date",
                "status", "notes",
            ])
        w.writerow([
            today.isoformat(), f"{year}-{month:02d}", "Y" if standing else "N",
            str(pdf_path.relative_to(ROOT)), "", response_due.isoformat(), "",
            "DRAFT_GENERATED", "Sign and email PDF to John P. Kelly's office.",
        ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="", help="YYYY-MM (default: previous month)")
    ap.add_argument("--standing", action="store_true",
                    help="request standing monthly delivery for 12 months (recommended)")
    args = ap.parse_args()

    if args.month:
        try:
            year, month = map(int, args.month.split("-"))
        except Exception:
            print(f"--month must be YYYY-MM, got {args.month!r}")
            return 2
    else:
        year, month = previous_month(date.today())

    out_path = OUT_DIR / f"{year}{month:02d}_ocean_clerk.pdf"
    build_pdf(year, month, args.standing or True, out_path)  # default standing=True
    append_log(year, month, True, out_path)
    print(f"Wrote {out_path}")
    print(f"Tracking: {LOG}")
    print(f"\nNext step: sign the PDF and email it to John P. Kelly's office.")
    print(f"Recipient: ocean-intel/RECON.md or methodology.html for current address.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
