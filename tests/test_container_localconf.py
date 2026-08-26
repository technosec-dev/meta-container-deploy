#!/usr/bin/env python3
"""Logic tests for container-localconf.bbclass.

The bbclass cannot be imported (it is BitBake metadata, not a Python module), so
these tests extract the pure-Python helper `_resolve_moving_tag_digest` and
exercise it with stubs. The property that matters most is DETERMINISM: when a
moving tag's digest cannot be resolved at parse time, the resolver must return
None (so the caller can mark the pull task nostamp) and must never read a
per-parse value like DATETIME, which would make the pull task's basehash change
on reparse and fail the build with "the metadata is not deterministic".

Run: python3 tests/test_container_localconf.py
"""
import os
import re
import subprocess
import sys
import types

BBCLASS = os.path.join(os.path.dirname(__file__), "..", "classes", "container-localconf.bbclass")


def load_resolver():
    src = open(BBCLASS).read()
    m = re.search(r"^def _resolve_moving_tag_digest\(.*?(?=^\S)", src, re.S | re.M)
    if not m:
        raise AssertionError("_resolve_moving_tag_digest not found in bbclass")
    ns = {"bb": types.SimpleNamespace(note=lambda *a, **k: None)}
    exec(m.group(0), ns)
    return ns


def make_gcv(vars_):
    def get_container_var(d, name, suffix, default=None):
        return vars_.get(suffix, default if default is not None else "")
    return get_container_var


class StrictD:
    """Fails if the resolver reads any per-parse value: that would be non-deterministic."""

    def getVar(self, k):
        raise AssertionError("resolver read d.getVar(%r): non-deterministic" % k)


def main():
    ns = load_resolver()
    resolve = ns["_resolve_moving_tag_digest"]
    orig_run = subprocess.run
    checks = []

    def check(name, cond):
        checks.append(cond)
        print(("PASS" if cond else "FAIL"), "-", name)

    # Immutable references carry no freshness key.
    ns["get_container_var"] = make_gcv({"DIGEST": "sha256:abc"})
    check("pinned digest returns ''", resolve(StrictD(), "c", "amd64") == "")
    ns["get_container_var"] = make_gcv({"OCI_ARCHIVE": "/img.tar"})
    check("oci archive returns ''", resolve(StrictD(), "c", "amd64") == "")

    # skopeo resolves -> the digest (change-only pulls).
    ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/x/y:devel"})
    subprocess.run = lambda args, **k: types.SimpleNamespace(stdout="sha256:deadbeef\n")
    try:
        check("skopeo success returns the digest", resolve(StrictD(), "c", "amd64") == "sha256:deadbeef")
    finally:
        subprocess.run = orig_run

    # skopeo missing -> None, deterministic across reparses (the fix).
    ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/x/y:devel"})

    def _oserr(args, **k):
        raise OSError("skopeo: not found")

    subprocess.run = _oserr
    try:
        r1 = resolve(StrictD(), "c", "amd64")
        r2 = resolve(StrictD(), "c", "amd64")
    finally:
        subprocess.run = orig_run
    check("skopeo missing returns None (not a timestamp)", r1 is None)
    check("unresolved is deterministic across reparses", r1 is None and r2 is None)

    # Private image with no parse-time auth (the cloud case) -> None, not a failure.
    ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/mistral-dev/x:devel"})

    def _cpe(args, **k):
        raise subprocess.CalledProcessError(1, args, stderr="unauthorized")

    subprocess.run = _cpe
    try:
        check("skopeo auth error returns None", resolve(StrictD(), "c", "amd64") is None)
    finally:
        subprocess.run = orig_run

    print()
    if all(checks):
        print("ALL %d CHECKS PASS" % len(checks))
        return 0
    print("SOME CHECKS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
