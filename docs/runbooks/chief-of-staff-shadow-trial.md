# Live Chief-of-Staff Shadow Trial

Use this runbook for private Calendar/Gmail evaluation in the macOS app. The trial is manual and read-only. It creates local operational state and feedback, but it cannot change Google data or add Gmail to Brain's knowledge layer.

**Readiness:** local release verification completed on 2026-07-14 with Ruff green, 746 Python tests, 27 Swift tests, and a signed local app bundle built, installed, and launched with a healthy daemon. The first live attempt exposed an oversized briefing-snapshot failure and weak terminal-result visibility. The latest app-driven validation retained source progress, completed Calendar, reported Gmail partial with 77 detector reviews deferred by the approved daily budget, persisted a bounded snapshot, and presented the terminal result in Today. The workflow is ready for owner review, but this result does not promote either connector or establish Gmail detector quality.

## What This Trial Can Access

Authorize two separate Google grants in **Ops > Connectors**:

1. **Google Calendar:** identity plus `calendar.events.owned.readonly`; the trial reads only the owned primary calendar.
2. **Gmail:** identity plus `gmail.readonly`; this grant is used only for bounded operational shadow detection.

Use the intended primary account for each card. The same email may authorize both cards, but the grants remain separate. Brain checks the exact account and exact scope set again before every run; a missing, broader, or changed-account grant stops the run.

If a Google card says **Not configured**, open **Set Up**, paste the Google OAuth client ID, and choose **Connect**. The same client ID may be entered on both cards, but Brain stores the resulting Calendar and Gmail grants separately in Keychain. The Google cards use PKCE and do not require a client secret; the sheet exposes the fixed loopback redirect URL and a **Provider Console** link if the OAuth client still needs configuration.

The approved local policy is fixed at:

- raw resumable API payloads retained for 7 days;
- normalized revision evidence retained for 30 days;
- attachments never fetched;
- quoted Gmail reply history stripped before normalized retention;
- external Calendar/Gmail writes disabled;
- bounded daily API, detector-call, and detector-token budgets.

Detector calls durably pre-reserve a conservative, no-refund input and total ceiling before launch. Gmail content is accepted only by the restricted Codex route, which enforces that same total as its per-process rollout cap and disables tools, network, session persistence, user configuration, rules, plugins, apps, MCP, hooks, memory, and subagents. Direct OpenAI, Anthropic, and Ollama selection fails closed. Provider-reported usage is logged separately; any positive call/input/total delta is added durably, while missing usage or an observed overage stops later calls and marks coverage partial.

Changed Gmail thread text is sent only to the restricted Codex detector model in a tool-less session. Brain does not retain raw detector prompts or responses by default. The detector can suggest local operational items; deterministic code validates evidence and lifecycle effects before anything reaches `ops.sqlite`.

Gmail capture, retrieval indexing, document/chunk creation, fact extraction, and wiki updates remain disabled. There is no automatic Shadow schedule. The owner—not an agent—authorizes both grants and starts every live pass.

## Run The Trial

1. Open **Ops > Connectors** and connect Calendar and Gmail separately. Confirm that each card shows the intended account and a connected read-only state.
2. Open **Today** and select **Run Shadow**. The first accepted run creates the private operations policy only if one does not already exist; an existing policy is never overwritten.
3. Leave the app open while the button shows **Running Shadow…**. Today polls the daemon automatically, so do not start another run.
4. Read the prominent terminal-result card at the top of Today:
   - **Complete:** both required sources finished with current coverage.
   - **Partial:** at least one source made progress, but pagination, a budget, retention cleanup, or another bounded failure left incomplete coverage.
   - **Failed:** neither source produced usable current coverage. The displayed error is the starting point for diagnosis.
5. Treat a partial or stale result as incomplete even when the visible focus list is empty. It is not an all-clear. Today refreshes after every terminal outcome, so completed source coverage remains reviewable even if briefing projection or snapshot persistence reports a separate failure.

The initial Calendar read is bounded to 14 days back and 90 days forward. Gmail stays inside the most recent seven days, selecting the last two days plus unread mail, and processes at most 200 threads per manual run. Remaining work is retained as visible partial coverage for the next run rather than importing historical mail or blocking the app for an unbounded pass.

The saved briefing is also a bounded preview: serialized sections target at most 240 KiB under the 256 KiB storage ceiling. Today preserves the true total, included, and omitted counts when it cannot include every audit or section card. The complete item and decision history remains in operational storage; preview truncation is never an all-clear.

## Review What It Found

Use Today as the evaluation surface:

- **Coverage** shows whether Calendar and Gmail were complete, partial, stale, or unavailable.
- **Focus**, **Urgent overflow**, **Now and next**, **Due**, **Waiting**, **Attention**, **Awareness**, and **Uncertain** show what the current projection admitted. Each operational item appears in one primary section rather than being repeated across several sections.
- **Uncertain** is not an action queue. Low-confidence, provisional, or ambiguous items remain here and do not display verified `P0`/`P1` badges.
- **Ignored & suppressed audit** is collapsed by default and shows the true total, a bounded reason/source preview, and how many entries were omitted from that preview. Review it as carefully as the surfaced items; it is where over-filtering becomes visible.
- **Local evidence** opens the retained source revision in the app. A provider link, when shown, is a separately labeled convenience and is not the evidence authority.
- **Prepare** on an upcoming Calendar item opens a bounded, read-only meeting packet using Calendar claims plus supported Brain facts/pages; suggestions are labeled and are never promoted to facts.

For each useful sample:

- choose **Looks right** when the item and next move are correct;
- choose **This is wrong** and explain a wrong title, owner, date, or interpretation;
- use **Done**, **Snooze**, **Dismiss**, or **Restore** only when that local operational action is accurate;
- use **Report Missing** for an email or Calendar obligation that should have appeared, including a short source hint.

These actions update only local operational history and evaluation records. They do not reply to mail, change labels, edit events, alter facts, or write wiki pages.

Compare the briefing with the actual Calendar and Gmail sources. Pay special attention to direct questions, promises, changed deadlines, cancellations/reschedules, travel, bills/renewals, deliveries, and other transactional items that a durable-fact filter would normally ignore.

## Rerun And Resume

After a run reaches a terminal state, selecting **Run Shadow** again starts the next bounded pass. Persisted cursors resume unfinished pagination and request only the next changes where the provider supports it. Observations, items, handled assessments, and cursor progress commit together, so a failed page does not advance past unapplied evidence.

Daily detector reservations are durable and no-refund. When Today reports reviews deferred by the approved daily budget, another same-day run may still leave those reviews deferred; that is expected and must remain visible as partial coverage. Do not delete reservations or raise the local policy budget merely to force completion. Resume after the next budget window, or make a separate explicit cost-policy decision before changing the limit.

Rerunning should reconcile newer source revisions into existing items rather than duplicate them. Use repeated manual runs to evaluate duplicate, stale, resurrection, and deadline-change behavior. A schedule is intentionally absent during shadow evaluation.

## Private Local Data And Disposal

The trial stores:

| Data | Location | Meaning |
|---|---|---|
| OAuth secrets/tokens | macOS Keychain | separate Calendar and Gmail credentials |
| policy | `~/brain/config/local/operations.yaml` | non-secret account bindings, privacy, and budgets |
| raw cache | `~/brain/cache/google-evidence/raw/` | disposable resumable API payloads, 7-day retention |
| normalized evidence | `~/brain/cache/google-evidence/normalized/` | revision-addressed retained evidence, 30-day retention |
| operational state | `~/brain/db/ops.sqlite` | items, transitions, cursors, briefings, and feedback |

Nightly maintenance automatically removes expired raw and normalized Google evidence and records the counts in its `google_evidence_retention` summary. If the cache has never been used, maintenance reports a harmless `not_configured` no-op and does not create it. A cleanup error is a visible nightly failure; cleanup never starts Calendar or Gmail source work.

To dispose of cached source payloads, first wait for Shadow to stop and quit PKM Brain, then move `~/brain/cache/google-evidence/` to Trash. This removes the disposable raw and normalized cache, not Keychain credentials or derived operational history; old evidence links will become unavailable. Disconnect each Google card in **Ops > Connectors** to revoke Brain's local credential use. Do not delete `ops.sqlite`, its WAL/SHM files, or the policy while the daemon is running. A full operational-state reset should use a coordinated backup/reset procedure rather than live file deletion.

## Stop Conditions

Stop testing, do not interpret the result as an all-clear, and preserve the displayed error when any of these occurs:

- either connector shows the wrong Google account or scopes beyond the exact approved read-only set;
- Google mail, labels, events, invitations, or RSVP state changes after a Brain run;
- Today reports complete coverage while a required source is unavailable, stale, deferred, or still paginating;
- an attachment body appears in retained evidence, or quoted history dominates normalized Gmail text;
- a direct or high-consequence obligation is hidden as handled without source evidence;
- repeated runs create duplicates, resurrect dismissed same-revision items, or leave changed/cancelled items stale;
- a provider/model budget is exceeded without a visible partial/deferred result;
- credentials, refresh tokens, or full detector prompts appear in Brain config, logs, reports, or briefing records.

Implementation and local release verification are complete for this manual trial. The latest app-driven validation persisted a bounded partial briefing with Calendar complete and Gmail visibly partial because 77 detector reviews were deferred by the approved daily budget, then left the terminal partial-result card visible in Today. Human review, continued labeled runs, and every promotion gate remain pending; partial coverage must never be interpreted as an all-clear.
