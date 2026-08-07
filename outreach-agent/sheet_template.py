"""Builds the demo workbook offered on the Settings page.

A new sheet fails the write gate (sheets.require_full_header) until row 1
carries all nine labels in the right order. Handing people a correct file to
start from removes the transcription step that gate keeps catching.

The workbook is assembled with the standard library. An .xlsx is a zip of XML
parts, and this project ships no dependency manifest, so importing openpyxl
here would become an undeclared install step for whoever deploys next.
"""
import csv
import zipfile
from io import BytesIO, StringIO

from sheets import EXPECTED_HEADER

FILENAME = "agent-hub-lead-sheet-template.xlsx"
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

CSV_FILENAME = "agent-hub-lead-sheet-template.csv"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

# Sendkeep reads the FIRST tab -- config.SHEET_RANGE is "A:I", unqualified,
# which Sheets resolves against the leftmost tab. So the grid stays sheet 1 and
# the notes tab follows it, where it can never be parsed as leads.
DATA_SHEET = "Leads"
NOTES_SHEET = "How to use"

# None of these sample rows can be emailed, by construction. campaign_readiness()
# queues any row with a blank Status and a filled Email, so a template built the
# obvious way -- realistic leads sitting ready to go -- would email its own
# examples the moment someone connected it as their real sheet. Every row here
# carries a Status, and every address is @example.com (RFC 2606 reserves that
# domain precisely so it can never reach a real mailbox). The Tom Baker row
# shows the ready-to-send shape and names the single edit that arms it.
DEMO_ROWS = [
    [
        "Jane Doe", "jane.doe@example.com", "Round Treasury", "Needs verification",
        "", "", "",
        "Co-founder listed on the company site; fits the seed-stage fintech target",
        "unverified",
    ],
    [
        "Alex Kim", "alex.kim@example.com", "Zango", "Sent",
        "thread_a1b2c3", "2026-07-15T09:30:00Z",
        "Hi Alex, saw Zango just launched and wanted to introduce myself...",
        "Named as CEO in a press interview about the launch",
        "verified",
    ],
    [
        "Maria Lopez", "maria.lopez@example.com", "BrightPath Recruiting", "Replied",
        "thread_d4e5f6", "2026-07-16T11:05:00Z",
        "Hi Maria, noticed BrightPath is hiring across three markets...",
        "Owner of a boutique UK recruiting agency, matches the target exactly",
        "verified",
    ],
    [
        "Sam Rivera", "sam.rivera@example.com", "Boutique HR", "Discarded",
        "", "", "",
        "Too generic a match -- not close enough to the ICP",
        "unverified",
    ],
    [
        "Tom Baker", "tom.baker@example.com", "Acme Health",
        "EXAMPLE -- clear this cell to queue the row",
        "", "", "",
        "Growth lead at a healthtech startup",
        "verified",
    ],
]

# Width in Excel character units, one per column of EXPECTED_HEADER.
_COLUMN_WIDTHS = [18, 30, 22, 30, 16, 22, 46, 52, 18]

_NOTES = [
    "Sendkeep -- lead sheet template",
    "",
    "HOW TO USE THIS FILE",
    "1. Put it in Google Drive and open it with Google Sheets (drag it into Drive, then",
    "   File > Save as Google Sheets).",
    "2. Delete the five example rows on the Leads tab. Keep row 1.",
    "3. Add your own leads: fill in Name, Email and Company and leave the rest empty.",
    "4. In Sendkeep, open Settings and choose this spreadsheet as your lead sheet.",
    "",
    "THE HEADER ROW",
    "Row 1 of the Leads tab must hold these nine labels, in this order:",
    ", ".join(EXPECTED_HEADER),
    "Sendkeep refuses to write statuses until all nine are there. That check is what",
    "stops it writing over data you keep in unlabeled columns, so it is worth keeping.",
    "",
    "WHO FILLS IN WHAT",
    "Name              you",
    "Email             you",
    "Company           you",
    "Status            both -- see below",
    "ThreadID          Sendkeep, when it sends",
    "SentAt            Sendkeep, when it sends",
    "EmailBody         Sendkeep, when it sends",
    "LeadReason        the lead sourcing agent, when it finds someone for you",
    "EmailConfidence   the lead sourcing agent",
    "",
    "THE STATUS COLUMN DECIDES WHO GETS EMAILED",
    "(blank)             queued -- Sendkeep emails this person on the next run",
    "Sent                already emailed; SentAt and ThreadID record when and where",
    "Replied             they answered, and the reply is waiting for you in Outreach",
    "Bounced             the address rejected the email permanently; never retried",
    "Delayed             delivery failed temporarily (full mailbox, greylisting) -- the",
    "                    address is probably fine, so it is not suppressed and replies",
    "                    are still watched for",
    "Discarded           skipped, never emailed",
    "Needs verification  sourced but unconfirmed, so it will not be emailed",
    "",
    "A blank Status is the only thing that makes a row eligible to send. That is why",
    "every example row in this file carries a Status and uses an @example.com address:",
    "nothing in this template can go out by accident, even if you connect it as-is.",
]


def _esc(value):
    """Escape text for an XML text node."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _col_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _row_xml(row_number, values, style=None):
    # Empty cells are omitted rather than written as empty strings: a skipped
    # cell reads as genuinely blank, which matters because a blank Status is
    # what marks a row sendable.
    cells = []
    for index, value in enumerate(values):
        text = "" if value is None else str(value)
        if not text:
            continue
        style_attr = f' s="{style}"' if style else ""
        cells.append(
            f'<c r="{_col_letter(index)}{row_number}"{style_attr} t="inlineStr">'
            f'<is><t xml:space="preserve">{_esc(text)}</t></is></c>'
        )
    return f'<row r="{row_number}">{"".join(cells)}</row>'


_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _leads_sheet_xml():
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>'
        for i, width in enumerate(_COLUMN_WIDTHS)
    )
    rows = [_row_xml(1, EXPECTED_HEADER, style=1)]
    rows += [_row_xml(n, row) for n, row in enumerate(DEMO_ROWS, start=2)]
    return (
        f"{_XML_DECL}"
        f'<worksheet xmlns="{_MAIN_NS}">'
        # Freeze row 1 so the labels stay visible while scrolling.
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols}</cols>"
        f'<sheetData>{"".join(rows)}</sheetData>'
        "</worksheet>"
    )


def _notes_sheet_xml():
    rows = "".join(_row_xml(n, [line]) for n, line in enumerate(_NOTES, start=1))
    return (
        f"{_XML_DECL}"
        f'<worksheet xmlns="{_MAIN_NS}">'
        '<cols><col min="1" max="1" width="88" customWidth="1"/></cols>'
        f"<sheetData>{rows}</sheetData>"
        "</worksheet>"
    )


def _styles_xml():
    # Two cell formats: 0 is the default, 1 is bold (used for the header row).
    return (
        f"{_XML_DECL}"
        f'<styleSheet xmlns="{_MAIN_NS}">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        "</fills>"
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        # Excel wants an explicit "Normal" style; without it some versions
        # report the workbook as needing repair.
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _workbook_xml():
    return (
        f"{_XML_DECL}"
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets>'
        f'<sheet name="{_esc(DATA_SHEET)}" sheetId="1" r:id="rId1"/>'
        f'<sheet name="{_esc(NOTES_SHEET)}" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    )


def build_xlsx():
    """Return the demo workbook as .xlsx bytes."""
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    doc_type = "application/vnd.openxmlformats-officedocument.spreadsheetml"

    parts = {
        "[Content_Types].xml": (
            f"{_XML_DECL}"
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{doc_type}.sheet.main+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{doc_type}.worksheet+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="{doc_type}.worksheet+xml"/>'
            f'<Override PartName="/xl/styles.xml" ContentType="{doc_type}.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f"{_XML_DECL}"
            f'<Relationships xmlns="{pkg_ns}">'
            f'<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": _workbook_xml(),
        "xl/_rels/workbook.xml.rels": (
            f"{_XML_DECL}"
            f'<Relationships xmlns="{pkg_ns}">'
            f'<Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{_REL_NS}/worksheet" Target="worksheets/sheet2.xml"/>'
            f'<Relationship Id="rId3" Type="{_REL_NS}/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": _leads_sheet_xml(),
        "xl/worksheets/sheet2.xml": _notes_sheet_xml(),
        "xl/styles.xml": _styles_xml(),
    }

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def build_csv():
    """The same grid as the workbook's Leads tab, for people who keep their
    leads as CSV (a CRM export, say).

    Note this is still a starting point for a Google Sheet, not an input format:
    nothing in the app parses CSV. Leads are read only through the Sheets API
    (sheets.get_all_rows), so the file has to be imported into Sheets first.

    A CSV carries no second tab, so the "How to use" guidance the workbook
    embeds lives at /getting-started for these users instead.
    """
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPECTED_HEADER)
    writer.writerows(DEMO_ROWS)
    # utf-8-sig: the BOM is what makes Excel open the file as UTF-8 instead of
    # guessing the local codepage. Google Sheets strips it on import.
    return buffer.getvalue().encode("utf-8-sig")
