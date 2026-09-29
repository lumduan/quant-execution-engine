# Deploying the engine — and the architecture check

Every deploy of the engine, on either node, goes through [`scripts/deploy.sh`](../../scripts/deploy.sh).
It asserts that the image's architecture equals the node's **before** anything changes, and
refuses otherwise (operator ruling 2026-09-29).

## Why an architecture check, when the image ID already matches

A matching image ID proves the image is the one you built. It says nothing about which CPU it was
built for. HOME builds the AWS images (arm64, under emulation) and keeps a standing arm64
emulation handler so it can (TK-0599 in the platform's ticket store, 2026-09-11). So an arm64
image recreated on HOME does not fail at start. It runs, slowly, under QEMU, and looks healthy.
The check is what makes that mistake loud.

## The two nodes

| | HOME | AWS |
|---|---|---|
| Architecture | amd64 | arm64 |
| Compose files | `docker-compose.yml` + `docker-compose.override.yml` | `docker-compose.yml` + `docker-compose.private.yml` + `docker-compose.aws.yml` |
| Image the compose files run | `quant-execution-engine-execution-engine` (build-only service, compose default name) | `quant-execution-engine:aws-arm64` |
| Where the image comes from | `docker compose build execution-engine` on HOME | built on HOME under a distinct tag `quant-execution-engine:aws-arm64-<sha>`, shipped with `docker save \| gzip \| ssh … docker load` |

The script reads the compose file set from the **running container's own compose labels**, so a
deploy recreates the container with exactly the files it was created with. It never builds and
never pulls (`--no-build --pull never`).

## Usage

```bash
scripts/deploy.sh --candidate <image> --dry-run   # every check, no change
scripts/deploy.sh --candidate <image>             # deploy <image>
scripts/deploy.sh                                 # recreate from what the compose files resolve
```

HOME, after a build: `scripts/deploy.sh` (the build moved the compose name to the new image).
AWS, after `docker load`: `scripts/deploy.sh --candidate quant-execution-engine:aws-arm64-<sha>`.

What a deploy does, in order:

1. Checks the candidate's (or the compose image's) OS and architecture against `docker info`.
   **A refusal here changes nothing.**
2. Tags the running image `…:rollback-<UTC timestamp>`.
3. Points the compose image name at the candidate, and checks it once more.
4. Recreates the service only (`--force-recreate --no-deps`) and waits for it to be healthy.
5. Checks the running container's image ID **and** architecture, then prints the rollback.

Rollback is the same script: `scripts/deploy.sh --candidate <the rollback tag it printed>`, so a
rollback is architecture-checked too.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | deployed, or with `--dry-run` every check passed |
| 2 | precondition: no existing container (a first bring-up is not a deploy), image absent, a compose file missing |
| 3 | **REFUSED** — an image's architecture or OS is not this node's |
| 4 | the recreate or the post-deploy check failed; the rollback command is printed |

## What it does not do

- It does not hold the AWS node lease. On AWS, acquire it first, and deploy only in a window
  outside every live session, with the times fetched from the exchanges.
- It does not build. The AWS image is built off the node; building on the production node is
  banned (umbrella `aws-quant-ops` skill).
