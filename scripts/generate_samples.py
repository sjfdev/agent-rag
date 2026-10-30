"""Generate sample department PDFs for local testing of the RAG agent."""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DOCS = {
    "HR": {
        "employee_handbook.pdf": [
            ("h1", "Acme Corp Employee Handbook"),
            ("h2", "1. Working Hours"),
            (
                "p",
                "Standard working hours are 09:00 to 18:00, Monday through Friday, with a "
                "one-hour lunch break. Core collaboration hours are 10:00 to 16:00, during "
                "which all employees are expected to be reachable on the internal chat tool.",
            ),
            ("h2", "2. Annual Leave"),
            (
                "p",
                "Full-time employees accrue 15 days of paid annual leave per calendar year. "
                "Leave accrues monthly at 1.25 days per month. Up to 5 unused days may be "
                "carried over to the first quarter of the following year; any remaining "
                "balance expires on March 31.",
            ),
            (
                "p",
                "Requests for leave longer than 3 consecutive working days must be submitted "
                "at least 10 business days in advance and approved by the direct manager.",
            ),
            ("h2", "3. Sick Leave"),
            (
                "p",
                "Employees are entitled to 10 days of paid sick leave per year. A medical "
                "certificate is required for any sick leave exceeding 2 consecutive days.",
            ),
            ("h2", "4. Remote Work"),
            (
                "p",
                "Employees may work remotely up to 2 days per week with manager approval. "
                "Remote work is not permitted during the probation period of the first "
                "3 months, except in exceptional circumstances approved by HR.",
            ),
            ("h2", "5. Probation and Confirmation"),
            (
                "p",
                "The standard probation period is 3 months. A performance review is conducted "
                "at the end of month 3. Confirmation letters are issued by HR within 5 working "
                "days after a successful review.",
            ),
            ("h2", "6. Resignation"),
            (
                "p",
                "Employees wishing to resign must provide 30 days written notice. During "
                "probation, the notice period is 3 days. Final settlement includes unused "
                "annual leave and is paid on the next regular payroll cycle.",
            ),
        ],
        "benefits_guide.pdf": [
            ("h1", "Acme Corp Benefits Guide"),
            ("h2", "1. Medical Insurance"),
            (
                "p",
                "All full-time employees are enrolled in the company medical plan from the "
                "first day of employment. The plan covers 90 percent of inpatient costs and "
                "70 percent of outpatient costs up to an annual limit of 50,000 CNY.",
            ),
            ("h2", "2. Annual Health Check"),
            (
                "p",
                "One free health check per year is provided for employees who have completed "
                "12 months of service. Booking is handled through the HR portal each October.",
            ),
            ("h2", "3. Learning Budget"),
            (
                "p",
                "Each employee has an annual learning budget of 5,000 CNY for courses, "
                "certifications, and technical books. Receipts must be submitted through the "
                "expense system with the category Learning and Development.",
            ),
            ("h2", "4. Employee Assistance Program"),
            (
                "p",
                "Confidential counseling is available 24/7 through the Employee Assistance "
                "Program. Up to 6 free sessions per year are provided for each employee and "
                "their immediate family members.",
            ),
        ],
    },
    "Finance": {
        "expense_reimbursement_policy.pdf": [
            ("h1", "Expense Reimbursement Policy"),
            ("h2", "1. Submission Deadline"),
            (
                "p",
                "All expense claims must be submitted through the finance system by the 5th "
                "day of the month following the expense date. Claims submitted after the "
                "deadline are processed in the next monthly cycle.",
            ),
            ("h2", "2. Required Documents"),
            (
                "p",
                "Every claim requires a valid invoice (fapiao) with the company tax ID. "
                "Credit card slips alone are not accepted. Missing invoices must be replaced "
                "within 30 days or the claim will be rejected.",
            ),
            ("h2", "3. Approval Chain"),
            (
                "p",
                "Claims up to 2,000 CNY require direct manager approval only. Claims between "
                "2,000 and 10,000 CNY require manager and department head approval. Claims "
                "above 10,000 CNY additionally require CFO approval.",
            ),
            ("h2", "4. Reimbursable Categories"),
            (
                "p",
                "Reimbursable expenses include business travel, client entertainment, office "
                "supplies, and approved training. Personal expenses, alcohol without a client "
                "present, and traffic fines are not reimbursable under any circumstances.",
            ),
            ("h2", "5. Payment Timeline"),
            (
                "p",
                "Approved claims are paid by bank transfer within 10 working days after final "
                "approval. Payment status can be tracked in the finance system under My Claims.",
            ),
        ],
        "travel_policy.pdf": [
            ("h1", "Business Travel Policy"),
            ("h2", "1. Booking"),
            (
                "p",
                "All business travel must be booked through the corporate travel portal at "
                "least 7 days in advance. Economy class is standard for flights under 4 hours; "
                "premium economy is allowed for flights longer than 4 hours with manager approval.",
            ),
            ("h2", "2. Daily Allowance"),
            (
                "p",
                "Domestic travel allowance is 200 CNY per day for tier-1 cities and 150 CNY "
                "per day for other cities. International allowance follows the published "
                "country rate table in the finance portal.",
            ),
            ("h2", "3. Accommodation Limits"),
            (
                "p",
                "Hotel limits per night are 600 CNY in tier-1 cities, 450 CNY in tier-2 cities, "
                "and 350 CNY elsewhere. Amounts above the limit require pre-approval from the "
                "department head before booking.",
            ),
            ("h2", "4. Expense Reports"),
            (
                "p",
                "A travel expense report must be filed within 7 working days after returning. "
                "Attach boarding passes, hotel invoices, and receipts for any single item over "
                "100 CNY.",
            ),
        ],
        "confidential_budget_2026.pdf": [
            ("h1", "CONFIDENTIAL - 2026 Department Budget"),
            (
                "p",
                "This document is restricted to the Finance department. It contains salary "
                "bands and budget allocations that must not be shared outside Finance.",
            ),
            ("h2", "Salary Bands"),
            (
                "p",
                "Engineering: junior 18-25k, senior 30-45k, staff 50-70k per month. Sales: "
                "base 12-20k plus commission. Finance: analyst 15-22k, manager 28-40k.",
            ),
            ("h2", "Budget Allocation"),
            (
                "p",
                "Total 2026 operating budget is 48,000,000 CNY. Marketing 12,000,000 CNY, "
                "R&D 22,000,000 CNY, Sales 8,000,000 CNY, G&A 6,000,000 CNY.",
            ),
        ],
    },
    "Tech": {
        "coding_standards.pdf": [
            ("h1", "Engineering Coding Standards"),
            ("h2", "1. Code Review"),
            (
                "p",
                "Every change must go through a pull request with at least one approving "
                "reviewer. Reviewers should respond within one business day. Self-approval is "
                "not permitted, and administrators cannot bypass review for production branches.",
            ),
            ("h2", "2. Branch Naming"),
            (
                "p",
                "Branches must follow the pattern feat/<ticket>, fix/<ticket>, or chore/<ticket>. "
                "Direct pushes to main and release branches are blocked by branch protection. "
                "Squash merge is the default merge strategy.",
            ),
            ("h2", "3. Commit Messages"),
            (
                "p",
                "Commits must follow the Conventional Commits format, for example "
                "feat(auth): add refresh token rotation. Each commit should be atomic and "
                "compile on its own.",
            ),
            ("h2", "4. Testing Requirements"),
            (
                "p",
                "New business logic requires unit tests with at least 80 percent line coverage "
                "for the changed files. Integration tests are required for any change touching "
                "the payment or authentication modules.",
            ),
            ("h2", "5. Formatting and Linting"),
            (
                "p",
                "Python code is formatted with ruff format and linted with ruff check. Type "
                "hints are required on all public functions. CI fails on any lint error or "
                "failing test, and merging is blocked until CI is green.",
            ),
        ],
        "security_policy.pdf": [
            ("h1", "Engineering Security Policy"),
            ("h2", "1. Secrets Management"),
            (
                "p",
                "Never commit secrets, API keys, or credentials to git. Use the approved "
                "secrets manager and inject values at runtime. If a secret is leaked, rotate "
                "it immediately and report the incident to the security team.",
            ),
            ("h2", "2. Access Control"),
            (
                "p",
                "Production access is granted on a least-privilege basis and reviewed "
                "quarterly. VPN is mandatory for any access to internal systems from outside "
                "the office network. Shared accounts are prohibited.",
            ),
            ("h2", "3. Dependency Management"),
            (
                "p",
                "Dependencies must be pinned and scanned weekly for vulnerabilities. Critical "
                "vulnerabilities must be patched within 7 days, high severity within 30 days. "
                "Adding a new third-party library requires a security review.",
            ),
            ("h2", "4. Incident Response"),
            (
                "p",
                "Report suspected incidents to security@acme.example within 1 hour of "
                "discovery. The on-call engineer must acknowledge within 15 minutes. A "
                "post-incident review is required within 5 business days for any severity-1 "
                "incident.",
            ),
        ],
        "oncall_runbook.pdf": [
            ("h1", "On-Call Runbook"),
            ("h2", "1. Rotation"),
            (
                "p",
                "On-call rotates weekly, starting Monday 10:00. Primary and secondary "
                "responders are assigned per team. Handover notes are required and must be "
                "posted in the team channel before the rotation ends.",
            ),
            ("h2", "2. Severity Levels"),
            (
                "p",
                "SEV1 is a full outage or data loss and requires immediate escalation. SEV2 is "
                "degraded service affecting a large customer segment. SEV3 is a minor issue "
                "with a workaround available.",
            ),
            ("h2", "3. Escalation"),
            (
                "p",
                "If an incident is not resolved within 30 minutes for SEV1 or 2 hours for "
                "SEV2, escalate to the engineering manager and the incident commander. Status "
                "updates are posted every 30 minutes until resolution.",
            ),
            ("h2", "4. Postmortem"),
            (
                "p",
                "A blameless postmortem is required for every SEV1 and SEV2 incident. Action "
                "items must be tracked in the issue tracker with an owner and a due date within "
                "10 business days.",
            ),
        ],
    },
}


def build_pdf(path: Path, blocks):
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1x", parent=styles["Heading1"], fontSize=18, spaceAfter=12)
    h2 = ParagraphStyle("H2x", parent=styles["Heading2"], fontSize=13, spaceAfter=8)
    body = ParagraphStyle("Bodyx", parent=styles["BodyText"], fontSize=10.5, leading=16, spaceAfter=8)
    style_map = {"h1": h1, "h2": h2, "p": body}

    story = []
    for kind, text in blocks:
        story.append(Paragraph(text, style_map[kind]))
        story.append(Spacer(1, 4))

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=path.stem,
    )
    doc.build(story)


def main():
    for dept, files in DOCS.items():
        folder = DATA_DIR / dept
        folder.mkdir(parents=True, exist_ok=True)
        for filename, blocks in files.items():
            target = folder / filename
            build_pdf(target, blocks)
            print(f"created {target}")


if __name__ == "__main__":
    main()
