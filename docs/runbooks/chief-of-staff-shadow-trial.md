# Live Chief-of-Staff Shadow Trial

Use this runbook for private Calendar/Gmail evaluation in the macOS app. Evaluation is manual and read-only; after owner authorization, Gmail mailbox mirroring runs locally in the background. The trial creates local operational state and feedback, but it cannot change Google data or add Gmail to Brain's knowledge layer.

**Readiness:** the independent Gmail mirror/provider-sync tranche passed its local release gate and is installed as of 2026-07-14. Its startup job was observed to register and fail closed before a provider request because the pre-existing daily Gmail allowance was already `1200/1200`; a fresh owner-authorized bootstrap and subsequent history-only run therefore remain to be reviewed. Do not treat the prior partial detector run, scheduler registration, or budget-gate result as mailbox validation or connector promotion.

## What This Trial Can Access

Authorize two separate Google grants in **Ops > Connectors**:

1. **Google Calendar:** identity plus `calendar.events.owned.readonly`; the trial reads only the owned primary calendar.
2. **Gmail:** identity plus `gmail.readonly`; this grant is used only for bounded operational shadow detection.

Use the intended primary account for each card. The same email may authorize both cards, but the grants remain separate. Brain checks the exact account and exact scope set again before every run; a missing, broader, or changed-account grant stops the run.

If a Google card says **Not configured**, open **Set Up**, paste the Google OAuth client ID, and choose **Connect**. The same client ID may be entered on both cards, but Brain stores the resulting Calendar and Gmail grants separately in Keychain. The Google cards use PKCE and do not require a client secret; the sheet exposes the fixed loopback redirect URL and a **Provider Console** link if the OAuth client still needs configuration.

The approved local policy is fixed at:

- raw resumable API payloads retained for 7 days;
- normalized revision evidence retained for 30 days;
- one rebuildable owner-only Gmail operational mirror and durable analysis queue;
- attachment metadata may be retained, but attachment bodies/bytes are never fetched or stored;
- quoted Gmail reply history stripped before normalized retention;
- external Calendar/Gmail writes disabled;
- bounded daily API, detector-call, and detector-token budgets.

Detector calls durably pre-reserve a conservative, no-refund input and total ceiling before launch. Gmail content is accepted only by the restricted Codex route, which enforces that same total as its per-process rollout cap and disables tools, network, session persistence, user configuration, rules, plugins, apps, MCP, hooks, memory, and subagents. Direct OpenAI, Anthropic, and Ollama selection fails closed. Provider-reported usage is logged separately; any positive call/input/total delta is added durably, while missing usage or an observed overage stops later calls and marks coverage partial.

Changed Gmail thread text is read by the restricted Codex detector only from the local durable queue in a tool-less session. Luna makes no Gmail calls and Brain does not retain raw detector prompts or responses by default. The detector can suggest local operational items; deterministic code validates evidence and lifecycle effects before anything reaches `ops.sqlite`.

Gmail capture, retrieval indexing, document/chunk creation, fact extraction, and wiki updates remain disabled. The full Shadow evaluation has no automatic schedule, but the fetch-only Gmail mirror runs on startup and about every 600 seconds after a valid private policy and exact owner-approved grant exist; the scheduled job may initialize `ops.sqlite` itself. The owner—not an agent—authorizes both grants and starts each evaluation pass.

## Run The Trial

1. Open **Ops > Connectors** and connect Calendar and Gmail separately. Confirm that each card shows the intended account and a connected read-only state.
2. Open **Today** and select **Run Shadow**. The first accepted run creates the private operations policy only if absent; an existing policy is never overwritten. Once that valid policy and the exact Gmail grant exist, the separately scheduled mirror may initialize the operational store and fetch independently of later Shadow clicks.
3. Leave the app open. The daemon's `gmail_mirror_sync` job runs on its own provider lane, while **Running Shadow…** tracks Calendar refresh and local Gmail queue analysis/reconciliation. Do not start another Shadow run.
4. Read the prominent terminal-result card at the top of Today:
   - **Complete:** Calendar coverage is current, Gmail mailbox sync is current, and no eligible Gmail analysis backlog is deferred.
   - **Partial:** provider pagination/resync, Calendar work, queued Gmail analysis, a budget, retention cleanup, or another bounded failure remains incomplete.
   - **Failed:** neither source produced usable current coverage. The displayed error is the starting point for diagnosis.
5. Treat a partial or stale result as incomplete even when the visible focus list is empty. It is not an all-clear. Today refreshes after every terminal outcome, so completed source coverage remains reviewable even if briefing projection or snapshot persistence reports a separate failure.

The initial Calendar read is bounded to 14 days back and 90 days forward. Gmail's first mirror pass uses exactly `newer_than:7d -in:spam -in:trash`, then uses Gmail history changes about every 600 seconds. Each normal sync unit is capped at 200 threads; pagination and changed-thread backlog remain durable for later provider ticks. After a complete checkpoint, Brain may retry at most ten due quarantined thread IDs with durable exponential backoff; a retry-only failure leaves mailbox freshness intact and keeps analysis visibly partial. If Gmail expires the history cursor, Brain repeats the bounded seven-day full query while preserving the prior mirror. Missing rows in that query are not deletions.

The saved briefing is also a bounded preview: serialized sections target at most 240 KiB under the 256 KiB storage ceiling. Today preserves the true total, included, and omitted counts when it cannot include every audit or section card. The complete item and decision history remains in operational storage; preview truncation is never an all-clear.

## Review What It Found

Use Today as the evaluation surface:

- **Coverage** separates Calendar state, Gmail mailbox checkpoint/freshness or resync state, scheduled-sync failure/pause state, and Gmail analysis/quarantine backlog. A missing, unreadable, uninitialized, paused, failed, or backlogged mirror is not an all-clear.
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

Gmail provider progress does not wait for another Shadow run. Each background tick resumes its durable full/history checkpoint and atomically commits mirror revisions, current pointers, provider-confirmed tombstones, queue changes, and checkpoint. A failed unit advances none of them. Selecting **Run Shadow** again refreshes Calendar and drains/reconciles eligible local Gmail queue work; detector failure does not roll back the mailbox checkpoint.

Daily detector reservations are durable and no-refund. When Today reports reviews deferred by the approved daily budget, another same-day run may still leave those reviews deferred; that is expected and must remain visible as partial coverage. Do not delete reservations or raise the local policy budget merely to force completion. Resume after the next budget window, or make a separate explicit cost-policy decision before changing the limit.

Rerunning should reconcile newer source revisions into existing items rather than duplicate them. Use repeated manual evaluation runs to assess duplicate, stale, resurrection, and deadline-change behavior. Only Gmail provider mirroring is scheduled; Calendar refresh, Luna analysis, briefing evaluation, and external effects remain unscheduled/manual.

## Private Local Data And Disposal

The trial stores:

| Data | Location | Meaning |
|---|---|---|
| OAuth secrets/tokens | macOS Keychain | separate Calendar and Gmail credentials |
| policy | `~/brain/config/local/operations.yaml` | non-secret account bindings, privacy, and budgets |
| raw cache | `~/brain/cache/google-evidence/raw/` | disposable resumable API payloads, 7-day retention |
| normalized evidence | `~/brain/cache/google-evidence/normalized/` | revision-addressed retained evidence, 30-day retention |
| Gmail operational mirror | `~/brain/cache/gmail-mirror/gmail-mirror.sqlite` | rebuildable immutable revisions, current pointers, tombstones, durable triage queue, and mailbox checkpoint |
| operational state | `~/brain/db/ops.sqlite` | items, transitions, cursors, briefings, and feedback |

Nightly maintenance automatically removes expired raw and normalized Google evidence and records the counts in its `google_evidence_retention` summary. The Gmail mirror is separately inventoried as private and rebuildable; it is not subject to the 7/30-day evidence-file pruning rule. Its directory must remain mode `0700` and database/WAL/SHM owner-only. This design relies on OS volume protection and does not claim separate app-level encryption.

To dispose of Gmail source material, disconnect Gmail, quit PKM Brain, then move `~/brain/cache/gmail-mirror/` and, if desired, `~/brain/cache/google-evidence/` to Trash. The mirror is rebuildable after reauthorization; deleting it does not remove Keychain credentials or derived operational history, and old evidence links may become unavailable. Do not delete live SQLite/WAL/SHM files or the policy while the daemon is running. A full operational-state reset requires the coordinated backup/reset procedure.

## Stop Conditions

Stop testing, do not interpret the result as an all-clear, and preserve the displayed error when any of these occurs:

- either connector shows the wrong Google account or scopes beyond the exact approved read-only set;
- Google mail, labels, events, invitations, or RSVP state changes after a Brain run;
- Today reports complete coverage while a required source is unavailable, stale, deferred, or still paginating;
- mailbox freshness and analysis backlog are merged into one misleading Gmail status, or Luna makes a Gmail provider call;
- a failed provider unit advances only some of mirror revision/current/tombstone/queue/checkpoint state;
- expired-history resync removes prior threads merely because they are absent from the seven-day query;
- an attachment body/byte appears in retained evidence, or quoted history dominates normalized Gmail text;
- a direct or high-consequence obligation is hidden as handled without source evidence;
- repeated runs create duplicates, resurrect dismissed same-revision items, or leave changed/cancelled items stale;
- a provider/model budget is exceeded without a visible partial/deferred result;
- credentials, refresh tokens, or full detector prompts appear in Brain config, logs, reports, or briefing records.

The scheduled Gmail mirror implementation is release-verified and installed, but still needs fresh owner-authorized mailbox validation. Review the first bootstrap, at least one history-only tick, daemon restart/resume, and a deliberately deferred analysis backlog before judging the UX. Human review, continued labeled runs, and every promotion gate remain pending; partial coverage must never be interpreted as an all-clear.
