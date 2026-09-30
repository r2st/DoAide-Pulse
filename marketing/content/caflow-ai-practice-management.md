A CA firm is not short of software. Most practices run tally-adjacent accounting tools, a filing utility, a document folder tree, a WhatsApp group per client and a spreadsheet that tracks who owes what. What they are short of is a system that knows the state of the practice.

Ask a partner at 4pm on the 18th which clients will miss the 20th and why, and the answer requires three phone calls. That question — cheap to ask, expensive to answer — is the one worth pointing technology at, and it is where the current wave of AI tooling is most often aimed at the wrong part of the problem.

## The four bottlenecks

Across small and mid-sized Indian practices, the same four constraints recur, and none of them is the filing itself.

**Document collection.** The single largest cause of a late filing is a client who has not sent a bank statement. This is not a technology problem in origin, but it becomes one at scale: chasing forty clients for eleven document types is a coordination load no individual can hold.

**Capacity allocation.** Work arrives on statutory dates, so it arrives in spikes. Who is overloaded during the third week is knowable in advance and almost never known in advance.

**Client communication.** Every status question — "has my return been filed", "what do you still need from me" — is answerable from data the firm already has, and is instead answered by interrupting a senior.

**Billing leakage.** Work performed outside the engagement scope gets done because refusing is awkward, and gets billed at a fraction of its value because reconstructing it at year end is guesswork.

Notice that filing appears nowhere. Filing is the visible output, and it is not the constraint.

## What AI is actually good at here

**Categorising inbound documents.** A client uploads twenty files with names like `IMG_2831.jpg` and `scan0004.pdf`. Identifying which is a bank statement, which is a purchase invoice, which is a Form 16 and which is a photograph of a parking receipt is classification work that models do well and humans find tedious. Getting this right converts a folder into a checklist that can be automatically marked off — which in turn makes "what is still missing" a query instead of a conversation.

**Extraction.** Once classified, the fields matter: period covered, entity, amounts. Extraction turns a document from something a person must open into a record the system can reason about.

**Drafting client communication.** A reminder that names the specific missing documents, references the specific deadline and adapts its tone to how overdue it is, is better than a generic nudge and takes a model a second to write. A partner reviewing twenty drafted reminders is a different job from writing twenty reminders.

**Summarising a client's position.** "Where does Sharma & Co stand this month" is a synthesis across obligations, documents, tasks and payments. It is a natural fit for generation, provided the underlying facts come from the database rather than from the model's memory.

**Surfacing anomalies.** Turnover that jumped enough to change audit applicability. A client whose document lag has doubled. These are computable signals that nobody has time to look for.

## What it should not touch

**The professional judgement.** Applicability determinations, positions on contentious items, anything that goes out under a member's signature. A tool can compute a suggestion and show its inputs. The conclusion is a professional's, and the liability follows the signature, not the software.

**Unattended filing.** Same argument as anywhere else: the irreversible step keeps a human on it.

**Client-facing answers without review.** A model answering "am I liable for advance tax" directly to a client, in a firm's name, is a professional indemnity question wearing a chatbot costume.

The line is consistent: automate the handling, keep the judgement.

## The status field that changes everything

One small modelling decision does more for a practice than most features: making **blocked** a first-class status, with a reason and an owner.

Most systems track pending and done. A task sitting at "pending" tells you nothing about whether the firm is waiting on itself or on the client. Once "blocked — awaiting bank statement, client" is a state, three things become possible: the reminder can name what it is waiting for, the workload view can distinguish real capacity from apparent capacity, and the partner's 4pm question gets an answer without phone calls.

It also changes the client relationship, because the record of who was waiting on whom stops being a matter of recollection.

## Measuring the right thing

Practices that adopt tooling successfully tend to track two numbers.

**Document lag** — the days between first request and complete receipt, per client. This is the leading indicator for everything downstream, and it identifies the clients whose fee should reflect the effort they cost.

**Buffer consumed** — how much of the internal buffer before the statutory date was used. A practice consistently filing on the deadline is not on time; it is one incident away from late.

Filings-on-time is the number everyone reports and the least useful of the three, because it is 100% in every practice that has not yet had its bad month.

## Where CAFlow fits

[CAFlow](https://caflow.doaide.com) is practice management built around this ordering. A pre-loaded statutory compliance calendar with per-client derived applicability and computed due dates across GST, TDS, income tax, ROC and audit. Tasks generated from obligations, with assignment and workload views. A passwordless client portal with AI-categorised document intake. Escalating reminders keyed to what is actually blocking a filing rather than to the date alone. Service-wise billing off the same records that tracked the work.

It is aimed at firms currently running on spreadsheets, WhatsApp groups and the senior partner's memory — which is most firms, and which works until it doesn't.

Two honest notes. A practice with fifteen clients does not need this; the spreadsheet is genuinely fine and the migration is not free. And no system fixes document collection on its own — it makes the chase cheap and the accountability clear, which is most of the battle, but the client still has to send the statement.
