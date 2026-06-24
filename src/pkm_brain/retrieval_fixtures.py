from __future__ import annotations

from typing import Any


RETRIEVAL_GOLDEN_CASES: list[dict[str, Any]] = [
    {
        "id": "sample-001",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_d048503297134984"],
        "query": (
            "Read-only evaluation task. Use ONLY the forked Brain data at "
            "/Users/Peter/brain-forks/wiki-review-llm-coach, not /Users/Peter/brain. Repo is "
            "/Users/Peter/pkm-brain on branch experiment/wiki-review-llm-coach. Confirm the fork has "
            "chief-of-staff compiled wiki/fact state, then run a few retrieval probes against that forked home "
            "using CLI or service calls with --home /Users/Peter/brain-forks/wiki-review-llm-coach. Include "
            "positive controls likely present, such as Hightouch, CloudZero, chief-of-staff wiki review, AI "
            "Chinese children's songs or dropshipping; and negative controls likely absent, such as fake "
            "business/engineering/personal topics. Evaluate what retrieval returns, whether compiled pages/facts "
            "make results more useful, whether absent topics still return noise, and reprioritize highest-ROI "
            "improvements. Do not edit files. Keep concise but concrete."
        ),
    },
    {
        "id": "sample-002",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_6494dbe069304f81"],
        "query": (
            "We are in the pkm-brain repo. User question: I'm looking to debug and also simplify the wiki "
            "review process so it's more human friendly. There is a huge backlog, and when I'm playing around "
            "with the UI, I couldn't approve the edit nor could I generate the questions to clarify things, is "
            "that not implemented? if so, create a plan on what to do next."
        ),
    },
    {
        "id": "sample-003",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_97664837d99f4b88"],
        "query": (
            'inspect the code from the amazon scraper, then the maxoutdeal scraper, and then the chase scraper, '
            'and then write up a MD that "reverse engineers" the spec from the code. Also, evaluate how well they...'
        ),
    },
    {
        "id": "sample-004",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_0f711b37c3b64192"],
        "query": (
            "going back to cloudflare, what if they rug-pull and starts charging for things? does this architecture "
            "make it so that it's hard to migrate away from them? what's the likihood of this type of behavior"
        ),
    },
    {
        "id": "sample-005",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_12eae5c870454dc3"],
        "query": "final check for any spelling grammar or self contradictory writing sets on the prioritization page",
    },
    {
        "id": "sample-006",
        "kind": "historical_session_query",
        "expected_verdict": "partial",
        "expected_source_ids": ["document:doc_ee5e4b2159654cd2"],
        "query": "write this as detailed spec based off of this and the state of the current code on what needs to be done to get to that target.",
    },
    {
        "id": "sample-007",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_c8871a4b49e6411d"],
        "query": (
            "You are reviewing the PKM Brain wiki architecture. Context: Brain ingests real source documents into "
            "SQLite/chunks; current legacy flow has LLM semantic wiki compiler and old wiki_change proposal packets. "
            "New Chief-of-Staff layer stores atomic source-backed facts in a facts table, resolves "
            "merges/supersedes/conflicts/open_questions deterministically, and renders managed Markdown wiki pages "
            "deterministically from active facts. The goal is a good wiki overview that evolves over time based on "
            "real ingested information, and that is useful when retrieved in future agentic interactions. Question: "
            "would you keep page writing deterministic from facts, make it LLM-powered, or use a hybrid? Propose "
            "the architecture, data contracts, review surfaces, and deprecation path for old wiki proposal bits. "
            "Be specific and critical; assume local-first, provenance, testability, and retrieval usefulness matter."
        ),
    },
    {
        "id": "sample-008",
        "kind": "historical_session_query",
        "expected_verdict": "partial",
        "expected_source_ids": ["document:doc_f92d98556f2e40a3"],
        "query": (
            "Does the service.ingest proposal then put the rest of the text into the body (or if there is such a "
            "field) so that the content is not lost, but the title isn't an absurd length?"
        ),
    },
    {
        "id": "sample-009",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_fedfacb44f6f4b91"],
        "query": (
            "Read-only evaluation task. Brain home is /Users/Peter/brain and repo is /Users/Peter/pkm-brain. "
            "Inspect the SQLite DB and wiki enough to choose a few positive-control topics that are actually in "
            "Brain and a few negative-control topics that should not be in Brain. Then use Brain retrieval as an "
            "agent would, preferably MCP if available or CLI fallback like 'uv run brain retrieve-context --home "
            "/Users/Peter/brain --task ...'. Do not edit files. Report: topics tested, what came back, whether "
            "results were useful, failure modes, and concrete improvement suggestions for making Brain retrieval "
            "answers more useful. Keep it concise but specific."
        ),
    },
    {
        "id": "sample-010",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_3f81db6e21344329"],
        "query": (
            "Audit this repository for spec-driven development quality. Do not modify files. Focus on whether the "
            "project and git repo follow proper spec-driven development best practices. Inspect the working tree, "
            "docs, tests, pyproject, CI, git history, and implementation as needed. Evaluate: presence and quality "
            "of requirements/specs, architecture/design docs, acceptance criteria, traceability from spec to "
            "implementation and tests, issue/ADR/change records, test coverage against the spec, release/publishing "
            "readiness, repo hygiene, security posture for a finance/write-capable MCP, and whether commits tell a "
            "spec-driven story. Return concise prioritized findings with severity, file/line references where "
            "possible, and concrete remediation steps. If there are no findings in a category, say so. Also include "
            "a final verdict: compliant, partially compliant, or not compliant with spec-driven development best practices."
        ),
    },
    {
        "id": "sample-011",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_afdc4316cddb4a40"],
        "query": (
            "Do a follow-up audit of this repository after the recent spec-driven-development hardening commit. "
            "Do not modify files. Review the current committed working tree, docs, tests, CI, and git history. "
            "Focus on whether the repo now satisfies proper spec-driven development best practices and whether "
            "the previous feedback appears remediated: formal requirements/specs, acceptance criteria, traceability, "
            "ADR/change records, test coverage against the spec, release/publishing readiness, repo hygiene, security "
            "posture for a finance/write-capable MCP, and whether remaining git history/process issues matter. Return "
            "concise prioritized findings with severity and file/line references where possible. Be strict: identify "
            "any remaining gaps, contradictions, weak tests, overclaims, or process artifacts that are present but not "
            "useful. Include a final verdict: compliant, partially compliant, or not compliant."
        ),
    },
    {
        "id": "sample-012",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_00dca444c27449c9"],
        "query": (
            "Need plan only. Repo facts: pkm-brain UI stdlib http.server in src/pkm_brain/ui_server.py. Wiki review "
            "endpoints exist for interview, interview/generate, reject, apply, approve-and-apply. Detail pane has "
            "Generate questions, Approve, Apply, Approve & Apply. Review table wiki rows only show Open and Reject. "
            "Apply requires status approved. Approve records an interview with disposition approved. Review queue "
            "excludes approved proposals though other pending helpers include approved. Generate questions catches "
            "provider config errors but not runtime complete errors; UI sends provider null so default codex may "
            "hang/fail. Tests pass for endpoint happy paths. Live backlog is 360 needs_interview plus 1 proposed, "
            "clustered by target page. User says UI could not approve or generate questions and wants a human-friendly "
            "backlog plan. Propose concise implementation-ready plan, local-first, no React/FastAPI/new schema unless necessary."
        ),
    },
    {
        "id": "sample-013",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_d1a4d80909944964"],
        "query": (
            "Follow-up audit this repo after commit 6a98a26. Do not modify files. Assess whether the previous "
            "spec-driven-development critique is now remediated. Review current committed files, tests, CI, docs, "
            "and git history. Be strict but concise. Return prioritized remaining findings with severity and "
            "file/line refs where possible, covering specs/acceptance criteria/traceability/ADRs/changelog/tests/CI/"
            "security/release/repo hygiene/git history. End with final verdict: compliant, partially compliant, or not compliant."
        ),
    },
    {
        "id": "sample-014",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_31c8e204f8a94eeb"],
        "query": (
            "do an audit of the brain pkm project and the github codebase. See how it measures up against the "
            "current spec. If there is any drift, update the spec doc to match the project. Then also call out what..."
        ),
    },
    {
        "id": "sample-015",
        "kind": "historical_session_query",
        "expected_verdict": "partial",
        "expected_source_ids": ["document:doc_a67ac5fb89a84457"],
        "query": (
            "How would I make this into an HTML page that's a single document with capabilities to edit the underlying "
            "fields so that it changes the rendering of the HTML live? I'm thinking, what if I want to ch..."
        ),
    },
    {
        "id": "sample-016",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_ee5e4b2159654cd2"],
        "query": "can you do a writeup of your recommendation and the discussion that lead up to that so that I can send it to codex?",
    },
    {
        "id": "sample-017",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_4f84427003d0493f"],
        "query": (
            "go into the brain project, and build a plan on how we can make the browser UI also function as a Wiki "
            "explorer and editor. Are there elegant ways in which the browser can review proposed memory, and..."
        ),
    },
    {
        "id": "sample-018",
        "kind": "historical_session_query",
        "expected_verdict": "partial",
        "expected_source_ids": ["document:doc_87288d2d8aab4562"],
        "query": "can you try that again? I think something bugged out and I got some garbeled text.",
    },
    {
        "id": "sample-019",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_862580871fd54b9c"],
        "query": (
            "We are redesigning PKM Brain's wiki review architecture. Current system has many wiki_change proposal "
            "batches and a browser UI that groups packets and asks an LLM to generate review questions/drafts. User "
            "feedback: it is too conservative, asking questions like whether to keep duplicates or where to store info. "
            "Those should be handled autonomously. Desired behavior: feels like a chief of staff curating knowledge pages; "
            "LLM aggressively aggregates, deduplicates, routes facts to pages, prioritizes recent high-confidence "
            "source-backed evidence, and only asks the user when there are direct factual conflicts or insufficient "
            "evidence. Also avoid huge proposal backlogs; instead ask users factual questions, get fact confirmations, "
            "and directly author/maintain wiki pages rather than asking the user to approve wiki prose. Local-first Python "
            "project, stdlib http.server UI, SQLite, Markdown wiki under ~/brain/wiki, review-gated memory/wiki currently. "
            "Propose a re-architecture: data model, workflow, UI, LLM responsibilities, safety constraints, migration path "
            "from current proposals, and implementation phases. Be opinionated and practical. Max 1200 words."
        ),
    },
    {
        "id": "sample-020",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_7cb5aec7b3124ada"],
        "query": (
            "Repo: pkm-brain. We fixed wiki review buttons; now user wants clever batching/triage for a huge wiki "
            "proposal backlog. Facts: pending queue has 362 batches and 1083 change items. Operations: create_page "
            "809 items across 420 targets, append_section 168 across 83 targets, replace_section 72 across 23 targets, "
            "replace_page 34 across 20 targets. Same-target clusters include projects/amazon-maxoutdeal-chase-reconciliation.md "
            "38 items, career/google-pm-interview.md 30, companies/hightouch.md 24. 129 target+section+operation groups "
            "have multiple pending items. Code behavior: items inside a single batch apply sequentially, but separate pending "
            "batches are not stacked into a branch; each proposal preview is against current live wiki. create_page fails if "
            "target exists; replace_section/replace_page are usually mutually exclusive for same target/section; append_section "
            "can accumulate but may duplicate. User asks: temporal grouping, topic grouping, reviewer psychology/stale memory, "
            "whether only newest edit is valid, and wants a plan. Give concise implementation-ready plan for batching review. "
            "Local-first, stdlib UI, no React/FastAPI; avoid schema unless strongly justified. Max 700 words."
        ),
    },
    {
        "id": "negative-001",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "ZephyrMart geothermal coffee roasting in Iceland",
    },
    {
        "id": "negative-002",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "mango orchard irrigation sensors in Fresno",
    },
    {
        "id": "negative-003",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Kubernetes eBPF Mars rover telemetry",
    },
    {
        "id": "hist-021",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_0bb736416b30494c"],
        "query": "chrome crashed a few times, look through the logs and identify the root cause",
    },
    {
        "id": "hist-022",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_fa6fc11310314a72"],
        "query": "Chief of staff architecture design discussion optimum spec page contracts action policy audit",
    },
    {
        "id": "hist-023",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_0c7559a10f5445fd"],
        "query": "Design PKM wiki architecture for agent retrieval",
    },
    {
        "id": "hist-024",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_aeac0d3cadb84352"],
        "query": "Prioritize Brain MCP retrieval improvements",
    },
    {
        "id": "hist-025",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_f9a5dea8739e4603"],
        "query": "Evaluate forked Brain data retrieval quality and ROI",
    },
    {
        "id": "hist-026",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_9bda813d297046bd"],
        "query": "Review headed login and JSON parsing patches",
    },
    {
        "id": "hist-027",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_989e0e85854b4fa1"],
        "query": "Review git diffs for blocking bugs",
    },
    {
        "id": "hist-028",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_e8dae4d4fde545ce"],
        "query": "add business idea re-creating childrens songs in chinese using AI and publishing it in english",
    },
    {
        "id": "hist-029",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_b9ee3774a5d9489c"],
        "query": "Fix fact duplication in PKM wiki page renderer",
    },
    {
        "id": "hist-030",
        "kind": "historical_session_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_126acb5a99e347c3"],
        "query": "preserve document identity across retrieval resets chunk ids not stable provenance immutable",
    },
    {
        "id": "doc-031",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_402f9cb52c6e45b6"],
        "query": "Peter Wang Netflix recruiter phone screen",
    },
    {
        "id": "doc-032",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_6b61001f76ac488f"],
        "query": "Google Product Analytics Interview operational database storage compute separation",
    },
    {
        "id": "doc-033",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_b8803ebbc5d24352"],
        "query": "Google Interview Analytics",
    },
    {
        "id": "doc-034",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_5468974eda7e4d29"],
        "query": "Hightouch Peter Corey conversation",
    },
    {
        "id": "doc-035",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_63ccc2d70fc3429f"],
        "query": "Interview with CloudZero Confirmation May 29",
    },
    {
        "id": "doc-036",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_1d4d156163cc49c6"],
        "query": "Call with Alicia",
    },
    {
        "id": "doc-037",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_defe15948b624cfa"],
        "query": "Phoebe Peter conversation",
    },
    {
        "id": "doc-038",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_2f6c06fa5baf4eb8"],
        "query": "Josh Peter conversation",
    },
    {
        "id": "doc-039",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_772fc3cc6c4f4ce3"],
        "query": "Spencer Peter Sierra chat",
    },
    {
        "id": "doc-040",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_9a7307076b2647c5"],
        "query": "Hari Networking Peter Wang",
    },
    {
        "id": "doc-041",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_2805d203d3de4be0"],
        "query": "Andrej Karpathy wiki idea shipped by Pinecone",
    },
    {
        "id": "doc-042",
        "kind": "source_document_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_d9f6a0c7f94f4333"],
        "query": "Memory and dreaming for self-learning agents",
    },
    {
        "id": "fact-043",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_f1885f0956494246"],
        "query": "AI Chinese children songs publish English versions legal distribution audience validation",
    },
    {
        "id": "fact-044",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_97664837d99f4b88"],
        "query": "Amazon MaxOutDeals Chase reconciliation Delaware received profitability",
    },
    {
        "id": "fact-045",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_2f7d5955ff784879"],
        "query": "Sierra take-home concise PDF prioritization assumptions 2-3 hours",
    },
    {
        "id": "fact-046",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_16c8e2cf534744bc"],
        "query": "Sierra offer written materials benefits Pave equity value slider e-signature",
    },
    {
        "id": "fact-047",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_1117263b4f5543ce"],
        "query": "Sierra Agent PM workload 996 Sunday work not standard",
    },
    {
        "id": "fact-048",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_105f3976158042a7"],
        "query": "WGS based PGT polygenic disease risk Orchid whole genome sequencing raw data portability",
    },
    {
        "id": "fact-049",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_f8917dba88864233"],
        "query": "PKM Brain source-aware retrieval lineage weak agent log evidence",
    },
    {
        "id": "fact-050",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_aeac0d3cadb84352"],
        "query": "Brain MCP retrieval improvement local knowledge search lower noise",
    },
    {
        "id": "fact-051",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_126acb5a99e347c3"],
        "query": "Preserve document identity across retrieval resets chunk ids not stable",
    },
    {
        "id": "fact-052",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_6b61001f76ac488f"],
        "query": "agent context cost observability cost latency accuracy",
    },
    {
        "id": "fact-053",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_6b61001f76ac488f"],
        "query": "agentic data platform strategy task scoped access metadata lineage",
    },
    {
        "id": "fact-054",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_d1a4d80909944964"],
        "query": "Monarch Money MCP official install check for Codex and Claude",
    },
    {
        "id": "fact-055",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_d9f6a0c7f94f4333"],
        "query": "local-first memory gives agents continuity outside a single session",
    },
    {
        "id": "fact-056",
        "kind": "fact_query",
        "expected_verdict": "found",
        "expected_source_ids": ["document:doc_fa6fc11310314a72"],
        "query": "page contracts chief of staff wiki fact routing gardener convergence target",
    },
    {
        "id": "fact-057",
        "kind": "fact_query",
        "expected_verdict": "partial",
        "expected_source_ids": ["document:doc_c8871a4b49e6411d"],
        "query": "two-zone rendering deterministic active facts synthesis cites fact IDs",
    },
    {
        "id": "negative-004",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "NimbusCRM lunar invoice reconciliation",
    },
    {
        "id": "negative-005",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "QuasarDentistry blockchain orthodontic retainers",
    },
    {
        "id": "negative-006",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "VioletRail submarine ticketing API",
    },
    {
        "id": "negative-007",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "HelioBakery quantum sourdough scheduling",
    },
    {
        "id": "negative-008",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Arctic bonsai drone pollination",
    },
    {
        "id": "negative-009",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Neptune warehouse forklift telemetry",
    },
    {
        "id": "negative-010",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Quartz avalanche hotel occupancy forecasting",
    },
    {
        "id": "negative-011",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "BlueCircuit underwater semiconductor fab",
    },
    {
        "id": "negative-012",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Frosted satellite espresso grinder",
    },
    {
        "id": "negative-013",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Obsidian vineyard robot tax treaty",
    },
    {
        "id": "negative-014",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Pixel lighthouse insurance claims",
    },
    {
        "id": "negative-015",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Paperclip volcano escrow API",
    },
    {
        "id": "negative-016",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Emerald tunnel fashion inventory",
    },
    {
        "id": "negative-017",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Solar canoe DNS propagation",
    },
    {
        "id": "negative-018",
        "kind": "negative_control",
        "expected_verdict": "no_strong_match",
        "expected_source_ids": [],
        "query": "Crimson museum antifreeze procurement",
    },
]
