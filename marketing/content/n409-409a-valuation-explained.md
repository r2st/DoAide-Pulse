A founder gets a 409A valuation back, sees a number, and asks the only question that matters to them: why is the common stock worth so much less than what the investors just paid?

It is a good question with a real answer, and the answer is most of what a 409A valuation is actually doing. A 409A valuation is not a price tag on your company; it is a defensible estimate of what one particular class of stock is worth to someone who cannot sell it.

This is part one of a series on **409A valuation**, written for founders and the engineers who end up building valuation systems. It covers what the rule requires, what safe harbor buys you, and what genuinely moves the number.

## What Section 409A requires

IRC Section 409A governs nonqualified deferred compensation. Stock options land inside it because an option granted with a strike price below the fair market value of the underlying stock on the grant date is, in the eyes of the code, deferred compensation.

If an option is caught by 409A, the consequences fall on the **employee**, not the company: immediate income recognition on vesting rather than exercise, an additional 20% federal penalty tax, and interest charges. You have handed someone a tax bill for stock they cannot sell.

So the compliance requirement is narrow and specific: grant at or above fair market value, and be able to defend that FMV.

## Safe harbor is about burden of proof

There is no rule that says you must hire an appraiser. What the regulations provide are **safe harbor presumptions** — methods where, if you use them, the IRS must prove your valuation was grossly unreasonable rather than you proving it was reasonable.

That inversion is the entire commercial value of a 409A report. You are not buying a number. You are buying the position that the number is presumed correct.

Three safe harbors exist in practice. The one nearly everyone uses is the **independent appraisal** safe harbor: a valuation performed by a qualified independent appraiser, as of a date no more than twelve months before the grant.

Two conditions keep it alive:

- **The twelve-month clock.** A valuation older than twelve months no longer supports a grant.
- **The material event rule.** Any event that would materially affect value invalidates the valuation *immediately*, regardless of the clock. A priced round is the obvious one. So are an acquisition offer, losing your largest customer, a major regulatory decision, or a pivot.

The second is where companies get hurt. A round closes in month four, grants keep going out against the month-zero valuation, and eight months of option grants are sitting on a strike price the company can no longer defend.

The practical rule: refresh annually, and refresh on any material event, whichever comes first.

## Why common is worth less than preferred

Back to the founder's question.

Your Series A investors paid ₹100 — or $100, the arithmetic is currency-agnostic — for preferred stock. Your 409A comes back at $25 for common. Nothing has been done to you. The two securities are genuinely not worth the same thing, and the difference is the preferences you negotiated.

Preferred stock in a typical venture deal carries:

- **A liquidation preference.** In a sale, preferred gets its money back before common gets anything. At 1x non-participating on a $10M preference, the first $10M of any exit belongs to preferred.
- **Conversion rights.** Preferred converts to common when that pays better — which is to say, preferred takes whichever of the two outcomes is higher.
- **Dividend and protective provisions**, which matter less to the arithmetic but still carry value.
- **Participation**, where negotiated, letting preferred take its money back *and* share the remainder.

Common stock is the residual. It receives what is left after every preference is satisfied. In a large exit that residual is most of the value. In a modest exit it is nothing.

So common is a claim on the upper tail of the outcome distribution, and preferred is a claim on the whole distribution with downside protection. A lower price for common is not a discount. It is a different security.

On top of that sits a **discount for lack of marketability**. Your common stock cannot be sold. There is no market, transfers need board consent, and a buyer would have to wait years for an exit. That illiquidity is worth something, and it is subtracted. DLOMs in the 20–35% range are common for early-stage private companies, derived rather than picked — typically from put-option models such as Finnerty or Chaffee, or from restricted-stock and pre-IPO studies.

The combination of preference waterfall plus DLOM is what produces the gap founders find surprising. The often-quoted "common is roughly a third of preferred" is a folk rule with no authority behind it, but it exists because the mechanics repeatedly land in that neighbourhood at early stages.

## The three approaches a 409A valuation can take

The AICPA practice aid for valuing privately-held-company equity issued as compensation frames three approaches. Every 409A report uses at least one, and says which.

**Market approach.** Value by reference to comparable transactions — either public companies trading at observable multiples, or M&A transactions in your sector. Requires selecting comparables that are genuinely comparable, which is where most of the judgement lives. A pre-revenue company has no meaningful multiple to apply.

**Income approach.** Discounted cash flow. Project free cash flows, discount at a rate reflecting the risk. Theoretically the purest method and practically the most sensitive to assumptions: a discount rate moved by two points and a terminal growth rate moved by one can move enterprise value by half. Useful for companies with real, forecastable revenue; close to fiction for a seed-stage company whose projections are a fundraising artifact.

**Asset approach.** Net asset value. Relevant for holding companies and for businesses worth less as a going concern than in liquidation. Rarely the answer for an operating startup.

For a venture-backed company that recently raised, none of these is usually the primary driver. The **backsolve** is — calibrating enterprise value so that the model reproduces the price actually paid in the round. Someone with real money just told you what the equity was worth in an arm's-length transaction; the most defensible thing a valuation can do is start there.

That is part two of this series: how the option pricing model, backsolve and PWERM allocate an enterprise value across a cap table, and why the same enterprise value produces different common prices under each.

## What actually moves your number

Ranked by how much they matter in practice:

1. **A priced round.** Dominates everything. Backsolve to the round price and the rest is allocation.
2. **Preference stack structure.** A 1x non-participating stack and a 2x participating stack on identical enterprise value produce materially different common values. This is negotiated at the term sheet, and its effect on your employees' strike price is rarely discussed there.
3. **Time to liquidity.** The expiry input in the allocation model. A company modelled two years from exit prices common higher than one modelled five years out, all else equal.
4. **Volatility.** Sourced from comparable public companies. Higher volatility fattens the tail that common has a claim on, which raises common value.
5. **DLOM.** Directly subtractive, and one of the more defensible places for reasonable people to disagree.
6. **Financial performance.** Matters, but at early stage it mostly matters through its effect on 1.

Notice what is not on the list: how much you would like the strike price to be. The temptation to push for a low 409A so options look attractive is real and it is a bad trade. A number your appraiser cannot defend is a number that fails at diligence, and it fails at exactly the moment — an acquisition, an IPO — when you can least afford a compensation issue in the data room.

## For the engineers

If you are building any of this, three things are worth knowing before you start.

**Every computed number needs a preserved original.** Analysts override model output — that is legitimate and expected. What is not acceptable is losing what the model said before the override. Store both, with who changed it and why. That audit trail is what a diligence team asks for.

**Reports are versioned documents, not renders.** A 409A report gets reviewed, revised and reissued. Treat the deliverable as a versioned object with immutable prior states, not a PDF regenerated from current data.

**The quant engine should be separable.** Black-Scholes allocation, waterfall solving and DLOM models are numerical code that must be testable in isolation against known cases. Coupling them to your web layer means you cannot regression-test a valuation, and valuations are exactly the thing you want regression tests for.

---

**[N409](https://n409.aiknol.com)** is a valuation platform built on that structure: AI extraction and normalisation of uploaded cap tables, financials and projections; comparable selection; an R-based quant engine running OPM and the income, market and asset approaches; analyst override with the computed original preserved; and versioned report drafting through to published PDF. It covers 409A alongside ASC 718/820, gift and estate, QSBS and EMI/CSOP.

*This is a technical explanation, not tax or valuation advice. 409A compliance depends on your specific facts — work with a qualified appraiser and your counsel.*
