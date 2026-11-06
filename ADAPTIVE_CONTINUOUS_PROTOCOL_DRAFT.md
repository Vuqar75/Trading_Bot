# Continuous adaptive study: pre-start draft (not ready for execution)

This draft does not start the experiment or authorize entries. The precommitted
strategy rules in `ADAPTIVE_ROUTER_POLICY.md` remain unchanged and economically
rejected. This protocol must be finalized and committed before first collection.

## Experiments and decision provenance

AI and deterministic baseline use separate financial/queue/route databases,
each starting once with 1,000 virtual USDT, split into 500 Spot cash and 500
isolated collateral. Max entry notional 10 USDT; isolated leverage 1x; no more
than one BTCUSDT exposure per experiment. The proposed deterministic allocation
is a fixed 500/500 split at initialization, with no automatic inter-wallet
transfers thereafter; an entry with insufficient funds in its market is rejected.
If a future explicitly authorized internal transfer occurs it is a balanced
ledger movement, not profit. Initialization never repeats on restart. This
policy is a draft until fixed in the pre-start protocol.

Both calculations use the same captured contemporaneous candle/snapshot record,
same market-specific source, prices, filters, risk and cost rules. The model
and baseline labels alone differ. The experiment starts at the actual durable
initialization time with cash; earlier decisions are ineligible for entry.
The previous snapshot contains derived features but not its raw candle inputs,
so past candle provenance cannot be reconstructed by a later fetch. Future
evidence must be captured in the same collection call, durable before its
model request, published by a trusted privileged producer and read-only to the
study service. A self-consistent JSON hash by itself authenticates nothing.
The verifier prototype is `research/adaptive_snapshot_proof.py`; it is not yet
connected to the server executor or a protected publisher.

## Market evidence and operational boundaries

For isolated execution and funding, use USD-M perpetual BTCUSDT data, including
the exchange's funding event time and mark, and separately record the actual
local first receipt time. The funding history endpoint does not supply an
independent publication timestamp; receipt must not be labelled publication.
Missing, changed, late or incomplete events must remain visible and block entry;
they must never silently become zero. Do not use a Spot price as futures mark.
The public USD-M server-time endpoint returned a response from the current VM;
this does not demonstrate funding history availability or correctness.

Before the already authorized deployment, establish the guarded
boundary for *all* experiment financial writers, trusted publisher and replay
protection, real funding collection, continuous supervision, split-wallet
accounting, bounded service, health, and an independently restored external
backup first. Measure resource headroom and preserve the independent observer,
the working financial system and the existing paid AI schedule. No old
financial or decision histories may be overwritten.
