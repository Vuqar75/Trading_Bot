# Confirmed fixes during the first-day audit

The read-only financial/decision audit remains the immutable pre-maintenance
snapshot. Do not attribute later maintenance observations to the first24hours.

Saved false book_receipt_time examples have BBO receive1–6ms AFTER the packet's
validation timestamp, while source E/T are already in the past. quote_packet
read now BEFORE acquiring the quote lock; a receiver may publish while it waits.
The deterministic temporary test reproduces the false rejection on b36103c.
Move only the clock sample INSIDE the same lock as component selection. Preserve
real future/clock-regression, stale5000ms, sequence, reconnect, overflow, liquidity
and single-writer checks. No blind tolerance, strategy, budget or risk adjustment.
The three new cases plus23existing quote/protection cases pass26unique tests at
CPU50%/128MiB; no full suite/model/trade or profiler. Test-only temporary ledger
positions do not touch the VM's actual paper history or original AI journal.

Fresh external restore was a required rollout gate. Saved hourly backup log
shows five consecutive3min ExecStartPost terminations12:27–16:27Baku; original
AI-budget backup still succeeded. Bracket DB grew to73510912B, latest verified
external ledger was11:29/34events/6fills, missing the later LONG. Retain failed
partial files and old receipts; don't call them completed backups.

A bounded6min validation at unchanged backup CPU15%/Memory96MiB and the SAME
hourly timer completed315.210308394s,34.736901s CPU,10chunks,40events/8fills,
full download/decrypt/hash/financial-summary restore success. The old3min bound
cannot contain that observed work; keep the explicit6min service drop-in.
It is unrelated to the strict5sec MARKET freshness bound. Future DB growth can
outgrow this fixed budget; this proves one current-size archive, not unlimited
scalability. No resource purchase, IAM change, AI call or schedule change.

Deploy only the tested stream module into a retained copy of current b36103c,
after a new flat/financial/response-ingestion check and the successful external
restore. Coordinate the existing analyst timer briefly only for the flat switch.
Rollback changes code, never restores an old DB over newer events; never stop
protection with open risk. Ordinary serve remains WITHOUT --phase-diagnostics.
Verify one writer/poller, actual hashes, preserved ledger, source/publication/UI,
and a fresh post-switch external restore. Do not create a diagnostic autochain.
