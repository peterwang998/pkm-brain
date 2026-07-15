# Email Ingestion

**Status:** compatibility pointer; Gmail operational mirroring and the encrypted archive are implemented and live-validated, while default retrieval indexing and durable-knowledge ingestion remain pending
**Last verified:** 2026-07-14 against the completed private 90-day copy and first Gmail history catch-up

The owner-authorized operational lane uses exact-scope `gmail.readonly` API access to maintain a bounded, rebuildable local mirror at `~/brain/cache/gmail-mirror/gmail-mirror.sqlite`: exact seven-day bootstrap, history-based incremental sync about every 600 seconds, immutable sanitized revisions/current pointers, provider-confirmed tombstones, a durable triage queue, content-free poison-thread quarantine, recoverable saved page tokens, and one atomic provider checkpoint. After normal checkpoint acceptance, at most ten due quarantined threads may be retried in a second generation with durable exponential backoff and parser-version reprocessing; retry-only failure cannot make an accepted mailbox checkpoint stale. Attachment metadata may be retained, but attachment bodies/bytes are not fetched or stored. Luna drains the local queue with no Gmail calls, and mailbox freshness plus scheduled-sync health remain independent from analysis/quarantine backlog.

This mirror is operational evidence only. It does not create inbox artifacts, documents, chunks, facts, entities, retrieval indexes, or wiki pages. The separate retrieval, durable-knowledge, and operational contracts live under [Future Gmail And Email Adapter](specs/capture-and-knowledge.md#future-gmail-and-email-adapter), [Chief-of-Staff Operations](specs/chief-of-staff-operations.md#gmail-lanes), and [the operational implementation plan](plans/chief-of-staff-operations-implementation-plan.md).

## Encrypted Gmail History Archive

The archive is a separate local evidence store, not a wider operational mirror or a Knowledge-ingestion path. It uses the account-bound Gmail API to save the exact RFC-2822 bytes returned by `messages.get(format=RAW)`, including MIME attachments. Raw messages and the parsed text used for local search are encrypted with AES-256-GCM; the archive key is held in macOS Keychain.

V1 has one sync state machine. It performs a resumable fixed 90-day backfill, then follows Gmail history incrementally for new and changed messages. If the Gmail history cursor expires, it restarts the full scan safely. A provider deletion marks the message unavailable at Gmail but retains the local ciphertext.

V1 search is intentionally simple: the daemon decrypts and scans locally when requested, then returns bounded results. It does not maintain a second full-text, token, or embedding index. `search_mail` returns bounded metadata and snippets; `get_mail_thread` returns bounded parsed text and attachment descriptors, never attachment bytes. Both tools require the authenticated daemon, are absent from the direct MCP fallback, and label message content as untrusted.

The scheduler reports the stored-message count, current state, or a plain-language pause reason; Gmail's unreliable result-size estimate is not presented as a total. Managed storage reports archive file size. Archive sync never mutates Gmail and does not invoke Luna, another LLM, fact extraction, Knowledge Curation, or the operational detector.

The first private run copied the fixed window from 2026-04-15T23:42:21Z through 2026-07-14T23:42:21Z, then processed five Gmail history changes and reported current. The result contains 7,746 active messages across 6,857 threads: 758,041,932 original raw bytes in an encrypted SQLite file occupying about 403 MB at installed verification. SQLite `quick_check` is `ok`; the store has exactly the three application tables defined by V1, its directory/database permissions are `0700`/`0600`, the archive is bound to the approved immutable Gmail identity, and the Keychain value round-trips as a 32-byte key.

The app and daemon surface is defined in [App And Operations](specs/app-and-operations.md).
