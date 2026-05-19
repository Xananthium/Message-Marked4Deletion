#!/usr/bin/env python3
"""
render-invoice.py — DIS-8 invoice renderer

Usage:
    render-invoice.py <invoice_uuid>

Looks up the invoice + its customer + their primary active domain, then
writes two files to /var/sites/<fqdn>/invoices/:
    <invoice_number>.html   — minimal vanilla HTML, customer-viewable
    <invoice_number>.txt    — plain-text version for email body

Both paths are printed to stdout. The script does NOT email anything.
Operator hand-sends until the email service is wired (DIS-138).

If amount_cents == 0 the documents render a "COMPED — no payment required"
header in place of the payment-link placeholder.
"""

import os
import sys
import json
from datetime import datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras

DSN = os.environ.get(
    "PAPERCLIP_DSN",
    "postgres://paperclip:3f99b0afdbedc68b2a60c3bd4c9cc2af753d6a0cacf1a730@127.0.0.1:5432/paperclip",
)

SITES_ROOT = "/var/sites"

COMPANY_NAME = "Digital Disconnections"
COMPANY_EMAIL = "cass@digitaldisconnections.com"
COMPANY_REMIT_LINE = "Remit to: TBD (payment rail not yet selected — DIS-86)"


def cents_to_dollars(cents: int) -> str:
    return f"${Decimal(cents) / Decimal(100):,.2f}"


def fetch(invoice_id: str):
    conn = psycopg2.connect(DSN)
    conn.set_session(readonly=True)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  i.id, i.invoice_number, i.amount_cents, i.status,
                  i.line_items, i.issued_at, i.due_at, i.paid_at,
                  i.payment_reference, i.stripe_invoice_id, i.notes,
                  c.id AS customer_id, c.email AS customer_email,
                  c.name AS customer_name, c.business_name,
                  c.billing_address,
                  (
                    SELECT fqdn FROM domains
                    WHERE customer_id = c.id AND status = 'active'
                    ORDER BY created_at ASC LIMIT 1
                  ) AS fqdn
                FROM invoices i
                JOIN customers c ON c.id = i.customer_id
                WHERE i.id = %s
                """,
                (invoice_id,),
            )
            row = cur.fetchone()
            if not row:
                sys.exit(f"ERROR: no invoice with id {invoice_id}")
            return row
    finally:
        conn.close()


def render_html(inv: dict) -> str:
    line_items = inv["line_items"] or []
    rows = "".join(
        f"""<tr>
  <td>{li.get('sku', '')}</td>
  <td>{li.get('description', '')}</td>
  <td style="text-align:right">{cents_to_dollars(li.get('amount_cents', 0))}</td>
</tr>"""
        for li in line_items
    )

    is_comped = inv["amount_cents"] == 0
    if is_comped:
        payment_block = """<div style="background:#f0f9ff;border:2px solid #0284c7;padding:16px;margin:24px 0;border-radius:6px">
  <strong style="color:#0284c7">COMPED &mdash; no payment required</strong><br>
  This invoice is recorded for accounting purposes. No action needed.
</div>"""
    else:
        payment_block = """<div style="background:#fef3c7;border:2px solid #d97706;padding:16px;margin:24px 0;border-radius:6px">
  <strong>Pay via [TBD payment link]</strong><br>
  Payment instructions will be sent separately. (DIS-86 pending)
</div>"""

    issued = inv["issued_at"].strftime("%Y-%m-%d") if inv["issued_at"] else "(draft)"
    due = inv["due_at"].strftime("%Y-%m-%d") if inv["due_at"] else "(not set)"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice {inv['invoice_number']}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 720px; margin: 40px auto; padding: 0 20px; color: #1f2937; }}
  h1 {{ border-bottom: 2px solid #1f2937; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  th, td {{ padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: left; }}
  th {{ background: #f9fafb; }}
  .meta {{ display: flex; justify-content: space-between; margin: 16px 0; }}
  .total {{ font-size: 1.4em; font-weight: bold; text-align: right; margin-top: 16px; }}
  footer {{ margin-top: 40px; font-size: 0.85em; color: #6b7280; border-top: 1px solid #e5e7eb; padding-top: 16px; }}
</style>
</head>
<body>
<h1>Invoice {inv['invoice_number']}</h1>

<div class="meta">
  <div>
    <strong>From:</strong><br>
    {COMPANY_NAME}<br>
    {COMPANY_EMAIL}
  </div>
  <div>
    <strong>To:</strong><br>
    {inv['customer_name'] or ''}<br>
    {inv['business_name'] or ''}<br>
    {inv['customer_email']}<br>
    {inv['fqdn'] or ''}
  </div>
  <div>
    <strong>Issued:</strong> {issued}<br>
    <strong>Due:</strong> {due}<br>
    <strong>Status:</strong> {inv['status']}
  </div>
</div>

<table>
  <thead>
    <tr><th>SKU</th><th>Description</th><th style="text-align:right">Amount</th></tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>

<div class="total">Total: {cents_to_dollars(inv['amount_cents'])}</div>

{payment_block}

<footer>
{COMPANY_REMIT_LINE}<br>
Questions? Reply to {COMPANY_EMAIL}.
</footer>
</body>
</html>
"""


def render_txt(inv: dict) -> str:
    line_items = inv["line_items"] or []
    lines = []
    lines.append(f"INVOICE {inv['invoice_number']}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"From: {COMPANY_NAME} <{COMPANY_EMAIL}>")
    to_parts = [
        inv["customer_name"] or "",
        inv["business_name"] or "",
        inv["customer_email"] or "",
        inv["fqdn"] or "",
    ]
    lines.append("To:   " + " / ".join(p for p in to_parts if p))
    lines.append("")
    issued = inv["issued_at"].strftime("%Y-%m-%d") if inv["issued_at"] else "(draft)"
    due = inv["due_at"].strftime("%Y-%m-%d") if inv["due_at"] else "(not set)"
    lines.append(f"Issued: {issued}   Due: {due}   Status: {inv['status']}")
    lines.append("")
    lines.append("-" * 60)
    lines.append(f"{'SKU':<20}{'Description':<30}{'Amount':>10}")
    lines.append("-" * 60)
    for li in line_items:
        sku = (li.get("sku") or "")[:18]
        desc = (li.get("description") or "")[:28]
        amt = cents_to_dollars(li.get("amount_cents", 0))
        lines.append(f"{sku:<20}{desc:<30}{amt:>10}")
    lines.append("-" * 60)
    lines.append(f"{'TOTAL':<50}{cents_to_dollars(inv['amount_cents']):>10}")
    lines.append("")
    if inv["amount_cents"] == 0:
        lines.append("*** COMPED — no payment required ***")
        lines.append("This invoice is recorded for accounting purposes only.")
    else:
        lines.append("Pay via [TBD payment link] — instructions to follow (DIS-86 pending).")
    lines.append("")
    lines.append(COMPANY_REMIT_LINE)
    lines.append(f"Questions? Reply to {COMPANY_EMAIL}.")
    lines.append("")
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: render-invoice.py <invoice_uuid>")
    invoice_id = sys.argv[1]

    inv = fetch(invoice_id)
    fqdn = inv["fqdn"]
    if not fqdn:
        sys.exit(
            f"ERROR: customer {inv['customer_id']} has no active domain; "
            "cannot place customer-facing invoice files. Add a domain row first."
        )

    out_dir = os.path.join(SITES_ROOT, fqdn, "invoices")
    os.makedirs(out_dir, exist_ok=True)

    html_path = os.path.join(out_dir, f"{inv['invoice_number']}.html")
    txt_path = os.path.join(out_dir, f"{inv['invoice_number']}.txt")

    with open(html_path, "w") as f:
        f.write(render_html(inv))
    with open(txt_path, "w") as f:
        f.write(render_txt(inv))

    print(f"HTML: {html_path}")
    print(f"TXT:  {txt_path}")
    print(f"Total: {cents_to_dollars(inv['amount_cents'])} ({inv['status']})")


if __name__ == "__main__":
    main()
