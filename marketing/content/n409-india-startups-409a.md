Most Indian founders meet Section 409A the same way: a US investor asks for the company's "current 409A" during diligence, and nobody in the room is certain whether the company is supposed to have one.

The honest answer is that it depends on a single structural fact, and a lot of advice on the subject gets it wrong in both directions. Some founders are told every startup needs a 409A. Others are told it is a purely American problem that Indian companies can ignore. Both are wrong often enough to be expensive.

## What a 409A actually binds

Section 409A of the US Internal Revenue Code governs deferred compensation for **US taxpayers**. Stock options land inside it because an option granted below the fair market value of the underlying stock is, in the eyes of the IRS, deferred compensation with the discount as the deferral.

Two consequences follow, and the second is the one that matters commercially.

First, the obligation attaches to the *option holder*, not the company. Where a grant is found to have been priced below FMV, the holder faces income inclusion as the option vests rather than when it is exercised, an additional 20% federal tax on top of ordinary income tax, and an interest charge computed as though the tax had been owed from the vesting date. Your employee gets the bill for your valuation.

Second, because the exposure sits with employees, it becomes a diligence item. An acquirer or a lead investor reviewing option grants priced against no defensible valuation is looking at a contingent liability spread across your cap table, and the standard remedy is an escrow or a purchase-price adjustment.

## The trigger for an Indian company

The question is not where your engineers sit. It is whether the entity granting options is a US entity, and whether any recipient is a US taxpayer.

You need a 409A when:

- **You have flipped, or are flipping, to a Delaware C-corp.** This is the common path for Indian startups raising from US funds, and the moment the Delaware entity becomes the one issuing options, its common stock needs a defensible FMV.
- **You grant options to US-based employees or contractors.** A US taxpayer holding options in your company is inside 409A regardless of where the company is incorporated. Founders regularly discover this after hiring their first US salesperson.
- **You are pre-flip but planning one.** Grants made before a flip get exchanged into the new entity's options, and the exchange is scrutinised. A clean valuation history makes that conversion routine; an absent one makes it a negotiation.

You do **not** need a 409A when you are an Indian private limited company granting ESOPs to Indian employees, with no US entity and no US grantees. That case is governed by Indian law, not the IRC — and this is the part most cross-border content skips.

## The Indian valuation you do need

An Indian company issuing ESOPs has its own valuation requirements, and satisfying them does not satisfy 409A, nor the reverse.

The perquisite value on which an Indian employee is taxed at exercise is based on the fair market value of the share, and for unlisted companies that FMV is determined by a Category I merchant banker registered with SEBI. Separately, share issuance and transfer pricing questions engage the Rule 11UA valuation machinery under the Income Tax Rules, and the Companies Act brings in registered valuers for certain issuances.

These are different exercises with different standards, different qualified-provider definitions, and different report formats. A company that has flipped often needs both: a 409A for the Delaware entity's option grants, and Indian valuations for whatever remains at the Indian subsidiary. Treating one report as covering both is the single most common structural error we see.

The dates, thresholds and qualifying-provider rules on the Indian side change more often than a blog post can track. Confirm the current position with your CA rather than with anything you read here — including this.

## What "professional" buys you

The regulation offers a presumption of reasonableness. Where the value is established by an independent appraisal meeting the requirements, and the grant is made within twelve months of that appraisal with no intervening material event, the burden shifts: the IRS must show the valuation was grossly unreasonable rather than the company having to prove it was right.

That burden shift is the entire product. It is not a certificate that your number is correct. It is a change in who has to argue.

This is also why the cheapest possible valuation is frequently the worst purchase. A report that a diligence lawyer can dismantle — stale comparables, an allocation method that does not match the cap table, no documented rationale for the marketability discount — provides the presumption on paper and loses it in practice.

## What actually moves the number

For founders trying to understand why the number came back where it did, six inputs carry most of the weight:

- **The preferred price from your last round**, which anchors enterprise value but is emphatically not the common stock price.
- **The preference stack**, because liquidation preferences and participation rights are paid before common sees anything.
- **The allocation method** — an option pricing model, a probability-weighted expected return model, or a hybrid — which determines how enterprise value is split across share classes.
- **Time to a liquidity event**, which drives the option-pricing volatility term.
- **The marketability discount**, reflecting that nobody can sell your common stock next week.
- **Recent secondary transactions**, which are the hardest input to argue away when they exist.

The gap between preferred and common is the thing founders find counterintuitive and diligence finds unremarkable. Common stock priced at a small fraction of the preferred is normal early on and compresses as the company matures and the preference stack shrinks in relative terms.

## Cadence

Refresh every twelve months, and refresh early on a material event: a priced round, a term sheet, a significant secondary, a change in forecast large enough that you would describe the company differently. Granting options against a valuation that predates a round you have already closed is how a clean process becomes an unclean one.

## Where N409 fits

[N409](https://n409.aiknol.com) is a valuation platform covering IRC 409A alongside ASC 718/820, gift, QSBS and EMI/CSOP work. Uploaded cap tables, financials and projections are extracted and normalised, comparables are selected, and an R-based quant engine runs the allocation — Black-Scholes OPM, income, market and asset approaches. Analysts review and override any computed figure, with the original preserved for audit, and the report is drafted, versioned and published as a PDF. Firms delivering valuations to startup clients can run it under their own branding.

It is built for the people producing valuations. If you are a founder who needs one, what you need is a provider — this is what a good one runs on.

And if you are an Indian company with no US entity and no US grantees, you do not need a 409A at all. Getting that answer right, early, is worth more than any report.
