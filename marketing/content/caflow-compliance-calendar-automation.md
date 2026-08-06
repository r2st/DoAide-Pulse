Ask a practising Chartered Accountant where the CA compliance calendar lives and you will get one of three answers: a spreadsheet, a WhatsApp group, or a diary. Often all three, disagreeing with each other.

It works, in the sense that most things get filed. It works by consuming the partner's memory as a database, and it fails in a specific way: not on the big client with the obvious deadline, but on the small client nobody thought about until the 19th.

This is a guide to modelling the Indian statutory compliance calendar properly — as data with rules, rather than as recurring reminders — for CAs who want to stop holding it in their heads and for developers building practice tooling.

## Why a CA compliance calendar is not a set of reminders

The instinct is to put deadlines in a calendar app. It breaks quickly, for reasons that are structural rather than cosmetic.

**Obligations are per-client, not per-firm.** GSTR-1 is the 11th for a monthly filer and the 13th of the quarter-following month under QRMP. A CA with 200 clients has some of each. One firm-wide reminder is wrong for most of them.

**Applicability is derived, not declared.** Whether a client owes a tax audit depends on turnover against the Section 44AB thresholds. Whether they file ROC returns depends on being a company. Whether TDS returns are due depends on whether they deducted. These are computable from client attributes, and computing them is how you stop missing the obligation nobody remembered to add.

**Dates move.** Extensions are routine in Indian compliance — sometimes days before the original date, sometimes after it. A hardcoded date is a date that will be wrong, and a system that cannot absorb an extension without editing 200 entries will not be updated.

**A deadline is a workflow, not a moment.** "GSTR-3B due 20th" is the end of a chain: collect documents, reconcile, compute liability, get client approval, pay, file, confirm. The date is when the chain must have finished. Managing the date without managing the chain is how a filing is discovered to be blocked on a missing bank statement at 6pm on the due date.

## The data model that works

Four entities carry it.

**Compliance type** — the obligation in the abstract. GSTR-1, GSTR-3B, TDS payment, TDS return, ITR, tax audit report, AOC-4, MGT-7. Each has a periodicity (monthly, quarterly, annual), a due-date rule, and an applicability rule expressed against client attributes.

**Client profile** — the attributes the applicability rules read. Entity type, GST registration and filing frequency, turnover band, TAN registration, audit applicability, financial year end. This is the input that makes obligations derivable.

**Obligation instance** — one compliance type, one client, one period, with a computed due date and a status. This is the row that appears on a calendar. Generated, not typed.

**Task** — the work. Assignee, checklist, required documents, current state. Hangs off the obligation instance, usually several per instance.

The instance layer is what makes the whole thing tractable. Generate instances ahead — a rolling window of a few months, or a full year — and everything downstream becomes a query. "What is overdue" is a filter. "What is Ramesh's workload next week" is a group-by. "Which clients are blocked on documents" is a join. None of it requires anybody to remember anything.

## Due-date rules

Express them as rules, not constants.

Typical shapes in Indian compliance:

- **Fixed day of the following month.** TDS payment by the 7th; GSTR-1 by the 11th; GSTR-3B by the 20th for monthly filers.
- **Fixed day after quarter end.** TDS returns, generally the last day of the month following the quarter, with the Q4 return running to a later date.
- **QRMP variants.** Quarterly GSTR-1 by the 13th of the month following the quarter, with monthly payment challans in between.
- **Fixed calendar date, annual.** ITR due dates differ for audit and non-audit cases; the tax audit report has its own.
- **Relative to an event.** ROC filings key off the AGM date, not the calendar — AOC-4 within 30 days of the AGM, MGT-7 within 60. These need the event recorded before the deadline can exist.

Three adjustments apply on top of every rule.

**Holiday and weekend shifting.** Where a statute allows the next working day, encode it, with a holiday calendar that includes state holidays — a firm with clients in three states has three holiday sets.

**Extensions as overrides.** An extension is a dated override on the instance, carrying its authority (the notification number) and leaving the original date visible. Never edit the rule; a rule edited for one extension quietly changes history.

**Internal buffers.** The date your team must finish is not the statutory date. A three-working-day internal buffer, configurable per compliance type, is the difference between a calm practice and a monthly emergency.

I am deliberately not publishing a full table of current due dates. They change often enough that a table in a blog post is a liability — build the rules, and source the dates from the notifications.

## Status that means something

Four colours is the standard and it is nearly right:

- **Upcoming** — not yet in the working window.
- **Due soon** — inside the window, work should have started.
- **Overdue** — past date, not filed. Escalate.
- **Filed** — done, with acknowledgement number and date stored.

Add a fifth, and it is the most useful one: **blocked**. Work started, cannot proceed, waiting on something — usually a client document. Blocked is a different management problem from due-soon. Due-soon needs staff time. Blocked needs a phone call, and no amount of staff time will fix it.

Track *why* it is blocked, and the reminder to the client writes itself.

## Document collection is the actual bottleneck

Every CA firm I have talked to describes the same failure mode: the filing is not late because the work is hard, it is late because the client sent the bank statement on the 19th.

The pattern that works has three parts.

**A checklist per obligation type.** GSTR-3B needs sales register, purchase register, and GSTR-2B. An ITR for a salaried individual needs Form 16, interest certificates, and capital-gains statements. Derive the checklist from the obligation, and you get "what is missing" as a computed field rather than a memory.

**A client-side intake that is not WhatsApp.** WhatsApp fails on retrieval, not on delivery — the document arrived, and finding it two weeks later in a group with 400 messages is the problem. A per-client portal with magic-link access (no password, because a password is a support ticket waiting to happen) and upload against a named checklist item solves retrieval.

**Escalating reminders tied to the blocked reason.** A reminder that says "please send documents" is ignorable. One that says "still needed for your July GST filing due the 20th: purchase register" is specific, and specificity is what gets a response.

Document categorisation is a good place for a language model, and a small one is enough. Classifying an upload as bank statement, sales invoice, Form 16 or challan, and pulling out a PAN or GSTIN to attach it to the right client and period, is well within free-tier models. Nothing here needs a frontier model, and the cost per document should round to zero.

## What this changes

The measurable effects, in rough order of size:

**Nothing is missed for lack of visibility.** Derived applicability means an obligation exists because the client's attributes imply it, not because someone remembered to add it.

**Work levels out.** With instances generated a quarter ahead, the 15th-to-20th crunch is visible in advance and can be started early. Most of the panic is schedulable.

**Client chasing becomes systematic.** Blocked-with-reason plus automated escalation replaces the partner remembering, on a Sunday, that Sharma & Sons still has not sent anything.

**Billing stops leaking.** Every obligation instance is billable work. A system that records the work also records what to invoice for, which is how the small extra jobs — the ones done as a favour and never billed — stop being free.

**The practice becomes transferable.** A firm whose compliance calendar lives in the senior partner's head cannot delegate, cannot scale, and cannot be sold. One that lives in a system can do all three.

---

**[CAFlow](https://caflow.aiknol.com)** is built on exactly this model: a pre-loaded Indian statutory compliance calendar with per-client derived applicability and computed due dates, tasks generated from obligations with assignment and workload views, a passwordless client portal with AI-categorised document intake, escalating reminders keyed to what is actually blocking a filing, and service-wise billing off the same records.

It is FastAPI, PostgreSQL and Celery under a React front end, with the compliance-type seed applied at migration time so the calendar exists before anything else runs.

*Due dates and thresholds in Indian compliance change frequently, including retroactively. Build the rules; source the dates from current notifications.*
