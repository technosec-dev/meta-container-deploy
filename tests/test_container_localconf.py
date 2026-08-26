#!/usr/bin/env python3
"""Logic tests for container-localconf.bbclass.

The bbclass cannot be imported (it is BitBake metadata, not a Python module), so
these tests extract the pure-Python helper `_is_moving_tag` and exercise it with
stubs. The property that matters most is DETERMINISM: the moving-vs-immutable
decision must come from local.conf variables alone and must never depend on a
registry lookup, a subprocess, or any per-parse value. A parse-time skopeo call
(the previous approach) made the pull task's basehash change between reparses
whenever skopeo's availability or the registry's state changed, failing the build
with "the metadata is not deterministic".

Run: python3 tests/test_container_localconf.py
"""
import os
import re
import subprocess
import sys
import types

BBCLASS = os.path.join(os.path.dirname(__file__), "..", "classes", "container-localconf.bbclass")


def load_func(name):
    src = open(BBCLASS).read()
    m = re.search(r"^def %s\(.*?(?=^\S)" % re.escape(name), src, re.S | re.M)
    if not m:
        raise AssertionError("%s not found in bbclass" % name)
    ns = {"bb": types.SimpleNamespace(note=lambda *a, **k: None)}
    exec(m.group(0), ns)
    return ns


def make_gcv(vars_):
    def get_container_var(d, name, suffix, default=None):
        return vars_.get(suffix, default if default is not None else "")
    return get_container_var


class StrictD:
    """Fails if the function reads any per-parse value or datastore var directly."""

    def getVar(self, k):
        raise AssertionError("read d.getVar(%r) directly: non-deterministic" % k)


def main():
    ns = load_func("_is_moving_tag")
    is_moving = ns["_is_moving_tag"]
    checks = []

    def check(name, cond):
        checks.append(cond)
        print(("PASS" if cond else "FAIL"), "-", name)

    # A digest-pinned or archive-backed reference is immutable: cached, reproducible
    # pull, so NOT a moving tag.
    ns["get_container_var"] = make_gcv({"DIGEST": "sha256:abc"})
    check("pinned digest is not moving", is_moving(StrictD(), "c") is False)
    ns["get_container_var"] = make_gcv({"OCI_ARCHIVE": "/img.tar"})
    check("oci archive is not moving", is_moving(StrictD(), "c") is False)
    # Digest wins even if an archive is also somehow set.
    ns["get_container_var"] = make_gcv({"DIGEST": "sha256:abc", "IMAGE": "ghcr.io/x/y:devel"})
    check("pinned digest with an image tag is still not moving", is_moving(StrictD(), "c") is False)

    # A plain tag is mutable: always-pull (the caller sets nostamp).
    ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/x/y:devel"})
    check("plain tag is moving", is_moving(StrictD(), "c") is True)
    ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/mistral-dev/x:devel"})
    check("private plain tag is moving", is_moving(StrictD(), "c") is True)

    # DETERMINISM: the decision is a pure function of local.conf, identical across
    # reparses. This is the regression the fix closes: it must not vary with a
    # network lookup, and it must never invoke a subprocess at parse time.
    ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/x/y:devel"})
    r1 = is_moving(StrictD(), "c")
    r2 = is_moving(StrictD(), "c")
    check("moving decision is stable across reparses", r1 is True and r2 is True)

    orig_run = subprocess.run

    def _boom(*a, **k):
        raise AssertionError("_is_moving_tag ran a subprocess at parse: non-deterministic")

    subprocess.run = _boom
    try:
        ns["get_container_var"] = make_gcv({"IMAGE": "ghcr.io/x/y:devel"})
        moving = is_moving(StrictD(), "c")
        check("no subprocess at parse time (moving tag)", moving is True)
        ns["get_container_var"] = make_gcv({"DIGEST": "sha256:abc"})
        pinned = is_moving(StrictD(), "c")
        check("no subprocess at parse time (pinned)", pinned is False)
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
