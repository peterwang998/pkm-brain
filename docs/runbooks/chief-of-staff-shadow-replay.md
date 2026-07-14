# Chief-of-Staff Retrospective Shadow Replay

Use the offline replay harness to measure Calendar/Gmail operational behavior before enabling a live briefing. It performs no provider calls, opens no production database, and does not invoke the live shadow runner.

## Inputs

Keep real fixtures outside git, preferably under a private experiment directory such as:

```text
~/Library/Application Support/PKM Brain Experiments/cos-shadow-eval/
```

All private inputs must be regular owner-only files (`chmod 600`):

- `operations.yaml`: the exact versioned policy used by the run;
- `timeline.yaml`: chronological normalized Calendar/Gmail records, briefing checkpoints, frozen system projections, source coverage, versions, usage, and zero-write audit counts;
- `labels.yaml`: the versioned operational evaluation fixture containing human truth;
- optional `reviews.yaml`: later `confirm|correct|missing|dismiss` review decisions and label overrides.

The timeline and labels must use the same fixture ID, classification, policy version, case IDs, source classes, and observation timestamps. Labels remain separate from predictions. A recorded projection is frozen detector/reconciler output, not truth.

The timeline schema is strict. Each checkpoint declares an `as_of` timestamp and explicit `complete|partial|unavailable` coverage. Each record points to one checkpoint and contains:

```yaml
case_id: gmail-thread-42-send-deck
checkpoint_id: 2026-07-13-morning
observed_at: "2026-07-13T07:15:00-07:00"
source: gmail
source_class: human
normalized:
  thread_id: thread-42
  subject: Synthetic example only
  messages: []
recorded_projection:
  # Full bounded projection fields validated by operational_replay.py.
  canonical_key: gmail.personal:thread-42:send-deck
  source_revision: history-100
  source_order: 100
  reconciliation_applied: true
  active_instances: 1
  item_detected: true
  item_kind: commitment
  lifecycle_state: active
  handled_verdict: needs_action
  owner: operator
  responsibility: owned
  due_at: null
  evidence_ids: [evidence:gmail:thread-42:message-7]
  sensitivity: normal
  handled_basis: direct_evidence
  authoritative_state: current
  local_route_rendered: true
  local_route_valid: true
  provider_route_rendered: true
  provider_route_valid: true
  source_identity_correct: true
  calendar_change_applied: true
  priority: high
  confidence: 0.95
```

Never put provider API payloads, credentials, refresh tokens, or attachment bytes in a replay timeline. Normalized message text is permitted only in a private local fixture and is never copied into the report.

## Run

```bash
uv run python -m pkm_brain.operational_replay \
  --policy "$HOME/Library/Application Support/PKM Brain Experiments/cos-shadow-eval/operations.yaml" \
  --timeline "$HOME/Library/Application Support/PKM Brain Experiments/cos-shadow-eval/timeline.yaml" \
  --labels "$HOME/Library/Application Support/PKM Brain Experiments/cos-shadow-eval/labels.yaml" \
  --reviews "$HOME/Library/Application Support/PKM Brain Experiments/cos-shadow-eval/reviews.yaml" \
  --report "$HOME/Library/Application Support/PKM Brain Experiments/cos-shadow-eval/report.json"
```

Omit `--reviews` before the first human-review pass. Add `--require-promotion` only for a held-out release-candidate fixture; ordinary shadow-only fixtures correctly report `promotion_passed=false` without making the command fail.

Exit status is:

- `0`: replay completed without a hard stop;
- `2`: invalid input/runtime failure or a non-averagable hard stop;
- `3`: `--require-promotion` was requested and a promotion gate failed.

The report is written atomically with mode `0600`. It contains input hashes, versions, review counts, source-coverage rates, cost counters, detection precision/recall, duplicate/stale/resurrection rates, handled-state metrics, briefing recall and false alarms, and full hard-stop/promotion status. It contains no normalized source bodies.

## Human review loop

1. Label every surfaced focus, overflow, and uncertain item.
2. Label stratified suppressed samples across human, bulk, transactional, and marketing mail.
3. Record missing items against an existing sampled source case, or add a new normalized case plus matching label.
4. Keep corrections in `reviews.yaml`; do not edit model output into ground truth.
5. Rerun and compare reports using their input hashes and version map.

For a release decision, retain immutable held-out dates and at least 30 chronological Gmail days. Calendar recurrence, cancellation, and reschedule cases remain mandatory.

## Rerunning an implementation

The command-line path deterministically replays frozen projections through the offline reconciliation/ranking implementation. Python tests or private experiment code may instead pass an object implementing `RetrospectivePipeline.process()` and `RetrospectivePipeline.brief()` to `run_retrospective_replay()`. The harness passes only normalized timeline records to that interface; it applies human reviews after prediction generation and before scoring, so the implementation cannot read its labels through the replay API.
