# Retention & Advance on Invoices (`ksa_retention_advance`)

Odoo 19 · depends on `account`, `sale`, `l10n_sa` (Saudi chart of accounts).

## 1. Purpose
For contracting projects, the client pays an advance at the start and receives a monthly progress invoice.
From each invoice the client holds back a **retention** (guarantee, usually 10% of the work done, paid after handover)
and **recovers part of the advance**. VAT stays on the full value of the work done. This module records all of this
on the customer invoice, keeps the amounts in separate places in the ledger, and reports the balances per customer and project.

## 2. Functionality
| Area | What it does |
|---|---|
| Settings | Company-level Retention Receivable account, default retention %, optional journal for the generated entries |
| Customer invoice | Retention %, expected handover date, advance payment to recover from, recovery by % or fixed amount; shows retention, advance recovery and **Amount to Pay Now** |
| Posting | Retention is moved to the Retention Receivable account with its own due date (handover date); the rest stays in the normal receivable |
| Advance recovery | Matched automatically against the selected advance payment; cannot exceed what is left on that advance |
| Release Retention | Per customer: pick invoices, release in full or part (amount + date); the amount becomes normal receivable, due on the release date |
| Credit notes | Full or partial; retention and recovered advance are returned in proportion |
| Reset to draft | Generated entries are reversed; re-posting creates them again, nothing is duplicated |
| Report | Per customer and project, as at any date, with Excel export |
| Print | Invoice PDF shows retention, advance recovery and amount to pay now (English and Arabic) |
| Currency / companies | Works for foreign-currency invoices and multi-company databases |

## 3. Configuration
**New account (created by the accountant):** *Retention Receivable* – type Receivable (or Current Asset), *Allow Reconciliation* ticked.
**Settings** – Accounting ▸ Retention & Advance ▸ Settings (accounting advisers):
* Retention Receivable Account – per company.
* Default Retention % – pre-filled on new customer invoices, can be changed per invoice.
* Journal for Retention Entries – optional miscellaneous journal; if empty the company's first miscellaneous journal is used.

**Advance payment:** register the advance as a normal customer payment (no invoice), tick **Project Advance** and optionally select the
**Sales Order** (project) it belongs to. Only ticked payments can be chosen on invoices.

**Invoice** – tab *Retention & Advance*: Retention %, Expected Handover Date (required when retention > 0), Advance Payment,
Recovery method (% of work done or fixed amount).

## 4. Where each amount is accounted
Example: work done 100,000, VAT 15%, retention 10%, recovery 20%, advance 200,000.

| Amount | Account | Due / status |
|---|---|---|
| Work done 100,000 | Revenue | – |
| VAT 15,000 (on the full 100,000) | VAT payable | – |
| **Amount to pay now 85,000** | Normal Receivable (open item of the invoice) | Due on the invoice due date; shown in the invoice residual and the aged receivable |
| **Retention 10,000** | **Retention Receivable** | Still owed by the client; due on the handover date |
| **Advance recovered 20,000** | Normal Receivable, matched against the advance payment | Advance left: 180,000 |

Retention is calculated on the untaxed amount; the invoice total (115,000) and VAT are not changed.

## 5. Journal entries
Everything below is a *new* entry; posted entries are never edited or deleted. Undoing means reversing.

1. **Invoice (standard Odoo):** Dr Receivable 115,000 / Cr Revenue 100,000 / Cr VAT 15,000.
2. **Retention / advance entry (created automatically when the invoice is posted, same date, miscellaneous journal):**
   Dr Retention Receivable 10,000 (due = handover date) · Cr Receivable 10,000 · Dr Receivable 20,000 · Cr Receivable 20,000.
   The two credits are reconciled with the invoice (invoice residual becomes 85,000); the 20,000 debit is reconciled with the advance payment.
3. **Retention release (wizard, amount X, date D):** Dr Receivable X (due D) / Cr Retention Receivable X (reconciled with the retention held).
   The client then pays X like any other amount due. A release can be undone (it is reversed).
4. **Credit note** (ratio *r* = credit note ÷ invoice, cumulative maximum 100%): Dr Receivable (10,000·r + 20,000·r) reconciled with the credit note ·
   Cr Retention Receivable 10,000·r reconciled with the retention held · Cr Receivable 20,000·r left open and tagged to the advance
   (advance given back, available again for recovery).
5. **Reset to draft:** the retention/advance (or credit adjustment) entry is reversed and its reconciliations released; posting again creates a fresh entry.

**Foreign currency:** lines carry the invoice currency; the company-currency value follows the invoice's own rate, so the split creates no exchange difference.
**Multi-company:** each entry is created in the invoice's company with that company's account and journal.
**Lock dates:** entries follow Odoo's lock dates. The release wizard refuses a release date inside a locked period (Odoo would otherwise silently move it).

## 6. Rules and controls
* Recovery cannot exceed the advance left (checked when saving and again when posting); retention + recovery cannot exceed the invoice total.
* Retention and advance settings of a posted invoice are locked; reset to draft first.
* An invoice cannot be reset to draft once its retention was released or credited, or while it has credit notes (undo those first).
* A credit note cannot return retention that was already released (undo the release first).
* Generated entries cannot be reset to draft directly; use the source invoice / credit note, or *Undo Retention Release*.
* Release cannot exceed the retention balance of the selected invoices; a partial amount is spread oldest invoice first and needs invoices in one currency.

## 7. Release Retention
Accounting ▸ Retention & Advance ▸ Release Retention, or the **Release Retention** button on the customer. Choose the customer, tick the invoices,
choose *Full* or *Part of the retention* (enter the amount) and the release date. Restricted to accounting advisers.

## 8. Report
Accounting ▸ Retention & Advance ▸ Report. Choose the *As at* date (and optionally companies and customers), press **Compute**, then **Export to Excel**.
Per customer and project (sales order; invoices without a sales order are grouped under the customer):

Work done (excl. VAT) · Total invoiced · Retention held · Retention released · Retention balance · Advance received · Advance recovered · Advance balance.

Amounts are in company currency at the original rates; credit notes reduce invoiced, retention held and advance recovered.

## 9. Assumptions
1. Retention and recovery % apply to the untaxed amount (work done), rounded in the invoice currency.
2. The handover date is entered per invoice.
3. One advance payment per invoice. Advance left = open credit of the payment + advance given back by credit notes.
4. The project of an invoice is the sales order of its lines (the first one if there are several).
5. Credit-note proportion uses the document total ratio.
6. Advance received in the report counts only payments ticked *Project Advance*.

## 10. Not included
* VAT due on the advance when it is received.
* Several advances on one invoice; a separate handover date per project.
* Revaluation of retention balances in foreign currency.

Note: because the retention and advance are reconciled against the invoice, a posted invoice shows payment status *Partially paid* until the amount to pay now is paid.
