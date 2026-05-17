# Working with maintainers

cext-review-toolkit finds bugs in **other people's code**. That code was written by real people who are
giving away their work for free. This document is about how to use what the tool produces in a way
that is genuinely useful to them, not a burden.

It is the most important document in this repository. The tool's value depends entirely on whether
maintainers want to receive what you send them.

---

## TL;DR — the four rules

1. **Reach out before you file.** A short, friendly message saying *"I ran a static-analysis tool, here's
   what it found in broad strokes — would the report be useful to you, and if so how would you like to
   receive it?"* takes 5 minutes and changes everything that follows.
2. **Treat the report as a homework assignment for the maintainer.** A 50-finding report can be
   50 hours of work. Their priorities, not yours, decide what gets fixed.
3. **Don't auto-file issues. Don't auto-open PRs.** This tool can produce a lot of well-formatted
   output very quickly. Humans should decide what to send and when.
4. **Security findings need responsible disclosure**, not a public issue. If something looks like a
   memory-safety bug exploitable by attacker-controlled input, follow the project's `SECURITY.md` (or
   email the maintainers privately) before doing anything else.

If you only read the TL;DR, that's enough to be a good citizen with this tool. The rest of this
document is the longer version.

---

## The first principle: ask, don't dump

The most consequential signal in this whole pipeline is whether the maintainer **wants** what you
generated. Not whether the findings are real. Not whether the report is well-written. Whether they
want to receive it, in this volume, right now.

Open-source maintainers — especially solo maintainers and small teams — are usually working on this
in their free time. A polished 80-finding report dropped into their issue tracker without warning can
read as:

- *"Here are 80 things you've done wrong."*
- *"Here is 50 hours of homework."*
- *"I ran a tool I don't fully understand and now you have to triage its output."*

That's not what you mean, but it's how the contact lands when there was no contact before it. A
maintainer who finds a 80-issue dump in their tracker on Monday morning is most likely to:

1. Close them all without reading.
2. Burn out a little.
3. Add the tool to their mental "things that hurt me" list.

Whereas a maintainer who got a 3-paragraph email or a friendly issue first — *"I ran a static-analysis
tool, found ~80 candidate issues across ~5 categories, would you like to see the report, and if so do
you prefer one issue per category, an umbrella issue, an email, or a gist?"* — is most likely to
respond with their actual preference, including sometimes "thanks, but no, I don't have time for this
right now" (which is a valid answer; respect it).

Maintainer preferences vary widely. Some examples we've seen in practice:
- *"Please send everything as a single umbrella issue with the full report linked."*
- *"Please file one GitHub issue per finding with reproducers inline."*
- *"Please email me the report; I'll triage and file my own issues from it."*
- *"Thank you, but I'd rather work through these myself; don't open PRs, the report is enough."*
- *"I can't take on more right now; please re-ask in three months."*
- *"Use our security policy for anything memory-unsafe."*

You cannot guess which of these applies. **Ask.**

---

## Before you run this tool

A short checklist before kicking off `/cext-review-toolkit:explore`:

- [ ] **Is the project actively maintained?** Last commit, last release, open-issue response time.
      An archived or barely-maintained extension is rarely worth a full review unless you intend to
      take over maintenance yourself.
- [ ] **Read `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`** if they exist. They tell you how
      the maintainer wants to be contacted, where to file what, and what's off-limits.
- [ ] **Skim recent commits and the changelog** for fixes that may already address what you'd find.
      A finding that's already merged on `main` is not a finding.
- [ ] **Skim the open issues and PRs** for in-flight work in the same area. Build a small
      known-issues baseline first (`gh issue list`, `gh pr list`, `gh pr diff <N>`) and classify
      each finding before sharing: `DUPLICATE-FILED` (already in an open issue),
      `DUPLICATE-FIX-IN-FLIGHT` (covered by an open PR), or `NET-NEW`. cext-review-toolkit's
      `git-history-analyzer` agent helps surface the recent-fix-completeness angle.
- [ ] **Check whether there's a security policy.** Memory-safety findings, exploitable buffer overflows,
      and attacker-controlled-input parsing bugs may belong in private disclosure first.
- [ ] **Is this a project you're genuinely engaged with?** Reviewing a project you don't actually use
      or care about, then dropping the report and disappearing, is a particular kind of friction.
      Maintainers can tell.

If most of those boxes aren't checked, the right move is usually to spend ten minutes on them before
running the tool, not to skip and run anyway.

---

## Before you share findings

After the tool produces a report:

- [ ] **Pull the latest source and re-verify.** A finding that was real when you cloned can be
      gone by the time you write it up. Drop fixed-upstream findings; mark them `DONE` in your notes.
- [ ] **Triage every finding yourself.** This tool produces candidates. Some are false positives.
      Some are real but not bugs (intentional design choices). Some are real but require maintainer
      context you don't have. **Don't forward findings you haven't read.**
- [ ] **Reproduce the FIX-class items you can.** A finding with a runnable reproducer carries far
      more weight than a static-analysis verdict. The `/cext-review-toolkit:explore` reproducer pass
      generates these for you, but they need verification on your machine before you cite them.
- [ ] **Translate severities into the maintainer's vocabulary.** Our `FIX` does not always equal their
      "must-fix-this-week". Calibrate.
- [ ] **Check whether what you have is enough to ask, not enough to dump.** A short message with a
      summary and a link to the report is asking. A 200-line email with 50 inline reproducers is dumping.
- [ ] **Decide who's actually filing.** If you're using this tool on a colleague's project, the
      colleague should be the one filing the issues. If it's a public OSS project, you are. Don't
      cross those wires.

---

## How to reach out — a template

Your first contact should fit in a notification preview. Examples that have worked:

> Hi [maintainer name],
>
> I'm Daniel — I maintain [cext-review-toolkit](https://github.com/devdanzin/cext-review-toolkit), a
> Claude-Code-based static analysis plugin for Python C extensions. I ran it on `<ext>` over the
> weekend and it surfaced ~N candidate findings across ~K categories (refcount discipline, error
> paths, NULL safety, …).
>
> Would the full report be useful to you, and if so how would you like it? Some maintainers prefer
> a single umbrella issue, some prefer one issue per category, some would rather have it as an
> email or a gist. I'm happy to follow your preference. I'm also happy to drop it entirely if it's
> not a good time.
>
> If anything looks security-relevant on first read I'll route it through your security policy
> rather than the public tracker.
>
> The full report is here for reference (private gist): [link]
>
> No rush, no pressure.
>
> Daniel

Things this template does well:
- **Identifies who you are and what tool you ran.**
- **Gives shape (count + categories) without dumping content.**
- **Offers options instead of choosing for them.**
- **Preempts the security-disclosure channel.**
- **Provides an exit ramp** ("happy to drop it entirely") so a "no thanks" is socially easy.
- **Is short.** Anything longer signals "this will consume a lot of your time".

If you don't have a private channel: open a GitHub Discussion, or open one issue titled something
like *"Static analysis report — preference for delivery?"* with the same content.

If the maintainer responds, follow their preference exactly. If they don't respond after ~2-3 weeks,
one polite follow-up is fine; after that, accept silence as "no" and move on.

---

## One issue, umbrella issue, or many?

Once you have the maintainer's preference, follow it. If they didn't specify, here's our default
heuristic:

| Situation | Recommended shape |
|---|---|
| ≤ 3 findings, all FIX-class, all in one area | One issue per finding, each with reproducer |
| 5-15 findings spanning categories | An **umbrella issue** linking to one sub-issue per finding (the [h5py model](https://github.com/h5py/h5py/issues/2825)) |
| 15-50 findings | One umbrella issue + the report posted as a gist; ask the maintainer if they want sub-issues filed or if the gist is enough |
| 50+ findings | Almost always: a gist + a conversation, never a unilateral dump |
| Free-threading-specific | Often a separate FT-focused report alongside the correctness one |
| Security-class | **Private disclosure first**, never the public tracker |

The umbrella pattern works because it gives the maintainer a single subscription point, lets them
close sub-issues incrementally without triage overhead, and makes the report's overall shape visible.
It also lets external contributors pick up individual sub-issues without having to read the whole
review.

---

## Calibrating severity

The tool tags every finding `FIX`, `CONSIDER`, `POLICY`, or `ACCEPTABLE`. These are *our* labels;
maintainers may translate them differently. Some mappings we've seen:

| Our label | Maintainer interpretation (varies) |
|---|---|
| **FIX** | "Likely real bug, want a reproducer, will look at this" |
| **CONSIDER** | "Maybe yes maybe no, depends on whether it matters in practice" |
| **POLICY** | "My call, not yours; sometimes 'we considered this and chose otherwise' rather than 'good idea, will adopt'" |
| **ACCEPTABLE** | "OK that the tool noticed, no action needed" |

Don't push back on a maintainer who downgrades or dismisses one of your `FIX` findings. They have
context you don't — design constraints, performance trade-offs, downstream callers, historical
incidents. *"We considered that; here's why we left it"* is a valid response, and not a license for
you to argue.

The single biggest credibility-builder in your communication is being honest when you're uncertain.
**`SOURCE-ONLY`** findings should be marked as such; **TSan-only races that need specific scheduling**
should be flagged; **OOM-only crashes that require fault injection** should be labeled. Maintainers
who see honest uncertainty trust the rest of the report more.

---

## Security findings: responsible disclosure

If your review surfaces something that looks like:

- A buffer overflow or out-of-bounds read on attacker-controlled input
- A use-after-free reachable from public API or untrusted data
- A NULL dereference that crashes the host process from external input
- A parsing bug that breaks input-validator assumptions (the lone-surrogate
  ujson finding is an example: the C parser silently mutates the string,
  bypassing a Python-side validator)

then it goes through the project's security disclosure channel **first**, not the public issue tracker.
In order of preference:

1. The project's `SECURITY.md` instructions (private email, GitHub Security Advisories, dedicated
   address).
2. A direct private email to a maintainer if no formal policy exists.
3. A GitHub Security Advisory draft on the project's repo (if you have permissions).

What "first" means in practice: file the security finding privately, give the maintainer time to
respond and patch, then — once they're ready — follow their guidance on whether/when/how to discuss
publicly. The standard 90-day disclosure window is a reasonable default; longer is fine if they ask.

Things to avoid:

- Filing a public issue titled "buffer overflow in `read()`" with a working PoC.
- Posting the same finding to social media before the maintainer has acknowledged it.
- Sending a CVE request before the maintainer has had a chance to confirm and patch.

If the maintainer doesn't have a security process and isn't responsive, that itself is a real
problem; consult upstream guidance ([CERT/CC's coordination guide](https://vuls.cert.org/confluence/display/Wiki/Vulnerability+Disclosure+Policy),
[OSV's disclosure docs](https://google.github.io/osv.dev/data/#data-sources)) rather than improvising.

---

## PRs: offer, don't push

It is sometimes appropriate to offer a PR alongside a finding. It is rarely appropriate to open
one before asking.

**Defaults that work:**

- Mention in your initial outreach: *"I'm happy to send PRs for the smaller fixes, or you may prefer
  to handle them yourself — let me know."*
- For one-line fixes (typo, missing `Py_DECREF`, missing NULL check) where the patch is unambiguous,
  it's reasonable to offer a draft PR after the maintainer has acknowledged the finding.
- For structural changes (multi-phase init migration, abi3 conversion, GC support, FT-readiness), do
  **not** open a PR without explicit prior agreement on the approach. These are design decisions
  with multi-week implications.
- For anything that touches the project's public API, the maintainer decides the API change, not you.

**Real example of a healthy "no thanks":** the ijson maintainer replied to our review with *"Thank you
very much, honestly, but no — I'd feel better if I addressed these issues myself, in turn learning a bit
more by doing."* That is a complete, valid answer. The right response is *"absolutely, just let me
know if you want anything from me."* Not *"are you sure? I have the patches ready."*

If the maintainer accepts your PR, follow their style guide and review process. If they reject your
PR, accept it. If they want to land their own version of the fix, mention you in the commit, and not
take your patch — that's also fine; the goal is the bug being fixed, not your name being on it.

---

## After the report is shared

- **Don't ghost.** If the maintainer engages, follow up. If they ask for clarification, provide it.
  If they take your PR, watch for review comments and respond promptly.
- **Don't nag.** One follow-up after 2-3 weeks of silence is fine. A second follow-up after another
  3-4 weeks is the upper limit. After that, accept silence; they may come back to it later or not.
- **Don't litigate dismissals.** If a maintainer marks something `wontfix` or pushes back on severity,
  one round of clarification is fine; beyond that, drop it.
- **Don't summarize on social media without consent.** Posting *"I just found N bugs in $ext using my
  tool"* before the maintainer has responded is the most common burnout-inducing failure mode.
  Wait for them to be visible in the conversation, or ask them.
- **Credit the maintainer in the fix.** If a maintainer takes your finding and ships a patch,
  thanking them in the issue is normal; mentioning their work positively in any subsequent writeup
  is normal. Treating them as a passive bug receptacle is not.
- **Maintain your own reputation.** People talk. A pattern of dropping reports on maintainers who
  didn't ask for them costs you future credibility on projects that *would* welcome a review.

---

## Anti-patterns

Things we've seen go wrong (sometimes from this tool's users, sometimes adjacent):

- **Auto-filing issues from the report.** The tool produces well-formatted output. It is tempting to
  pipe the output directly into `gh issue create`. Don't. Every issue should pass through human
  triage first.
- **Auto-opening PRs from the suggested fixes.** Same problem, more disruptive.
- **Dumping a 50-finding report into a 1-maintainer repo.** Especially harmful for solo maintainers
  with day jobs. Always ask first.
- **Filing the same review in multiple places** (issue + PR + email + Discord) thinking redundancy
  helps. It just multiplies the maintainer's triage cost.
- **Filing security-class findings in the public tracker** because the security policy is annoying
  to find or use.
- **Taking offense at dismissals.** A maintainer saying "no, that's intentional" is a complete
  answer. Don't escalate.
- **Posting analysis summaries to Twitter / Mastodon / blog before the maintainer has even seen them.**
  Especially "look how many bugs $ext has" style. Even if true, it punishes the maintainer for being
  the one who exposed their work to scrutiny.
- **Writing a "name and shame" thread when a maintainer doesn't respond.** They may be sick, on
  vacation, or just out of capacity. Silence is not an invitation to escalate publicly.
- **Generating reports for projects you don't actually use.** This is the #1 sign of "tool is being
  used as a content farm rather than for genuine engagement."
- **Leaving the tool's signature in commit messages or PR descriptions verbatim.** Maintainers don't
  need to read "Co-Authored-By: cext-review-toolkit"; they need to see human judgment in your
  contribution.

---

## Examples of working well

For reference, three reviews where the engagement worked the way this document describes:

- **ijson** (Rodrigo Tobar / ICRAR): private email first, full report shared as a gist, maintainer
  declined PRs but accepted the report. Outcome: positive engagement, no churn for the maintainer,
  fixes will land on his timeline. ([discuss.python.org thread](https://discuss.python.org/t/systematically-finding-bugs-in-python-c-extensions-575-confirmed-so-far/106875),
  ["Working through reports" reply on the thread](https://discuss.python.org/t/106875/5))
- **h5py**: review surfaced ~30 findings; rather than filing 30 issues, we opened one umbrella issue
  ([#2825](https://github.com/h5py/h5py/issues/2825)) linking sub-issues, with the full report
  attached as a gist. Maintainers triaged at their own pace.
- **uvloop**: review surfaced FT-readiness gaps; private discussion first about whether the project
  was actively pursuing FT-safety (it was), TSan-confirmed reproducers shared inline, individual
  fixes proposed only for the highest-confidence items.

If you're not sure whether your engagement is on this kind of footing, the answer is probably to
slow down and reach out before sharing more.

---

## When in doubt

Ask yourself: *"If I were the maintainer of this project, and someone I'd never met sent me what I'm
about to send, would I be glad they did?"*

If yes, send it.

If you can't tell, ask first.

If no, don't send it.
