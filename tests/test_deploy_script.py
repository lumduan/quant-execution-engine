"""scripts/deploy.sh — the architecture gate on every engine deploy (operator ruling 2026-09-29).

The script runs against a fake ``docker`` on PATH that keeps its state in a JSON file and logs
every call, so each test can assert both the exit code and that a refusal moved no tag and
recreated nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"

AMD = "sha256:" + "a" * 64
ARM = "sha256:" + "b" * 64
AMD_NEW = "sha256:" + "c" * 64
WINDOWS = "sha256:" + "d" * 64

FAKE_DOCKER = r"""
import json, sys
state_path, log_path = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
with open(state_path) as fh:
    st = json.load(fh)
with open(log_path, "a") as fh:
    fh.write(json.dumps(args) + "\n")

def save():
    with open(state_path, "w") as fh:
        json.dump(st, fh)

def resolve(ref):
    if ref in st["tags"]:
        return st["tags"][ref]
    if ref in st["images"]:
        return ref
    return None

def opt(name):
    return args[args.index(name) + 1]

if args[:1] == ["info"]:
    print(st["node_arch"])
elif args[:2] == ["image", "inspect"]:
    iid = resolve(args[2])
    if iid is None:
        print("")  # the real CLI prints an empty line before failing
        sys.stderr.write("Error response from daemon: No such image\n")
        sys.exit(1)
    img = st["images"][iid]
    fmt = opt("--format")
    print(iid if fmt == "{{.Id}}" else f"{img['os']}/{img['arch']}")
elif args[:1] == ["ps"]:
    want = [a.split("=", 1)[1] for i, a in enumerate(args) if args[i - 1] == "--filter"]
    want = [w.split("=", 1) for w in want]
    for c in st["containers"]:
        if all(c["labels"].get(k[len("label="):] if k.startswith("label=") else k) == v
               for k, v in want):
            print(c["id"])
elif args[:1] == ["inspect"]:
    c = next(c for c in st["containers"] if c["id"] == args[1])
    fmt = opt("--format")
    if fmt == "{{.Name}}":
        print(c["name"])
    elif fmt == "{{.Image}}":
        print(c["image"])
    else:
        key = fmt.split('"')[1]
        print(c["labels"].get(key, ""))
elif args[:1] == ["tag"]:
    st["tags"][args[2]] = resolve(args[1])
    save()
elif args[:1] == ["compose"]:
    if "config" in args:
        svc = {} if st["compose_image"] is None else {"image": st["compose_image"]}
        print(json.dumps({"services": {"execution-engine": svc}}))
    elif "up" in args:
        if st["up_result"] == "fail":
            sys.exit(1)
        ref = st["compose_image"] or "quant-execution-engine-execution-engine"
        c = st["containers"][0]
        c["image"] = st["tags"][ref] if st["up_result"] == "ok" else AMD_OTHER
        save()
else:
    sys.stderr.write(f"fake docker: unhandled {args}\n")
    sys.exit(99)
""".replace("AMD_OTHER", repr("sha256:" + "e" * 64))


class Node:
    """A throwaway checkout with a fake docker daemon behind it."""

    def __init__(self, root: Path, node_arch: str, compose_image: str | None) -> None:
        self.repo = (root / "repo").resolve()
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / "scripts" / "deploy.sh")
        self.files = [self.repo / "docker-compose.yml", self.repo / "docker-compose.aws.yml"]
        for f in self.files:
            f.write_text("services: {}\n")
        self.state_path = root / "state.json"
        self.log_path = root / "calls.log"
        self.log_path.write_text("")
        ref = compose_image or "quant-execution-engine-execution-engine"
        self.ref = ref
        self.state: dict[str, Any] = {
            "node_arch": node_arch,
            "compose_image": compose_image,
            "up_result": "ok",
            "images": {
                AMD: {"os": "linux", "arch": "amd64"},
                AMD_NEW: {"os": "linux", "arch": "amd64"},
                ARM: {"os": "linux", "arch": "arm64"},
                WINDOWS: {"os": "windows", "arch": "amd64"},
            },
            "tags": {},
            "containers": [
                {
                    "id": "c0ffee",
                    "name": "/quant-execution-engine",
                    "image": AMD,
                    "labels": {
                        "com.docker.compose.service": "execution-engine",
                        "com.docker.compose.project": "quant-execution-engine",
                        "com.docker.compose.project.working_dir": str(self.repo),
                        "com.docker.compose.project.config_files": ",".join(
                            str(f) for f in self.files
                        ),
                    },
                }
            ],
        }
        bindir = root / "bin"
        bindir.mkdir()
        fake = root / "fake_docker.py"
        fake.write_text(FAKE_DOCKER)
        docker = bindir / "docker"
        docker.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "{self.state_path}" '
            f'"{self.log_path}" "$@"\n'
        )
        python3 = bindir / "python3"
        python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        for exe in (docker, python3):
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.path = f"{bindir}{os.pathsep}{os.environ['PATH']}"

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def load(self) -> dict[str, Any]:
        return dict(json.loads(self.state_path.read_text()))

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.save()
        return subprocess.run(
            [str(self.repo / "scripts" / "deploy.sh"), *args],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": self.path},
            check=False,
            timeout=60,
        )

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def mutating_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["tag"] or (c[:1] == ["compose"] and "up" in c)]


@pytest.fixture
def home(tmp_path: Path) -> Node:
    """HOME: amd64, the service is build-only, so compose resolves the default name."""
    node = Node(tmp_path, "x86_64", None)
    node.state["tags"][node.ref] = AMD
    return node


@pytest.fixture
def aws(tmp_path: Path) -> Node:
    """AWS: arm64, the compose file names the image."""
    node = Node(tmp_path, "aarch64", "quant-execution-engine:aws-arm64")
    node.state["containers"][0]["image"] = ARM
    node.state["tags"][node.ref] = ARM
    return node


# ---- the refusals ------------------------------------------------------------------------------


def test_home_refuses_an_arm64_candidate_and_changes_nothing(home: Node) -> None:
    home.state["tags"]["quant-execution-engine:aws-arm64-738890b"] = ARM
    r = home.run("--candidate", "quant-execution-engine:aws-arm64-738890b")
    assert r.returncode == 3, r.stderr
    assert "REFUSED" in r.stderr and "linux/arm64" in r.stderr and "nothing was changed" in r.stderr
    assert home.mutating_calls() == []
    after = home.load()
    assert after["tags"][home.ref] == AMD
    assert after["containers"][0]["image"] == AMD


def test_home_refuses_a_clobbered_compose_tag(home: Node) -> None:
    """The 2026-07-13 shape: an arm64 build landed on the name HOME's compose resolves."""
    home.state["tags"][home.ref] = ARM
    r = home.run()
    assert r.returncode == 3, r.stderr
    assert "compose image" in r.stderr
    assert home.mutating_calls() == []
    assert home.load()["containers"][0]["image"] == AMD


def test_aws_refuses_an_amd64_candidate_and_changes_nothing(aws: Node) -> None:
    aws.state["tags"]["quant-execution-engine-execution-engine:latest"] = AMD
    r = aws.run("--candidate", "quant-execution-engine-execution-engine:latest")
    assert r.returncode == 3, r.stderr
    assert "this node is linux/arm64" in r.stderr
    assert aws.mutating_calls() == []
    assert aws.load()["containers"][0]["image"] == ARM


def test_a_non_linux_image_is_refused(home: Node) -> None:
    home.state["tags"]["odd:1"] = WINDOWS
    r = home.run("--candidate", "odd:1")
    assert r.returncode == 3
    assert home.mutating_calls() == []


def test_a_refusal_is_also_raised_in_dry_run(home: Node) -> None:
    home.state["tags"]["quant-execution-engine:aws-arm64"] = ARM
    r = home.run("--candidate", "quant-execution-engine:aws-arm64", "--dry-run")
    assert r.returncode == 3


# ---- what is not a refusal ---------------------------------------------------------------------


def test_an_absent_image_is_a_precondition_failure_not_an_arch_refusal(home: Node) -> None:
    r = home.run("--candidate", "nope:1")
    assert r.returncode == 2
    assert "not present" in r.stderr and "REFUSED" not in r.stderr
    assert home.mutating_calls() == []


def test_no_container_means_no_deploy(home: Node) -> None:
    home.state["containers"][0]["labels"]["com.docker.compose.project.working_dir"] = "/elsewhere"
    r = home.run("--dry-run")
    assert r.returncode == 2
    assert "found 0" in r.stderr


@pytest.mark.parametrize(("node_arch", "image"), [("x86_64", AMD), ("aarch64", ARM)])
def test_kernel_and_docker_spellings_of_one_arch_match(
    tmp_path: Path, node_arch: str, image: str
) -> None:
    node = Node(tmp_path, node_arch, None)
    node.state["tags"][node.ref] = image
    node.state["containers"][0]["image"] = image
    r = node.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "architecture OK" in r.stdout


def test_dry_run_passes_the_checks_and_changes_nothing(home: Node) -> None:
    home.state["tags"]["quant-execution-engine-execution-engine:new"] = AMD_NEW
    r = home.run("--candidate", "quant-execution-engine-execution-engine:new", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout and "would tag" in r.stdout
    assert home.mutating_calls() == []


# ---- the deploy itself -------------------------------------------------------------------------


def test_a_matching_candidate_deploys_with_a_rollback_tag(home: Node) -> None:
    home.state["tags"]["quant-execution-engine-execution-engine:new"] = AMD_NEW
    r = home.run("--candidate", "quant-execution-engine-execution-engine:new")
    assert r.returncode == 0, r.stderr
    after = home.load()
    assert after["containers"][0]["image"] == AMD_NEW
    rollback = [t for t in after["tags"] if ":rollback-" in t]
    assert len(rollback) == 1 and after["tags"][rollback[0]] == AMD
    assert f"rollback: scripts/deploy.sh --candidate {rollback[0]}" in r.stdout
    kinds = [c[0] if c[0] == "tag" else "up" for c in home.mutating_calls()]
    assert kinds == ["tag", "tag", "up"]


def test_recreate_uses_the_containers_own_compose_files_and_never_builds_or_pulls(
    aws: Node,
) -> None:
    aws.state["tags"]["quant-execution-engine:aws-arm64-new"] = ARM
    r = aws.run("--candidate", "quant-execution-engine:aws-arm64-new")
    assert r.returncode == 0, r.stderr
    up = next(c for c in aws.calls() if c[:1] == ["compose"] and "up" in c)
    files = [up[i + 1] for i, a in enumerate(up) if a == "-f"]
    assert files == [str(f) for f in aws.files]
    assert up[up.index("-p") + 1] == "quant-execution-engine"
    for flag in ("--no-build", "--force-recreate", "--no-deps", "--wait"):
        assert flag in up
    assert up[up.index("--pull") + 1] == "never"
    assert up[-1] == "execution-engine"


def test_the_same_image_is_a_config_recreate_without_a_rollback_tag(home: Node) -> None:
    r = home.run()
    assert r.returncode == 0, r.stderr
    assert "configuration, not code" in r.stdout
    assert [c for c in home.calls() if c[:1] == ["tag"]] == []


def test_a_failed_recreate_prints_the_rollback(home: Node) -> None:
    home.state["tags"]["quant-execution-engine-execution-engine:new"] = AMD_NEW
    home.state["up_result"] = "fail"
    r = home.run("--candidate", "quant-execution-engine-execution-engine:new")
    assert r.returncode == 4
    assert (
        "Roll back: scripts/deploy.sh --candidate quant-execution-engine-execution-engine:rollback-"
        in r.stderr
    )


def test_the_post_check_catches_a_container_on_the_wrong_image(home: Node) -> None:
    home.state["tags"]["quant-execution-engine-execution-engine:new"] = AMD_NEW
    home.state["up_result"] = "wrong_image"
    r = home.run("--candidate", "quant-execution-engine-execution-engine:new")
    assert r.returncode == 4
    assert "is not the target" in r.stderr
