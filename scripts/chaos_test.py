#!/usr/bin/env python3
"""Automated chaos gate: break the running system and require it to heal itself.

For each scenario we inject a fault through the healer API, then poll until the
healer reports an incident correlated with that experiment as `recovered`, and the
service is fully healthy again. Any failure, escalation or timeout fails the run.

Standard library only, so it runs on a bare Jenkins agent.

    python scripts/chaos_test.py --url http://localhost:30080 --scenarios kill_pod,crash,errors,outage
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

SERVICES = ["orders", "payments", "inventory"]


def call(url: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def wait_all_healthy(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = call(url, "/api/state")
        if state["services"] and all(s["status"] == "healthy" for s in state["services"]) and state["stats"]["open"] == 0:
            return
        time.sleep(2)
    raise TimeoutError("system did not settle into a healthy state")


def run_scenario(url: str, service: str, kind: str, timeout: float) -> dict:
    before = max((i["id"] for i in call(url, "/api/state")["incidents"]), default=0)
    injected = call(url, "/api/chaos", {"service": service, "kind": kind})
    started = time.time()
    print(f"  injected {kind:<10} -> {injected['detail']}", flush=True)

    while time.time() - started < timeout:
        state = call(url, "/api/state")
        related = [
            i for i in state["incidents"]
            if i["id"] > before and i["deployment"] == service and i["status"] != "superseded"
        ]
        bad = [i for i in related if i["status"] in ("failed", "escalated")]
        if bad:
            return {"service": service, "kind": kind, "ok": False, "reason": bad[0]["timeline"][-1]["message"]}
        svc = next(s for s in state["services"] if s["name"] == service)
        done = [i for i in related if i["status"] == "recovered"]
        if done and not any(i["status"] in ("detected", "remediating") for i in related) and svc["status"] == "healthy":
            first = min(done, key=lambda i: i["detected_at"])
            return {
                "service": service, "kind": kind, "ok": True,
                "incident": first["title"], "source": first["source"], "action": first["action"],
                "detect_seconds": first["detection_seconds"],
                "recover_seconds": max(i["recovery_seconds"] or 0 for i in done),
                "wall_seconds": round(time.time() - started, 1),
            }
        time.sleep(2)
    return {"service": service, "kind": kind, "ok": False, "reason": f"no verified recovery within {timeout:.0f}s"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:30080")
    parser.add_argument("--scenarios", default="kill_pod,crash,errors,leak,outage,bad_deploy")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    url = args.url.rstrip("/")

    print(f"chaos gate against {url}")
    wait_all_healthy(url, 180)
    results = []
    for index, kind in enumerate(k.strip() for k in args.scenarios.split(",") if k.strip()):
        service = SERVICES[index % len(SERVICES)]
        print(f"[{index + 1}] {kind} on {service}")
        result = run_scenario(url, service, kind, args.timeout)
        results.append(result)
        if result["ok"]:
            print(f"  PASS  {result['incident']} via {result['source']} -> {result['action']}; "
                  f"detected {result['detect_seconds'] or 0:.1f}s, recovered {result['recover_seconds']:.1f}s")
            wait_all_healthy(url, 120)
        else:
            print(f"  FAIL  {result['reason']}")

    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} scenarios healed automatically")
    if args.report:
        import os

        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w") as handle:
            json.dump({"results": results, "passed": passed, "total": len(results)}, handle, indent=2)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
