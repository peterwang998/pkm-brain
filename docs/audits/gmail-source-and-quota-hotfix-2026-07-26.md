# Gmail source and quota hotfix — 2026-07-26

## Outcome

Brain's local-first Gmail design is working: the encrypted 90-day archive is
complete and in live incremental mode, the seven-day operational mirror is in
history-based incremental mode, and Gmail Knowledge ingestion reads local
files without calling Gmail. The observed "Gmail unavailable" experience and
the apparent API-limit warning were separate presentation and coordination
problems, not a failed bulk import.

## Evidence

- Encrypted archive: live phase, complete coverage, 8,522 stored message rows.
- Operational mirror: complete coverage and an active Gmail history cursor.
- Gmail Knowledge: local archive-to-file-to-index processing; zero Gmail API
  calls in this stage.
- Brain safety budget: 750 of 20,000 Gmail requests used on the day of the
  investigation. No retained day reached the configured 20,000-request limit.
- The Gmail archive health probe succeeded locally. Knowledge search was
  temporarily blocked while the large historical local-index backlog was being
  processed; that is independent of Gmail API quota.

## Root causes

1. Today cards used the ambiguous labels `Local evidence` and `Source`. `Source`
   opened Gmail's website, while the first action opened Brain's offline copy.
   A Gmail web page can be unavailable without the local copy being unavailable,
   and opening either link does not call the Gmail API.
2. One current operational item cited a historical mirror revision that was no
   longer retained even though the thread's latest local revision was present.
3. The archive and operational mirror each enforced Google's per-minute quota
   in a separate in-memory window. Each reader could be compliant by itself but
   their back-to-back combined burst could exceed the per-user quota window.
4. Status text did not distinguish Brain's conservative daily safety budget
   from an actual Google 403/429 quota response.

## Changes

- Today actions now say `View local copy` and `Open in Gmail`.
- Exact retained revisions remain preferred. If an exact Gmail revision is no
  longer present, Brain can show the latest local mirror revision with an
  explicit mismatch warning and both revision identifiers; it never silently
  substitutes evidence.
- The archive and mirror now share one rolling per-account quota window. Archive
  request pacing is aligned with the mirror at two requests per second.
- Scheduler results separately identify Brain safety-budget pauses and Google
  rate limiting, with bounded retry behavior and no private provider details.

## Verification

- Full Python suite: 2,793 passed.
- Swift suite: 28 passed.
- Focused Gmail, MCP, source-route, and quota suite: 72 passed.
- Ruff lint, formatting, and Git whitespace checks passed.

## Remaining architectural tradeoff

The encrypted archive and Chief-of-Staff mirror intentionally retain different
representations and still perform modest duplicate reads for changed mail. At
idle steady state they each make a small history check every ten minutes. The
next optimization, if needed, is one provider ingestion stream that atomically
fans out into both local projections. That is a larger migration and is not
required for personal-use quota safety after this hotfix.
