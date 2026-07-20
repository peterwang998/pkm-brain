# Gmail Brain v2 Evaluation — 2026-07-17

**Status:** isolated corpus and filtering evaluation complete; fact-quality and temporal-recall acceptance not met
**Environment:** `/Users/Peter/brain-v2`; the live Brain was not modified
**Models:** external Codex CLI only — `gpt-5.6-luna` low for extraction, `gpt-5.6-luna` medium for critic and route resolution, and `gpt-5.6-sol` medium for the final sampled judge

This report is deliberately aggregate-only. It contains no mailbox title, body, address, provider identifier, credential, access code, or other message-derived secret.

## Verdict

The v2 Gmail projection now matches the original Brain's aggregate admission rate. Projection v6/classifier v5 admitted 356 of 6,960 active threads to fact extraction (5.11%), close to the original Gmail benchmark's 4.8%, while marking 6,594 threads (94.74%) for default retrieval suppression. No eligible document was also classified for default suppression. This does not establish equivalent composition or recall: the earlier classifier-v4 title-only review found no systemic advertising leak and no systemic routine-mail false negative, but the small v5 delta was not separately title-reviewed.

The fact path is not ready for production promotion. In the fresh v6 15-document pilot, 42 candidates produced 12 applied actions, 11 review holds, and 19 critic rejections, but retained zero structured event times and emitted nine temporal warnings. The final `gpt-5.6-sol` audit judged only 8 of 12 applied facts acceptable. All four bad actions had no retained structured `event_time` and used `event_page_occurrence_compatible`: an existing event page was allowed to lend its occurrence to evidence that had not survived Gmail's stricter temporal repair. The complete run was rolled back. The post-run guard now forbids that inference, but it has not received another external corpus audit; high admission precision has still not translated into acceptable temporal recall.

Chief-of-Staff workflows should not use Brain facts as their operational state machine. Brain should remain their durable memory and evidence source; current meeting, draft, reminder, approval, and delivery state belongs in the separate operational substrate.

## Corpus And Index Integrity

The immutable projection-v6/classifier-v5 run discovered and captured 7,125 current revisions. Its current projection contained:

| Measure | Result |
| --- | ---: |
| Active Gmail documents | 6,960 |
| Unique superseded revision paths retained as evidence | 35,408 |
| Physical superseded document rows (two origin copies per path) | 70,816 |
| Provider-deleted documents | 165 |
| Active, retrievable Gmail chunks | 15,953 |
| Current projection chunks including deletion tombstones | 16,118 |
| Eligible for fact extraction | 356 (5.11%) |
| Marked for default retrieval suppression | 6,594 (94.74%) |
| Capture, embedding, or vector errors | 0 |
| Missing or stale vectors | 0 |

The complete isolated index contained 27,910 matching current SQLite and Lance rows with `BAAI/bge-small-en-v1.5` 384-dimensional embeddings. SQLite/vector consistency passed with zero missing or stale vectors. The only index-doctor recommendation was compaction of accumulated immutable projection versions and Lance data files; it was not a consistency fault.

The port exposed a node-identity portability defect: prior documents used a machine hostname as origin, so the first v6 ingestion created a second inactive copy of 35,408 existing revision rows. Every duplicate path had the same content hash, none remained active, and retrieval indexes contain only the current projection. Gmail ingestion now uses the stable `gmail-knowledge` origin and migrates a matching legacy path instead of duplicating it. The inactive exact duplicates remain retained pending a citation-aware cleanup rather than being deleted speculatively.

Eligibility became progressively narrower as the evaluation exposed over-admission: 34.1% in the initial v2 policy, 6.7% after the first source gate, 5.08% in classifier v4, and 5.11% in classifier v5 against the slightly newer archive. The final rate is aligned with the original Brain benchmark's 4.8% while retaining a separate lane for important temporal mail.

### Final classifier distribution

| Delivery / importance / actionability | Active | Fact eligible |
| --- | ---: | ---: |
| Bulk / routine / informational | 2,735 | 0 |
| Transactional / routine / informational | 1,597 | 0 |
| Unknown / routine / informational | 1,440 | 0 |
| Bulk / advertising / promotional | 743 | 0 |
| Transactional / important-temporal / time-sensitive | 223 | 223 |
| Mixed / durable-candidate / informational | 73 | 69 |
| Transactional / advertising / promotional | 60 | 0 |
| Transactional / important-temporal / action-required | 33 | 33 |
| Human / durable-candidate / informational | 22 | 16 |
| Unknown / advertising / promotional | 19 | 0 |
| Mixed / durable-candidate / action-required | 6 | 6 |
| Mixed / important-temporal / action-required | 6 | 6 |
| Mixed / important-temporal / time-sensitive | 3 | 3 |

All 265 important-temporal candidates were eligible. Of 101 durable candidates, 91 were eligible. These are source-admission results, not proof that downstream extraction produced a correct fact or structured time.

Generic retrieval now suppresses advertising, bulk, and routine mail. A generic query containing words such as “email,” “mail,” “Gmail,” or “inbox” does not reveal advertising; newsletters or promotions require an explicit request. Explicit mailbox search may include routine transactional or unknown mail without making it generally salient. The bounded retrieval fanout was increased so this suppression does not starve legitimate explicit mailbox recall.

## Private Manual Review

A content-minimized classifier-v4 review examined 75 titles in four policy-conditioned samples without copying any title into this report:

- 20 of 20 important-temporal eligible samples were genuinely event- or obligation-like. This measures precision among admitted candidates, not recall over all temporal mail.
- Of 15 durable eligible samples, 14 appeared plausibly durable and one was an obvious false positive.
- All 15 advertising exclusions were clearly promotional.
- The 25 routine exclusions were mostly routine. One plausible relevance edge and one security/update edge merit future labeled review, but neither exposed a systemic false-negative pattern.

This is encouraging evidence for the source gate, not a substitute for a blinded, randomly sampled precision/recall set. The v5 change isolates a qualifying transactional-temporal message from a separate promotional message in the same thread and has synthetic regression coverage, but its refreshed corpus was not independently title-reviewed before this report closed.

## Identity-Derived Importance Policy

The following short profile was derived from durable Brain material, not from mailbox volume or the Gmail pilot:

> Peter is a staff-level product leader focused on metadata, lineage, governance, and intelligence, with a growing emphasis on AI agents, agentic workflows, and production LLM/inference infrastructure. He favors zero-to-one and growth environments with broad product, go-to-market, and business scope. Career processes, substantive meetings, direct commitments, project obligations, and deadlines are high-salience; compensation matters but is not the sole or primary ranking objective.

That profile should seed an explicit, inspectable ranking policy:

1. Rank direct commitments, imminent meetings, interviews, deadlines, requested decisions, and material changes to active projects highest.
2. Elevate substantive work in the operator's product domains and stated AI-agent direction, plus concrete career-process steps and follow-ups.
3. Preserve durable relationship, project, and organizational context when it could improve a future decision or meeting.
4. Suppress promotions, newsletters, routine receipts, status notifications, and low-consequence logistics unless a direct obligation or configured legal, financial, security, safety, or travel exception applies.

The profile is a draft preference model, not authority. The Chief of Staff must record the policy version and ranking reasons, allow correction, and never infer importance merely from sender frequency or traffic volume.

## Fact And Temporal Pilots

Four pilots used 15 selected Gmail documents through the external Codex path. Every run was fully rolled back from a verified pre-run backup after its audit. The first three used classifier v4; the fresh acceptance run used the rebuilt v6/v5 corpus. The figures below are retained as redacted evaluation evidence only.

| Pilot | Raw output / admitted candidates | Applied | Needs human | Critic rejected | Sol result | Main finding |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Initial | 98 / 81 | 60 | — | 21 | 37 acceptable, 23 bad | Event occurrences were attached to incompatible event pages; five bad judgments also involved privacy-boundary failures. |
| First hardening | — / 36 | 20 | 5 | 11 | 13 acceptable, 7 bad | Five remaining cross-occurrence routes, one overgeneralized source claim, and one person/page identity mismatch. |
| V4 hardening | 64 / 42 | 13 | 11 | 18 | 12 acceptable, 1 bad | Unsafe routes mostly failed closed and structured temporal recall fell to zero; one event/date route-evidence incompatibility still passed. |
| Fresh v6 acceptance | 54 / 42 | 12 | 11 | 19 | 8 acceptable, 4 bad | Zero structured times; all four bad facts used an existing event page as occurrence evidence after Gmail temporal repair had retained no event time. |

In the fresh v6 run, 54 raw outputs yielded 42 candidates; validation counted ten direct rejections and twelve total rejections across retry accounting. Route resolution held 14 candidates rather than inventing or borrowing an occurrence identity. A content-free scan of all 42 persisted action payloads found zero recognized sensitive-value leaks; three actions contained expected masking markers. The result still cannot be accepted: one-third of the applied facts failed the independent judge and event-time recall was zero.

Recorded v4 telemetry was 32 external `gpt-5.6-luna` low extractor calls, 64 `gpt-5.6-luna` medium critic calls, one `gpt-5.6-luna` medium resolver call, and two batched `gpt-5.6-sol` medium auditor calls. The fresh v6 run used the same configured roles and four critic workers. Every new call used the external Codex provider and ephemeral one-shot execution.

### Temporal assessment

The current temporal path has useful structural safeguards:

- `observed_at` comes from the trusted Gmail internal timestamp and rendered byte-range index, never a sender-authored date header or ingestion time.
- A fact targeting an event page needs one grounded occurrence compatible with that page. Ambiguous, conflicting, or unresolved occurrences go to L3 review. After the v6 audit, an existing page's date is no longer sufficient for an undated fact.
- A deterministic Gmail repair accepts only a literal, cited full-year date. Exact time requires a literal ISO timestamp with an offset or `Z`; otherwise the result is day precision. Inclusive end dates are normalized to next-day exclusive ends.
- A message timestamp never proves that a named event occurred, and a date mention without a grounded event phrase cannot become an occurrence.

These constraints stopped several earlier cross-occurrence paths, but they are too brittle for ordinary email prose. None of the fresh v6 run's 42 candidates retained a structured event time, even though the source gate intentionally selected important-temporal material, and nine temporal warnings remained. The architecture is structurally safer and empirically low-recall. It should stay isolated until a broader labeled set demonstrates both event-identity precision and useful recall for natural-language dates, ranges, timezones, cancellations, and reschedules.

The four fresh Sol failures showed the deeper version of the route problem: even a real existing event page cannot substitute for occurrence evidence in the current source. The post-run repair now makes Gmail routing consume only a structured `event_time` that survived Gmail temporal validation, or an already applied, critic-agreed same-source occurrence anchor. It cannot recreate a rejected time from looser regexes over model-authored statements. This closes the observed deterministic path in tests, but it does not repair the zero-recall temporal parser and has not yet passed another external Gmail audit.

## Privacy Incident, Rollback, And Boundary

The initial pilot exposed secret-like message values in generated evidence and in non-ephemeral external Codex session history. Its derived facts, actions, audits, and wiki changes were fully restored from a verified pre-run backup. Every later pilot was also rolled back after its quality audit. The encrypted Gmail archive and immutable source/chunk corpus remain intentionally intact as the authorized local evidence set.

The remediation now applies a length-preserving Gmail secret sanitizer before extractor, retry, critic, evidence-repair, auditor, MCP, and Gmail output boundaries; masks cached evidence while preserving offsets; propagates recognized exact sensitive values; and rejects a sensitive value presented as a fact. Coverage includes common prefix/postfix authentication-code wording and auth-scoped path, query, and fragment tokens with negative controls for ordinary years, budget codes, help links, and non-secret slugs. Gmail-derived text is explicitly untrusted in extractor, critic, auditor, direct MCP, proxy MCP, and generic Brain retrieval surfaces. Later external invocations use `codex exec --ephemeral`, so new one-shot runs do not persist local Codex session transcripts. A scan of the fresh v6 pilot found zero recognized leaks across all 42 action payloads; three payloads exercised masking. This is pattern-based defense, not a claim that arbitrary private text has become non-sensitive; normalized mail still crosses an external-model privacy boundary when that path is explicitly enabled.

On 2026-07-19, after explicit owner authorization, all 229 pre-remediation one-shot sessions were removed through Codex's supported permanent-delete command; 182 had been flagged by the content-free inventory as containing recognized Gmail-sensitive values. The frozen UUID manifest matched its preflight count and SHA-256 fingerprint before deletion. Post-cleanup verification found zero target rollout files and zero target Codex state rows, retained the primary working thread and three neighboring subagent sessions, and returned `ok` from the Codex state database's SQLite `quick_check`. Brain's captured agent-log inbox/raw stores contained only those three excluded subagent sessions from the window and no target pilot transcript. This is supported logical deletion; filesystem snapshots or external backups, if any, are outside this verification.

## Chief-Of-Staff Substrate Recommendation

Brain should be the Chief of Staff's memory, not its nervous system. Keep one app, daemon, and private home, but retain separate physical stores and lifecycle contracts:

- **Meeting preparation:** read current Calendar/Gmail operational observations from `ops.sqlite` and durable context from Brain. Cache a packet only with a dependency manifest/hash over every source observation, Brain revision, query/index configuration, policy, and composer version; invalidate it when any dependency changes.
- **Proactive drafting:** keep unsent content in dedicated `ops_drafts` and append-only draft versions with reply-target revision, provenance, staleness, approval, send verification, and receipt state. An unsent draft is not a Brain fact.
- **Active reminders:** use durable operational rules, occurrences, delivery attempts/receipts, timezone/DST behavior, restart catch-up, snooze, acknowledgement, cancellation/reschedule, deduplication, and quiet-hours policy. A Brain date can propose a reminder; it cannot fire or prove delivery.

This boundary preserves Brain as the common recall and evidence substrate while giving proactive workflows the transactional state, current-provider authority, and idempotent execution semantics that facts do not provide.

## Limitations And Promotion Gate

- The extraction pilots covered only 15 selected documents. The 75-title review was policy-conditioned, used classifier v4, and was not a random mailbox-wide recall sample; classifier v5 did not receive a separate manual title review.
- Historical archive rows created before Gmail labels were persisted have an explicit provider-label coverage gap. They never default to human, which favors precision over recall.
- Attachment bodies are excluded. Calendar/ICS attachments are not parsed, so some high-quality event structure is unavailable.
- Classification and temporal repair are deterministic and conservative. The final zero-time result shows that conservative failure can still be a product failure.
- The fresh v6 Sol sample contained four event-occurrence route-evidence failures among 12 applied facts; the complete pilot was rolled back rather than retaining partially accepted Gmail-derived knowledge. The deterministic route was fixed afterward but not externally rerun.
- Pattern-based secret scanning cannot prove the absence of all private or sensitive content. The old external Codex sessions were logically deleted after owner authorization, but this does not erase independent filesystem snapshots or external backups.
- The fresh pilot's complete `observed_at_basis` aggregate was not preserved before rollback. The code fails closed on unresolved Gmail message spans, but this is not a measured 42-of-42 result.
- A legacy host-origin pass left 35,408 inactive, byte-identical duplicate Gmail document rows. Stable Gmail origin identity prevents recurrence, but citation-aware physical cleanup is still pending.
- General retrieval reset/rechunk maintenance still replaces active chunk IDs and can orphan persisted fact citation IDs. Gmail fallback reactivation preserves retained chunk IDs, but the broader maintenance migration remains unresolved.
- Index compaction remains recommended after repeated immutable projection versions.

Production Gmail Knowledge projection and fact extraction remain disabled. Promotion requires, at minimum: a larger blinded labeled sample; acceptable advertising/routine precision and important-mail recall; zero secret-boundary violations; zero cross-occurrence/page-identity violations; useful structured event-time recall; and an all-acceptable final `gpt-5.6-sol` medium audit.
