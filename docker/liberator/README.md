# ⚠️ VESTIGIAL — the live Liberator bridge config is NOT here

These files are a **stale copy** of the bridge's config set, left behind when the Liberator
bridge was de-nested from `quant-execution-engine/third_party/` into the umbrella's
`broker-api/` plane (`feature-broker-api-plane`). They were last touched **2026-06-14** and
**nothing in this repository mounts, COPYs, or reads them** — no Dockerfile, no compose file.

## The live copies

`broker-api/docker/liberator/` — mounted read-only into the running bridge by
`broker-api/docker-compose.yml`:

```yaml
- ./docker/liberator/trading_hour.yaml:/app/config/trading_hour.yaml:ro
```

Verified 2026-09-06: the umbrella copy is digest-identical to the file inside the running
container on both nodes.

## 🔴 Why `trading_hour.yaml` was DELETED rather than left here

It was a vendor **sample** (`# Trading Hours Configuration (Sample)`), and its `market_holidays`
block held **two 2025 dates and no 2026 data at all** — while the live calendar in `broker-api/`
holds **20 entries, all 2026**.

That is not a harmless duplicate. A holiday calendar answers a **yes/no** question, and an empty
one answers **"not a holiday" for every date you ask about**. On 2026-09-06 it did exactly that:
a check of whether 2026-09-07 was a trading day read this file, got `False`, and would have been
believed. What caught it was a positive control — *"does this calendar contain any 2026 entries
at all?"* — not the query itself. **An absent answer and a negative answer are indistinguishable
without that control**, which is why the file is gone rather than annotated.

## 🔴 Why `system.yaml` was DELETED too (2026-09-06)

It differed from the live copy by exactly one value — and it was the wrong one:

```
engine (stale)      logging.level: "INFO"
broker-api (live)   logging.level: "WARNING"
deployed, BOTH nodes logging.level: "WARNING"   ← measured, not assumed
```

`WARNING` was set under **[[TK-0032]]**: the relay's per-request order-book-lookup INFO flood
filled the AWS node's root disk on 2026-07-06, dockerd failed to start, and the node was down for
roughly **34 hours**. `INFO` is the setting that caused it.

So this file was worse than merely out of date. It was a plausible-looking, correctly-structured
config whose one meaningful value was the one a P0 had already been paid to change — sitting in a
directory a reader could easily mistake for the live one. Copying it forward would have
re-introduced the outage; nothing would have warned them.

⚠️ Note the *shape* it shares with the deleted `trading_hour.yaml`: neither file was broken, and
neither would fail a syntax check. Both were **valid files carrying a wrong answer**, which is why
labelling was not enough for either.

## What is still here

| file | vs `broker-api/docker/liberator/` | risk |
|---|---|---|
| `order_config.yaml` | **identical** | none — a harmless duplicate |
| `session_status.yaml` | **identical** | none — a harmless duplicate |
| `Dockerfile` | **DIFFERS** | comments only: it still says the bridge is *"vendored under `third_party/`"* and that *"the execution-engine owns this build"*. Both were true before the de-nesting and are false now. Nothing builds it, so the staleness is descriptive rather than executable |

**Both remaining `.yaml` files are byte-identical to the live ones**, so the directory no longer
holds a value that disagrees with production. What is left is a stale build recipe and two exact
duplicates.

They are kept because thirteen plan and operations docs still cite these paths — most of them
*historical* records that were accurate when written, which is why the fix is not to rewrite them.
**Treat every file here as historical.** If you need to know what the bridge actually runs, read
`broker-api/docker/liberator/` or the container itself.
