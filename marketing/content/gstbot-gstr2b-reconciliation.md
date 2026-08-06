Every month, GSTR-2B reconciliation somewhere in India begins the same way: a finance team opens the downloaded GSTR-2B JSON, opens the purchase register next to it, and starts scrolling. Two columns of invoice numbers, two columns of amounts, and a growing suspicion that the numbers do not agree.

They usually do not. And the gap between them is money — Input Tax Credit you are entitled to and did not claim, or ITC you claimed and will be asked to reverse with interest.

This is the first post in a series on **AI in tax compliance**. It starts with GSTR-2B reconciliation because reconciliation is where the money actually leaks.

## What GSTR-2B reconciliation is reconciling against

GSTR-2B is an auto-drafted ITC statement. Your suppliers file their outward supplies in GSTR-1; the portal aggregates everything filed against your GSTIN and hands you a **static** statement for the period.

Static is the important word. GSTR-2A is dynamic — it keeps changing as suppliers file late. GSTR-2B is frozen once generated, which is exactly what makes it the reference document for a return you are about to file. You are reconciling against a snapshot, not a moving target.

The consequence that catches people out: since the amendment to Rule 36(4) and the insertion of Section 16(2)(aa), ITC is available essentially only to the extent it appears in GSTR-2B. An invoice sitting in your purchase register that your supplier never filed is not a claim you can make. It is a conversation you need to have with your supplier.

## The four ways a line fails to match

Reconciliation sounds like a single operation. It is really four different problems wearing one name, and they have completely different remedies.

**1. Exact match.** Invoice number, GSTIN, taxable value and tax amounts all agree. Nothing to do. In most books this is 70–85% of lines, and it is the boring majority that manual review still spends most of its time confirming.

**2. Present in both, values differ.** The supplier filed ₹1,18,000 where your books say ₹1,17,940. Rounding, a freight line captured differently, a rate applied at 18% on one side and 12% on the other. Small individually, systematic in aggregate. The remedy is a correction — yours or theirs — and the decision of which depends on which document is right, not which is bigger.

**3. In your books, not in GSTR-2B.** The supplier has not filed, has filed late, or has filed against the wrong GSTIN. This is the expensive one. You cannot claim it this period. You can claim it when it appears, subject to the Section 16(4) time limit for the financial year. Until then it is a receivable from your supplier's compliance behaviour.

**4. In GSTR-2B, not in your books.** Something was filed against your GSTIN that you did not record. Usually an invoice that never reached accounts payable. Occasionally something more interesting.

The fourth category is the one people ignore because it looks like free money. It is not — claiming credit for a supply you cannot evidence is the kind of thing that comes back at assessment.

## Matching is harder than string equality

The naive implementation compares invoice numbers. It fails immediately, because invoice numbers in the wild look like this:

```
INV/2026/0042      INV-2026-42        inv 2026 42
2026/INV/42        42                 INV/2026/0042 (Rev 1)
```

All six can be the same invoice. Your supplier's ERP formats it one way, your data-entry formats it another, and the portal faithfully carries whatever the supplier typed.

A reconciliation engine that is worth anything therefore matches on a **tuple with tolerances**, not on a key:

- Supplier GSTIN — the one field that is genuinely canonical, because it is validated at source.
- Normalised invoice number — case-folded, separators stripped, leading zeros removed.
- Invoice date, with a window. A date typed as 03/04/2026 vs 04/03/2026 is a day-month transposition, not a different invoice.
- Taxable value, with a rounding tolerance.

Then it scores candidates rather than accepting or rejecting them. A pair that agrees on GSTIN, amount and date but disagrees on invoice number is almost certainly a match with a typo. A pair that agrees on invoice number and disagrees on amount by 40% is almost certainly not.

This is where a language model earns its place, and it is worth being precise about where it does not. The LLM is not the matcher — a deterministic scoring function is faster, cheaper and auditable, and you want auditable. The LLM is for the messy edge: reading a scanned invoice into structured fields, deciding that "Reliance Retail Ltd" and "RELIANCE RETAIL LIMITED" are one vendor, telling you in a sentence why line 4,117 was flagged. Extraction and explanation, not arithmetic.

## The reversals nobody tracks until the audit

Matching gets you an eligible-ITC number. It is not your final number, because several rules claw credit back.

**Rule 37 — the 180-day rule.** If you have not paid your supplier within 180 days of the invoice date, the ITC you claimed on it must be reversed, with interest. This is a payables problem masquerading as a tax problem, and it is invisible to anyone reconciling GSTR-2B alone, because the invoice matched perfectly. The data you need is in your ageing report, not in the return.

**Rules 42 and 43 — proportionate reversal.** Where inputs are used partly for exempt supplies or non-business purposes, credit is reversible in proportion. Rule 42 covers inputs and input services; Rule 43 covers capital goods, spread over their useful life. A business with any exempt turnover at all needs this computed monthly, and it is almost never computed monthly.

**Section 16(4) — the deadline.** Credit for a financial year cannot be claimed indefinitely. An unmatched invoice from April is a problem you can still fix; the same invoice discovered eighteen months later is not.

A reconciliation that reports "eligible ITC: ₹X" without carrying these is reporting a number that will not survive contact with an assessment.

## Supplier health is a leading indicator

Here is the thing manual reconciliation structurally cannot do: it looks at one month.

Run the same match across twelve months and per-supplier patterns fall out immediately. Some suppliers file on time, every time. Some file two months late, every time. Some file for other customers and not for you, which is a specific and quite serious signal.

That history is directly actionable. A supplier who has filed late in nine of the last twelve months will file late again, and you can provision for it rather than discovering it. You can hold payment against filing. You can decide, at renewal, that a 2% better price from a chronic non-filer is not actually a better price.

This is the argument for keeping reconciliation history in a database rather than in a folder of spreadsheets named `Recon_Mar26_final_v3.xlsx`. The value is not in any one month's output. It is in the twelve months of it.

## What to automate first

If you are building or buying this, the sequence that pays back fastest:

1. **Normalise and match.** Deterministic scoring, tolerances on amount and date, GSTIN as the anchor. This alone removes most of the manual scrolling.
2. **Classify the exceptions.** Every unmatched line gets one of the four labels above and a suggested action. An exception list without a recommended action is just a shorter list to scroll.
3. **Carry the reversals.** Pull payables ageing for Rule 37. Compute 42/43 if you have exempt turnover. Do this before the eligible-ITC number is shown to anyone, not after.
4. **Score the suppliers.** Cheap to add once you have history, and it changes commercial decisions rather than just filing ones.
5. **Extract with AI last.** OCR and field extraction from PDFs and photos is the flashiest part and the least load-bearing. It matters when your intake is paper. It does not fix a matcher that does not tolerate `INV-42` vs `INV/0042`.

Steps 1 through 4 are ordinary engineering. Step 5 is where the model goes.

---

**[GSTBot](https://gstbot.aiknol.com)** implements this sequence for Indian SMBs: bulk invoice intake with AI extraction, deterministic GSTR-2B matching with tolerances, exception classification, Rule 37/42/43 tracking, and supplier health scoring built from filing history. It exists because the tools that do reconciliation properly are priced for enterprises, and the tools priced for small businesses mostly do filing.

Next in this series: **generating GSTR-1 and pre-filling GSTR-3B from matched data** — what can be derived safely, and what still needs a human before it goes to the portal.

*Filing rules and due dates change, sometimes retroactively. Treat the rule references here as orientation, not advice, and confirm the current position with your CA or the GST portal before you file.*
