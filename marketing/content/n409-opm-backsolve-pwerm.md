Part one of this series ended on a claim: for a venture-backed company that recently raised, the enterprise value is not really the hard part. Someone paid a price in an arm's-length transaction and that price is your anchor.

The hard part is **allocation** — turning one enterprise value into a per-share value for each of the seven classes on your cap table, each with different preferences, each converting under different conditions.

This post covers the three methods that do that: the option pricing model, the backsolve built on top of it, and PWERM. If you are building valuation software, this is the part you implement.

## The insight underneath the option pricing model

The option pricing model rests on an observation that is obvious once stated and non-obvious before: **every class of equity in a company with a preference stack is a call option on enterprise value.**

Consider common stock in a company with a single $10M liquidation preference. If the company sells for $8M, common receives nothing. At $12M, common receives $2M. At $50M, common receives $40M.

Plot payoff against exit value and you get a hockey stick with a kink at $10M. That is precisely the payoff diagram of a call option struck at $10M. Common stock *is* a call option on the enterprise, with the preference stack as its strike.

Once you see that, Black-Scholes applies. Not by analogy — the payoff structure is genuinely identical, so the same closed-form machinery values it.

## Breakpoints: where the waterfall changes slope

Real cap tables do not have one kink. They have many, and finding them is the first half of the implementation.

A **breakpoint** is an enterprise value at which the marginal distribution of an additional dollar changes. Between breakpoints, every dollar splits the same way. At a breakpoint, someone starts or stops participating.

They arise from:

- Each liquidation preference being satisfied, in seniority order.
- Participation caps being reached, after which participating preferred stops sharing.
- Each class's **conversion indifference point** — the value above which converting to common pays more than taking the preference. Above it, that class behaves as common; below it, as debt-like preference.
- Option and warrant strikes coming into the money, which dilutes everyone above that point.

Getting seniority right matters and is easy to get wrong. Stacked preferences pay in strict order — Series C, then B, then A. *Pari passu* preferences share pro rata when the pool is short. Term sheets say which; cap-table software does not always record which; and reading the actual documents is unavoidable.

The output is a sorted list of breakpoints and, for each interval between them, the fraction of a marginal dollar each class receives.

## From breakpoints to per-class value

With breakpoints in hand, the option pricing model proceeds mechanically.

Treat each breakpoint as the strike of a call option on enterprise value. Value each with Black-Scholes, using:

- **Spot** — enterprise value.
- **Strike** — the breakpoint.
- **Volatility** — from comparable public companies, typically 50–80% annualised for early-stage technology.
- **Time to expiry** — expected time to a liquidity event, usually 2–5 years.
- **Risk-free rate** — a government yield at matching tenor.

The value of the tranche *between* two adjacent breakpoints is the difference between the two call values — a call spread. Allocate each tranche's value across the classes participating in it, in their marginal proportions. Sum per class, divide by shares outstanding, apply DLOM.

Three properties are worth checking in any implementation:

- **The allocations sum to enterprise value.** The lowest breakpoint is at zero, and a call struck at zero is worth spot. If your tranches do not total spot, you have a bug — usually a missed breakpoint.
- **Volatility is not a minor input.** It controls how much probability mass sits in the tail that common has a claim on. Moving it 60% → 70% can move common value 15–20%. It deserves documented sourcing.
- **Time to expiry cuts both ways.** Longer horizons give the option more time to finish in the money, raising common value, while the discount reduces it. The net effect is model-specific and worth testing rather than assuming.

## The backsolve: calibrating to reality

Everything above needs a spot price. Where does enterprise value come from?

The **backsolve** — formally, the OPM backsolve method — inverts the question. Instead of estimating enterprise value and deriving the round price, take the round price as given and solve for the enterprise value that reproduces it.

A recent Series B priced at $4.20 per preferred share is not an estimate. It is a transaction. So:

1. Build the full breakpoint structure including the new round.
2. Guess an enterprise value.
3. Run OPM. Read off the implied value of the new preferred class.
4. Compare to the actual price paid.
5. Adjust and repeat until they agree.

Step 5 is a one-dimensional root find. The function is monotonic and smooth — implied preferred value rises with enterprise value — so bisection converges reliably and Brent's method converges quickly. Guard the bracket: an implausible upper bound and a badly conditioned cap table can send a naive Newton iteration somewhere silly.

Once solved, that enterprise value feeds the allocation for every other class, common included.

This is the most defensible method available for a recently-funded company, and for a simple reason: it starts from a price someone actually paid, rather than from a projection someone actually wrote.

Its limits are real. It assumes the round was arm's-length — a strategic investor paying for a commercial relationship, or a materially non-pro-rata insider round, breaks that. And it decays. Six months on, with the business materially changed, the backsolve anchor is stale and the market or income approach has to carry more weight.

## PWERM: when the outcomes are genuinely different

The option pricing model assumes a single continuous distribution of enterprise values. That assumption is fine for a company on an uncertain but continuous trajectory. It is wrong for a company facing a small number of discrete, qualitatively different futures.

A biotech at a phase-three readout does not have a lognormal outcome distribution. It has two: approval, and not.

The **probability-weighted expected return method** models these directly. Enumerate scenarios — IPO, strategic acquisition, secondary sale, dissolution. For each, specify a value, a date and a probability. Run the waterfall in each scenario to get per-class proceeds. Discount to present at a risk-adjusted rate. Weight by probability. Sum.

PWERM's strength is that it captures structure OPM smooths away — an acquisition at $80M in eighteen months and an IPO at $400M in four years are different events, not two draws from one distribution, and the preference stack treats them very differently.

Its weakness is that it is only as good as the probabilities, and the probabilities are judgement. A 40%/60% split is a defensible assumption; it is not an observation. PWERM makes the sensitivity explicit, which is honest, and it makes the number arguable, which is uncomfortable.

There is also a **hybrid** in common use: PWERM at the top level for genuinely discrete branches, with OPM allocating within each branch where the outcome inside that branch is continuous. Most methodologically careful reports for later-stage companies with a plausible near-term exit end up here.

## Choosing, in practice

| Situation | Primary method |
|---|---|
| Priced round in the last few months | Backsolve |
| No recent round, continuous uncertainty | OPM with market/income enterprise value |
| Small number of discrete, dissimilar outcomes | PWERM |
| Near-term exit visible, continuous within branches | Hybrid PWERM/OPM |
| Holding company, or worth more dead than alive | Asset approach |

The choice is a disclosure item, not an internal detail. A report that does not say which method it used and why is a report that will not survive review.

## Implementation notes

Things that are obvious in retrospect and expensive to learn:

**Breakpoint discovery is where the bugs live, not Black-Scholes.** Black-Scholes is four lines and you can test it against published values. Enumerating breakpoints across stacked and pari passu preferences, participation caps, conversion indifference and option strikes is intricate, and every cap table finds a new corner of it. Build it as a pure function from cap table to breakpoint list, and test it against hand-worked examples.

**Test the invariant, not just the outputs.** Allocations summing to enterprise value catches a large fraction of real defects, and it holds for every cap table, so you can assert it on generated inputs as well as fixtures.

**Keep the numerics out of the web layer.** Allocation is numerical code with strong testability requirements. Run it as a separate, versioned engine — R and Python both have the libraries and the statistical credibility — and let the application call it. You want to be able to re-run a two-year-old valuation with the engine version it originally used.

**Store inputs, not just results.** Volatility, expiry, risk-free rate, DLOM basis, comparable set and the breakpoint structure are all part of the answer. A report that shows a per-share value without them cannot be defended in diligence, and neither can a database that did not keep them.

---

**[N409](https://n409.doaide.com)** implements this stack: an R-based quant engine running Black-Scholes OPM, backsolve and the income, market and asset approaches, with AI-assisted extraction of cap tables and financials feeding it, analyst override with the computed original preserved for audit, and versioned reports published as PDFs.

Part one of this series covers what Section 409A requires, what safe harbor buys you, and why common stock is worth less than preferred.

*Technical explanation, not valuation advice. Method selection for a real 409A is a matter of professional judgement.*
