You wrote a script that finds hiring managers and emails them about your candidacy. It worked for a week. Then the replies stopped, and you assumed nobody was interested.

Nobody was reading. Somewhere around email 80, Gmail started filing you under Promotions, and somewhere around email 200 it started filing you under Spam. Your open rate did not drop because your message got worse. It dropped because your cold email deliverability did — the sender reputation that decides whether a message reaches an inbox at all.

This is a guide to not having that happen: the authentication that has to be right, the warm-up that has to be gradual, and the sending behaviour that keeps you out of the filter.

## Why job outreach is a cold email deliverability problem

Cold email from a personal domain is one of the harder deliverability cases. You have no sending history, low volume (so every signal is noisy), and content that pattern-matches to bulk mail. Mailbox providers are deciding whether you are a person writing to another person, or a script.

The good news: you *are* a person writing to another person, and if you send accordingly the filters mostly agree.

## Part 1: Authentication

Three records. All three matter, and DMARC alignment is the one people miss.

### SPF

SPF is a DNS TXT record listing who is allowed to send for your domain.

```
v=spf1 include:_spf.google.com ~all
```

`include:_spf.google.com` authorises Google's infrastructure. `~all` is a soft fail for everything else — start here, move to `-all` once you are confident nothing legitimate is sending from elsewhere.

Two things to know. SPF has a hard limit of **10 DNS lookups**; exceed it and the whole record fails, taking your authentication with it. And SPF checks the envelope sender (`Return-Path`), not the `From:` header the recipient sees, which is why SPF alone proves very little.

### DKIM

DKIM cryptographically signs outbound mail. The receiver fetches your public key from DNS and verifies the signature.

In Google Workspace: Apps → Google Workspace → Gmail → Authenticate email. Generate a 2048-bit key, publish the TXT record it gives you, then click Start authentication. The order matters — start it before the DNS has propagated and you will sign mail with a key nobody can find.

DKIM survives forwarding, which SPF does not. That alone makes it the more valuable of the two.

### DMARC

DMARC ties the other two to the address the recipient actually sees, and tells receivers what to do on failure.

```
v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.com; pct=100
```

Start at `p=none` — monitor only. Read the aggregate reports for a couple of weeks, confirm your legitimate mail is passing, then move to `p=quarantine` and eventually `p=reject`.

The concept worth understanding is **alignment**. DMARC passes when SPF or DKIM passes *and* the passing domain matches the domain in the `From:` header. This is what makes DMARC meaningful: anyone can pass SPF for their own domain while forging yours in `From:`. Alignment closes that.

The practical consequence for job outreach: send from the domain in your `From:` address. Relaying through a third-party SMTP that signs as its own domain will authenticate fine and align badly.

**This is the strongest argument for sending through your own Gmail via OAuth rather than a bulk provider.** The mail originates from Google's infrastructure, is DKIM-signed as your domain, aligns by construction, and inherits your existing account reputation. There is no alignment work to do because there is no mismatch to fix.

### Verifying

Send to a checker like `check-auth@verifier.port25.com` or Mail-Tester and read the report. You want three passes and DMARC alignment on both identifiers. If DKIM passes but DMARC fails, you have an alignment problem, not a signing problem — check which domain is in the signature.

## Part 2: Warm-up

A brand-new domain sending 200 emails on day one is indistinguishable from a spammer. Volume must ramp.

A workable curve for a personal domain:

| Days | Per day |
|---|---|
| 1–3 | 5–10 |
| 4–7 | 15–20 |
| 8–14 | 25–40 |
| 15–21 | 40–60 |
| 22+ | 60–100 |

Two caveats that matter more than the exact numbers.

**Engagement beats volume.** Twenty emails that get five replies build reputation faster than a hundred that get none. Reputation is a function of how recipients react, not how many you sent. This is why job-search outreach done properly — researched, relevant, personal — is easier to deliver than generic sales blast, if you actually do the research.

**Reciprocal warm-up pools are a liability.** Services that have accounts email each other and mark everything as important generate engagement signals that are, straightforwardly, fake. Providers have got good at spotting the pattern, and the penalty for being spotted is worse than the slow ramp you were avoiding.

If your domain has sent nothing for months, treat it as new and ramp again. Reputation decays.

## Part 3: Sending behaviour

Authentication gets you eligible for the inbox. Behaviour decides whether you land there.

**Randomise timing.** Emails at 09:00:00, 09:05:00 and 09:10:00 are a cron job. Jitter the gaps — anywhere from two to twenty minutes — and confine sending to business hours in the recipient's timezone. A 03:00 send says automation regardless of content.

**Cap hard, daily.** Google Workspace allows 2,000 external recipients per day. That is a ceiling, not a target. For personal outreach, 50–100 is plenty and stays far from the shape of bulk mail.

**Personalise past the first name.** `Hi {{first_name}}, I saw your company is hiring` is a template and reads as one. `Hi Priya, I read your post on migrating the billing service off Rails and had a question about the dual-write window` is a person. The second gets replies; replies are the reputation signal that actually matters.

**Verify addresses before sending.** Bounces are among the most damaging signals available. Keep your bounce rate under 2%, and remove hard bounces immediately and permanently.

**Stop chasing.** Two follow-ups, then stop. A third is not persistence, it is the thing that gets you marked as spam — and a spam complaint is worth many multiples of a non-reply in reputation terms. Gmail Postmaster Tools will show your complaint rate; keep it under 0.1% and treat 0.3% as the point where you have a serious problem.

**Plain text, few links.** Elaborate HTML, tracking pixels and link shorteners all correlate with bulk mail. A short plain-text email with one link — your portfolio, if relevant — is both more deliverable and, for this use case, more convincing.

**Thread your follow-ups.** Reply in the same thread rather than starting a new one. It is what a human does, and it gives the recipient context without re-reading.

## Part 4: Watch the signals

Set up **Gmail Postmaster Tools** for your domain. It is free, it takes ten minutes, and it gives you the three numbers that matter: spam complaint rate, domain reputation, and authentication pass rates. It needs some volume before it reports, which is another reason to ramp deliberately.

Track your own numbers too, and treat a decline as an incident:

- **Reply rate.** The one that matters. A well-targeted job-search campaign can see 15–30%; under 5% means targeting or message, not deliverability.
- **Bounce rate.** Over 2% and something is wrong with your list.
- **Complaint rate.** Over 0.1% and you should stop and reconsider your targeting.

If reply rate falls off a cliff while send volume is steady, pause. Send a few test emails to accounts you control at Gmail, Outlook and Yahoo, and see where they land.

## Putting it together

The compliance floor, briefly: identify yourself honestly, do not forge headers, and honour an opt-out immediately and permanently. Job-search outreach to a work address about a role is not marketing under most regimes, but "I'd rather you didn't email me" is a complete sentence and the correct response to it is to stop.

The technical stack that works:

1. SPF, DKIM and DMARC configured and **aligned**, verified with a real test.
2. Sending through your own mailbox via OAuth so alignment and reputation come for free.
3. A ramp, respected even when you are impatient.
4. Randomised timing, hard daily caps, business hours in the recipient's timezone.
5. Personalisation that required you to read something.
6. Two follow-ups, threaded, then stop.
7. Postmaster Tools, watched.

None of it is exotic. All of it is tedious, which is why it is worth automating — and why automating it badly, by sending faster, is the one change that makes everything worse.

---

**[TalentPing](https://talentping.doaide.com)** automates exactly this loop: it finds recruiter and hiring-manager contacts, drafts personalised outreach, sends from your own Gmail over OAuth so SPF/DKIM/DMARC align by construction, applies warm-up ramping and throttling with randomised timing, then classifies replies and drafts responses. The positioning is deliberate — quality-targeted outreach rather than volume, because volume is the thing that breaks deliverability.
