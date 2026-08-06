Ask a finance executive at an Indian SMB what GST filing costs them and you will get an answer in rupees — the software licence, the CA's monthly retainer. Ask what it costs in hours and the number is much larger and much less visible.

Almost none of those hours are spent filing. Filing is a form submission. The hours go into the work that has to happen before the form can be filled in honestly, and that work is mostly document handling.

This is about where AI genuinely helps in GST compliance, where it should be kept well away, and how to tell the difference before you buy something.

## The monthly cycle, honestly described

A small business with a few hundred purchase invoices a month runs roughly this loop.

Invoices arrive as PDFs by email, as photographs on WhatsApp, as scans from a vendor portal, and as paper. Someone types them into Tally or a spreadsheet. The purchase register that results is compared against GSTR-2B, the auto-drafted statement of input tax credit derived from what suppliers actually filed. Mismatches are chased. Reversals under Rules 37, 42 and 43 are computed, or more commonly forgotten. GSTR-1 and GSTR-3B data is assembled and submitted.

The first step in that list consumes more time than everything after it. Data entry is the bottleneck, and it is a bottleneck made of unstructured documents in inconsistent formats — which is precisely the problem machine learning has become good at.

## Where AI earns its place

**Extraction from heterogeneous documents.** A supplier invoice contains a GSTIN, an invoice number, a date, a taxable value, a tax rate split across CGST/SGST or IGST, and HSN codes per line. Every supplier lays this out differently. Template-based OCR fails on the second vendor; a model that has learned what a GSTIN looks like in context does not care about layout. This is the single highest-value application in the stack, and it is unglamorous.

**Classification of exceptions.** Once a mismatch is found, it belongs to a class: invoice missing from GSTR-2B, present with a different value, present with a different tax split, duplicated, or recorded against the wrong supplier. Each class has a different remedy, and sorting them is pattern work.

**Reading supplier behaviour.** A supplier who has filed late in four of the last six months is a leading indicator for the credit you are about to not receive. That history is already in your data; nobody has time to compute it by hand.

**Drafting the chase.** The email to a supplier about a missing invoice is templated work with a few variables. Generating it is safe, sending it without a human glance is not.

## Where AI has no business being

**The tax arithmetic.** Rate application, reversal computation under Rules 42 and 43, the 180-day clock in Rule 37 — these are deterministic rules with legal consequences. They belong in code you can read, test and point at during an assessment. A language model that computes a reversal is a system whose output you cannot reproduce or defend.

**The eligibility decision.** Whether credit is available on a given expense is a legal question with a documented basis. A tool can surface the rule and flag the risk. It should not silently decide.

**Anything irreversible.** Filing is submission to a government portal. The gap between "prepares return data" and "files your return unattended" is the whole safety margin, and it should not be crossed to save two minutes a month.

The useful mental model: AI turns documents into structured data, deterministic code turns structured data into tax positions, and a human approves anything that leaves the building.

## Matching that isn't string equality

The reason reconciliation resists automation is that exact matching does not work on real data. Invoice numbers are transcribed with different prefixes, leading zeros and separators on each side. Values differ by a rupee from rounding. Dates land either side of a month boundary.

What works is a scored match over a candidate set — GSTIN, then a normalised invoice number, then value within tolerance, then date within a window — with the tolerance configurable, because a business with thousands of small invoices wants a different threshold from one with fifty large ones.

Match confidence should be visible. High-confidence matches clear silently; the middle band goes to a human queue ordered by rupee value at risk. Reviewing three hundred exceptions is a day. Reviewing the eleven that account for most of the exposure is twenty minutes, and it is the same day's work in terms of credit protected.

## The reversals nobody tracks

Two of them, consistently:

**Rule 37.** Credit taken on an invoice you have not paid within the prescribed window has to be reversed. This requires joining the purchase register against payments, which is a join nobody runs, which is why this surfaces during an audit rather than during a month-end.

**Rules 42 and 43.** Proportionate reversal where inputs and capital goods are used partly for exempt or non-business supply. Businesses with any exempt turnover owe this monthly and settle it annually, and the monthly part is routinely skipped.

Neither is hard to compute. Both are hard to *remember*, which is an argument for a system rather than for a smarter person.

## What to automate first

In order of return:

1. **Intake and extraction.** This is where the hours are.
2. **Reconciliation with tolerances and a ranked exception queue.** This is where the money is.
3. **Reversal tracking.** This is where the audit risk is.
4. **Return data preparation.** Genuinely useful, and the smallest of the four.

A tool that does step four beautifully and leaves you typing invoices has automated the part that was never the problem.

## Where GSTBot fits

[GSTBot](https://gstbot.aiknol.com) is built around that ordering. Bulk intake by PDF, photo or Excel with AI extraction of vendor, GSTIN, invoice number, amount, tax rate and HSN code. Reconciliation against GSTR-2B with configurable tolerances rather than exact string matching. Every exception classified with a recommended action. Rule 37, 42 and 43 tracking. Supplier filing health scored from history. GSTR-1 and GSTR-3B data prepared for the portal.

It exists because reconciliation tools that work properly have historically been priced for enterprises, while the tools priced for small businesses mostly do filing — which, as above, is the part that was already easy.

It does not file for you, and it does not decide eligibility for you. If what you want is a service where compliance leaves your hands entirely, what you want is a CA, and a good one is worth the retainer.
