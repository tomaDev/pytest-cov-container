"""Ownership: which containers belong to this checkout and this xdist worker.

Regression cases come from a real incident: a nested git worktree
(``<root>/.claude/worktrees/<x>``) shares the root as a path prefix, and a
root-prefix check let two sessions harvest each other's coverage and reap
each other's containers.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pytest_cov_container import ownership
from pytest_cov_container.config import PluginConfig

ROOT = Path("/work/project")


def _attrs(*sources: str, env: tuple[str, ...] = ()) -> dict:
    return {
        "Mounts": [{"Source": source} for source in sources],
        "Config": {"Env": list(env), "Image": "public.ecr.aws/lambda/python:3.14-rapid"},
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"{ROOT}/.aws-sam/build/ApiFunction", True),
        (f"{ROOT}/.aws-sam/build/KBSyncFunction-Shared", True),
        # Nested worktree: shares the root prefix, is NOT ours.
        (f"{ROOT}/.claude/worktrees/other/.aws-sam/build/ApiFunction", False),
        # Sibling checkout whose path merely string-prefixes ours.
        (f"{ROOT}-crm/.aws-sam/build/ApiFunction", False),
        # The prefix dir itself is not "under" it.
        (f"{ROOT}/.aws-sam/build", False),
        ("/tmp/other/.aws-sam/build/ApiFunction", False),
    ],
)
def test_mounts_under_claims_only_this_checkouts_prefix(source, expected):
    assert ownership.mounts_under(_attrs(source), ROOT / ".aws-sam/build") is expected


def test_any_matching_mount_suffices():
    attrs = _attrs("/opt/layer", f"{ROOT}/.aws-sam/build/ApiFunction")
    assert ownership.mounts_under(attrs, ROOT / ".aws-sam/build")


def test_no_mounts_is_not_ours():
    assert not ownership.mounts_under({}, ROOT / ".aws-sam/build")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (("AWS_REGION=us-east-1", "MARK=gw1"), True),
        (("MARK=gw0",), False),
        (("MARK=gw10",), False),  # exact match: gw1 must not claim gw10
        (("AWS_REGION=us-east-1",), False),  # marker absent
    ],
)
def test_has_env_is_exact(env, expected):
    assert ownership.has_env(_attrs(env=env), "MARK", "gw1") is expected


@pytest.mark.parametrize(("env", "expected"), [({}, "main"), ({"PYTEST_XDIST_WORKER": "gw3"}, "gw3")])
def test_worker_id(monkeypatch, env, expected):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert ownership.worker_id() == expected


def _run_find_owned(monkeypatch, attrs_list, *, all_workers=False, **cfg_kwargs):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    backend = MagicMock()
    kept = []

    def find_containers(image_pattern, label, predicate):
        kept.extend(i for i, attrs in enumerate(attrs_list) if predicate(attrs))
        return kept

    backend.find_containers.side_effect = find_containers
    cfg = PluginConfig(image_pattern="public.ecr.aws/lambda/*", **cfg_kwargs)
    ownership.find_owned(backend, cfg, ROOT, all_workers=all_workers)
    assert backend.find_containers.call_args.kwargs["image_pattern"] == ["public.ecr.aws/lambda/*"]
    return kept


def test_find_owned_combines_mount_and_worker(monkeypatch):
    build = f"{ROOT}/.aws-sam/build/ApiFunction"
    attrs_list = [
        _attrs(build, env=("MARK=gw1",)),  # ours
        _attrs(build, env=("MARK=gw0",)),  # sibling worker
        _attrs("/elsewhere/.aws-sam/build/ApiFunction", env=("MARK=gw1",)),  # other checkout
    ]
    kept = _run_find_owned(monkeypatch, attrs_list, mount_prefix=".aws-sam/build", worker_env="MARK")
    assert kept == [0]


def test_find_owned_all_workers_keeps_the_mount_check(monkeypatch):
    build = f"{ROOT}/.aws-sam/build/ApiFunction"
    attrs_list = [
        _attrs(build, env=("MARK=gw1",)),
        _attrs(build, env=("MARK=gw0",)),
        _attrs(build),  # predates the marker
        _attrs("/elsewhere/.aws-sam/build/ApiFunction", env=("MARK=gw1",)),
    ]
    kept = _run_find_owned(
        monkeypatch, attrs_list, all_workers=True, mount_prefix=".aws-sam/build", worker_env="MARK"
    )
    assert kept == [0, 1, 2]


def test_find_owned_without_keys_keeps_every_image_match(monkeypatch):
    kept = _run_find_owned(monkeypatch, [_attrs(), _attrs()])
    assert kept == [0, 1]
