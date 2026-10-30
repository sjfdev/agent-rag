"""Generate the multi-format demo dataset under data/Demo/.

Idempotent: running it again overwrites the same files (ingestion will then
report each one as updated because the bytes change only if content changes).

Usage:
    python scripts/generate_demo_files.py
"""

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "Demo"


def write_text_files(root: Path):
    (root / "policy.md").write_text(
        """# Demo IT & Security Policy

## VPN access

All employees must request VPN access through the self-service IT portal.
Requests are approved by the line manager within one business day. The VPN
client profile is downloaded from the portal after approval.

## Passwords

Passwords must be at least 14 characters and are rotated every 180 days.
Multi-factor authentication is mandatory for all remote sessions.

## Device encryption

Laptops must have full-disk encryption enabled before connecting to the
corporate network.

| Topic | Rule |
| --- | --- |
| VPN | Request in the IT portal; manager approval required |
| Wi-Fi | Guest network: DEMO-GUEST (password at reception) |
| USB storage | Encrypted devices only |
""",
        encoding="utf-8",
    )

    (root / "faq.txt").write_text(
        """Demo support FAQ

Q: How do I request VPN access?
A: Open a request in the self-service IT portal; your manager approves it.

Q: What is the Wi-Fi password for visitors?
A: Use the DEMO-GUEST network; the password is available at reception.

Q: 访客如何连接 Wi-Fi？
A: 使用访客网络 DEMO-GUEST，密码可在前台获取。

Q: Who owns the demo environment?
A: The QA team owns it; ask in the #demo-support channel.
""",
        encoding="utf-8",
    )

    (root / "runbook.html").write_text(
        """<!doctype html>
<html>
<head><title>Demo Service Runbook</title></head>
<body>
  <h1>Demo Service Runbook</h1>
  <p>Smoke tests run every weekday at 09:30 and on every merge to main.</p>
  <h2>Incident response</h2>
  <p>Page the on-call engineer if error rate exceeds 2% for five minutes.</p>
  <h2>Rollback</h2>
  <p>Roll back with the previous container tag; the database is backward compatible.</p>
  <script>console.log("SHOULD_NOT_BE_INDEXED");</script>
</body>
</html>
""",
        encoding="utf-8",
    )

    (root / "metadata.json").write_text(
        """{
  "service": "demo-api",
  "owner": "qa-team",
  "sla": "99.9%",
  "environment": "demo",
  "refresh": "every Monday 06:00 UTC"
}
""",
        encoding="utf-8",
    )

    (root / "contacts.csv").write_text(
        """name,team,email,responsibility
Alice,QA,alice@example.com,smoke tests
Bob,Ops,bob@example.com,on-call rotation
Carol,Security,carol@example.com,access reviews
""",
        encoding="utf-8",
    )


def write_docx(root: Path):
    from docx import Document

    doc = Document()
    doc.add_heading("Demo Release Checklist", level=1)
    doc.add_paragraph(
        "Every release must complete the following steps before production deploy."
    )
    doc.add_paragraph("1. Freeze the release branch and announce it in #releases.")
    doc.add_paragraph("2. Run the full regression suite; smoke tests alone are not enough.")
    doc.add_paragraph("3. Obtain sign-off from QA and the on-call engineer.")

    table = doc.add_table(rows=3, cols=3)
    rows = [
        ("Step", "Owner", "Done"),
        ("Run regression suite", "QA", "yes"),
        ("Verify rollback plan", "Ops", "yes"),
    ]
    for row, values in zip(table.rows, rows, strict=False):
        for cell, value in zip(row.cells, values, strict=False):
            cell.text = value

    doc.core_properties.title = "Demo Release Checklist"
    doc.save(root / "release_checklist.docx")


def write_pptx(root: Path):
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Demo Onboarding"
    body = slide.shapes.add_textbox(0, 0, 6000000, 2000000)
    body.text_frame.text = (
        "First week: create accounts, request VPN access, finish security training."
    )
    slide.notes_slide.notes_text_frame.text = (
        "Ask your buddy for a tour of the demo environment."
    )

    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "Second week"
    body2 = slide2.shapes.add_textbox(0, 0, 6000000, 2000000)
    body2.text_frame.text = "Shadow the on-call rotation and ship one small fix."

    prs.save(root / "onboarding.pptx")


def write_xlsx(root: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Overview"
    ws.append(["metric", "value"])
    ws.append(["test_coverage", "85%"])
    ws.append(["smoke_duration", "12m"])
    ws.append(["open_bugs", 3])

    detail = wb.create_sheet("Details")
    detail.append(["area", "owner"])
    detail.append(["API", "Alice"])
    detail.append(["UI", "Bob"])
    wb.save(root / "metrics.xlsx")


def write_pdf(root: Path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = root / "program_overview.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, 760, "Demo Program Overview")
    c.setFont("Helvetica", 12)
    lines = [
        "The demo environment is refreshed every Monday at 06:00 UTC.",
        "Smoke tests run on every merge; the full regression suite runs nightly.",
        "Demo data is synthetic and must never be used for real customers.",
    ]
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 22
    c.showPage()
    c.save()


def main():
    root = DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    write_text_files(root)
    write_docx(root)
    write_pptx(root)
    write_xlsx(root)
    write_pdf(root)

    files = sorted(p for p in root.iterdir() if p.is_file() and not p.name.startswith("."))
    print(f"Generated {len(files)} demo files in {root}:")
    for path in files:
        print(f"  {path.name} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
