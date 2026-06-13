# Deployment — compose topology

How the engine and its dependencies are wired across the three compose configurations. The
operator-facing version is [`docs/operations/bring-up.md`](../../docs/operations/bring-up.md); this is
the agent reference for the container/network/env-load details.

## Services & containers

| Service | Container | Network exposure | Role |
|---------|-----------|------------------|------|
| `execution-engine` | `quant-execution-engine` | host `:8400` → container `:8000` | the API |
| `execution-redis` | `quant-execution-redis` | **internal only** (no host port) | dedupe / single-flight lock / rate-limit; URL `redis://execution-redis:6379/0` |
| `liberator-trading-api` | `liberator-trading-api` | **internal only** (`:8200`, no host port) | bundled upstream the `LiberatorAdapter` composes over (D9) — overlay only |
| `liberator-redis` | `liberator-redis` | **internal only** | Liberator's OWN Redis (OTP/session state) — overlay only |

All join the external `quant-network` (`external: true`); `quant-postgres` (`db_execution`) and
`quant-api-gateway` come from their own repos. Use **service hostnames inside containers**, never
`localhost`.

## The three configurations

```bash
# a. public / sim default — broker-free, PUBLIC_MODE=true, STAGE=sim
docker compose up -d

# b. owner mode + Settrade (cloud API; creds from .env via env_file)
docker compose -f docker-compose.yml -f docker-compose.private.yml up -d

# c. owner mode + bundled Liberator upstream (internal-only)
docker compose -f docker-compose.yml \
               -f docker-compose.private.yml \
               -f docker-compose.liberator.yml up -d
```

- `docker-compose.private.yml` flips `EXECUTION_ENGINE_PUBLIC_MODE=false` and adds `env_file: .env`
  (broker secrets + `EXECUTION_ENGINE_API_KEY`). Settrade is a **cloud API — no overlay service**.
- `docker-compose.liberator.yml` adds `liberator-trading-api` + `liberator-redis` and points the
  engine at `EXECUTION_ENGINE_LIBERATOR_BASE_URL=http://liberator-trading-api:8200/api/v1`. Layer it
  **after** the private overlay (public/sim stays broker-free).

## Env-load order (the Liberator gotcha)

- **Engine:** `EXECUTION_ENGINE_*` from the process environment + `.env` (private overlay
  `env_file`). `pydantic-settings`, frozen at startup.
- **Bundled Liberator upstream:** its settings loader reads **YAML over env** — so Redis host and the
  internal port are pinned via the mounted `docker/liberator/system.yaml` (plus
  `session_status.yaml` / `trading_hour.yaml` / `order_config.yaml`, which `validate_configuration()`
  requires at startup; the image ships only `*.yaml.example` templates so these complete it
  deterministically — no secrets). Its **credentials** (`LIBERATOR_USERNAME` / `PASSWORD` / `PIN`,
  `TEL_NO`, `API_KEY`) come from the same `.env` (`env_file`). The auth-token store + logs persist on
  named volumes so an OTP session survives a restart.

## Host ports vs in-container

Only the engine exposes a host port (`:8400`, override `EXECUTION_ENGINE_HOST_PORT`). Redis and the
Liberator upstream are internal-only — preserving the sole-credential-owner boundary
(`liberator-trading-api` is never reachable from outside the network, never a platform peer, not in
the umbrella network-contract table).

## Fresh-clone gotcha

`third_party/liberator-trading-api` is a nested **private git submodule**. After cloning, run
`git submodule update --init --recursive` (or `--init` in this repo) before the Liberator overlay can
build. A `-` prefix in `git submodule status` = it failed to populate (usually missing GitHub https
credentials). The public/sim default does not need it.

See also: [`order-flow.md`](order-flow.md), [`order-book-service.md`](order-book-service.md),
[`docs/operations/configuration.md`](../../docs/operations/configuration.md).
