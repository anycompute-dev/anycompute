"""Read account metadata with the protected CI credentials and report only fixed fields."""

import json
import os
import sys
from contextlib import closing
from http.client import HTTPSConnection
from math import inf

from anycompute.contract import AuthError, ProviderError
from anycompute.providers import daytona
from tests.conformance.runner import GRACE_S, MAX_CREATES, MAX_TTL_S


_SECRET_NAMES = {"boat": "BOAT_API_KEY", "e2b": "E2B_API_KEY", "daytona": "DAYTONA_API_KEY"}
_METADATA = {
    "boat": ("boat.dev", "/api/v1/limits", "Authorization", "Bearer "),
    "e2b": ("api.e2b.app", "/default-templates", "X-API-Key", ""),
}
_MAX_RESPONSE = 128 * 1024


def _metadata(provider, key):
    host, path, header, prefix = _METADATA[provider]
    # A fixed destination and no redirects keep each key at its provider.
    with closing(HTTPSConnection(host, timeout=15)) as conn:
        conn.request("GET", path, headers={header: prefix + key, "Accept": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            return response.status, None
        body = response.read(_MAX_RESPONSE + 1)
    if len(body) > _MAX_RESPONSE:
        raise ValueError("Account metadata exceeds the response limit")
    return response.status, json.loads(body)


def _object(value):
    return value if isinstance(value, dict) else {}


def _boat(payload):
    if payload.get("ok") is not True or payload.get("type") != "limits.info" or type(payload.get("canStart")) is not bool:
        return {"access": "unverified"}
    starts = _object(_object(payload.get("starts")).get("day")).get("remaining")
    hours = _object(payload.get("planIncluded")).get("vmHoursRemaining")
    suite_hours = MAX_CREATES * (MAX_TTL_S + GRACE_S) / 3600
    return {
        "access": "verified",
        "can_start": payload["canStart"],
        "daily_starts_cover_suite": starts >= MAX_CREATES if type(starts) is int and starts >= 0 else None,
        "included_vm_time_covers_suite": hours >= suite_hours if type(hours) in (int, float) and 0 <= hours < inf else None,
    }


def _e2b(templates):
    if not isinstance(templates, list):
        return {"access": "unverified"}
    base = [
        row for row in templates if isinstance(row, dict) and any(
            isinstance(row.get(field), list) and "base" in row[field] for field in ("aliases", "names")
        )
    ]
    result = {"access": "verified", "base_template": "unverified"}
    if len(base) != 1:
        return result
    cpu, mem = base[0].get("cpuCount"), base[0].get("memoryMB")
    if type(cpu) is int and type(mem) is int and cpu > 0 and mem > 0:
        result.update(base_template="within reservation" if cpu <= 2 and mem <= 4096 else "exceeds reservation",
                      vcpus=cpu, memory_mb=mem)
    return result


def _daytona():
    snapshot = daytona._call("inspect snapshot", lambda client, _: client.snapshot.get(daytona._PROJECT_SNAPSHOT),
                             project=True)
    result = {"access": "verified", "snapshot_matches": False, "matching_warm_pool": "unverified"}
    try:
        snapshot_id = daytona._checked_snapshot_id(snapshot)
    except ProviderError:
        return result
    result["snapshot_matches"] = True
    try:
        pools = daytona._call("inspect warm pools", lambda client, _: client.warm_pool.list(), project=True)
        if isinstance(pools, list) and all(
            isinstance(pool.target, str) and isinstance(pool.snapshot, str) for pool in pools
        ):
            matches = any(
                pool.target == daytona._PROJECT_TARGET and pool.snapshot in (snapshot_id, snapshot.id, snapshot.name)
                for pool in pools
            )
            result["matching_warm_pool"] = "present" if matches else "absent"
    except Exception:
        # This optional lookup can require a different permission from the snapshot read.
        pass
    return result


def _inspect(provider, key):
    try:
        if provider == "daytona":
            return _daytona()
        status, payload = _metadata(provider, key)
        if status != 200:
            return {"access": "denied" if status in (401, 403) else "unverified", "http_status": status}
        return _boat(_object(payload)) if provider == "boat" else _e2b(payload)
    except AuthError:
        return {"access": "denied"}
    except Exception:
        # Provider responses and transport errors must stay out of public CI logs.
        return {"access": "unverified"}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in _SECRET_NAMES:
        raise SystemExit("Select boat, e2b, or daytona.")
    provider = sys.argv[1]
    key = os.getenv(_SECRET_NAMES[provider])
    result = _inspect(provider, key) if key else {"access": "missing key"}
    print("Cloud account result:", json.dumps({"provider": provider, **result}))
    return 0 if result["access"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
