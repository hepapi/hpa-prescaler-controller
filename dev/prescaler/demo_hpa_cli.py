#!/usr/bin/env python3
"""Show / set autoscaling helm params on local demo Argo CD apps.

Plain Application (demo-app) is updated via argocd_updater.
ApplicationSet children are updated by patching the ApplicationSet generator
values so the AppSet controller does not revert the change.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from argocd_updater import (  # noqa: E402
    ArgoAppUpdateStatus,
    find_helm_source,
    get_argocd_app,
    update_argocd_app,
)

DEMO_APP = "demo-app"
DEMO_APPSET_APPS = ("demo-appset-a", "demo-appset-b")
DEMO_APPS = (DEMO_APP, *DEMO_APPSET_APPS)
APPSET_NAME = "demo-appset"
ARGOCD_NS = "argocd"
KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-hpa-prescaler")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger("demo-hpa")


def _kubectl_json(*args: str) -> dict:
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-o", "json", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _kubectl(*args: str) -> None:
    subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _autoscale_values(app_name: str) -> tuple[str, str] | None:
    app, status = get_argocd_app(app_name, logger)
    if status != ArgoAppUpdateStatus.SUCCESS:
        return None
    source = find_helm_source(app.get("spec") or {})
    if not source:
        return None
    params = (source.get("helm") or {}).get("parameters") or []
    values = {p.get("name"): p.get("value") for p in params}
    return (
        str(values.get("autoscaling.minReplicas", "?")),
        str(values.get("autoscaling.maxReplicas", "?")),
    )


def _wait_apps(min_replicas: int, max_replicas: int, timeout_secs: int = 60) -> bool:
    import time

    want = (str(min_replicas), str(max_replicas))
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        current = {name: _autoscale_values(name) for name in DEMO_APPS}
        if all(v == want for v in current.values()):
            return True
        time.sleep(2)
    return False


def _patch_appset(min_replicas: int, max_replicas: int) -> None:
    appset = _kubectl_json("get", "applicationset", APPSET_NAME, "-n", ARGOCD_NS)
    elements = appset["spec"]["generators"][0]["list"]["elements"]
    for el in elements:
        el["minReplicas"] = str(min_replicas)
        el["maxReplicas"] = str(max_replicas)
    tmp = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", "-"],
        input=json.dumps(appset),
        check=True,
        capture_output=True,
        text=True,
    )
    if tmp.returncode != 0:
        raise RuntimeError(tmp.stderr)


def cmd_show(_: argparse.Namespace) -> int:
    print("Argo CD app autoscaling (helm parameters):")
    missing = False
    for name in DEMO_APPS:
        vals = _autoscale_values(name)
        if vals is None:
            print(f"  {name:16}  (not found)")
            missing = True
            continue
        mn, mx = vals
        print(f"  {name:16}  min={mn}  max={mx}")
    return 1 if missing else 0


def cmd_set(args: argparse.Namespace) -> int:
    config = {"minReplicas": args.min_replicas, "maxReplicas": args.max_replicas}
    failed = False

    ok, status = update_argocd_app(DEMO_APP, config, logger)
    if not ok:
        print(f"  FAIL {DEMO_APP}: {status.value}")
        failed = True
    else:
        print(f"  OK   {DEMO_APP}: min={args.min_replicas} max={args.max_replicas}")

    try:
        _patch_appset(args.min_replicas, args.max_replicas)
        print(
            f"  OK   ApplicationSet/{APPSET_NAME}: "
            f"minReplicas={args.min_replicas} maxReplicas={args.max_replicas}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL ApplicationSet/{APPSET_NAME}: {exc}")
        failed = True

    if not _wait_apps(args.min_replicas, args.max_replicas):
        print("  WARN timed out waiting for all demo apps to reach target min/max")
        failed = True
        for name in DEMO_APPS:
            vals = _autoscale_values(name)
            print(f"       {name}: {vals}")
    else:
        for name in DEMO_APPSET_APPS:
            print(f"  OK   {name}: min={args.min_replicas} max={args.max_replicas}")

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", help="Print current min/max for demo apps")
    p_show.set_defaults(func=cmd_show)

    p_set = sub.add_parser("set", help="Set min/max on demo apps (and AppSet template)")
    p_set.add_argument("min_replicas", type=int)
    p_set.add_argument("max_replicas", type=int)
    p_set.set_defaults(func=cmd_set)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
