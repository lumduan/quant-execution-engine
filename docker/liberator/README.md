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

## What is still here, and the same caveat applies

| file | vs `broker-api/docker/liberator/` |
|---|---|
| `order_config.yaml` | identical |
| `session_status.yaml` | identical |
| `system.yaml` | **DIFFERS** — do not read this one as current either |
| `Dockerfile` | **DIFFERS** |

They are kept only because several plan and operations docs still cite these paths; removing the
directory wholesale is a documentation pass, not a file deletion. **Treat every file here as
historical.** If you need to know what the bridge actually runs, read `broker-api/docker/liberator/`
or the container itself.
