"""The spec describes what the server actually serves.

A hand-maintained spec is a second source of truth that drifts the first time
somebody adds an endpoint and forgets. A spec describing a route the server
does not serve is worse than none: a generated client fails at runtime with a
404 that looks like a server fault.

So the drift is a test failure.
"""

from __future__ import annotations

import json

from romarr.openapi import DESCRIPTIONS, UNDOCUMENTED, served_routes, spec


def test_every_served_route_is_described():
    missing = sorted(served_routes() - set(DESCRIPTIONS) - UNDOCUMENTED)
    assert not missing, (
        "these routes are served but undocumented:\n  " + "\n  ".join(missing))


def test_nothing_is_described_that_is_not_served():
    """The direction that produces a client which 404s."""
    served = served_routes()
    # `/` and `/metrics` are matched before the route table, so they are
    # served without appearing as a `route.path ==` comparison.
    special = {"/", "/metrics", "/api/v1/openapi.json"}
    extra = sorted(set(DESCRIPTIONS) - served - special)
    assert not extra, "described but not served: " + ", ".join(extra)


def test_the_spec_is_valid_enough_to_generate_from():
    document = spec("1.2.3")
    assert document["openapi"].startswith("3.")
    assert document["info"]["version"] == "1.2.3"
    assert document["paths"]
    for path, operations in document["paths"].items():
        assert path.startswith("/")
        for method, operation in operations.items():
            assert method in ("get", "post", "put", "delete")
            assert operation["summary"]
            assert operation["operationId"]
            assert "200" in operation["responses"]


def test_operation_ids_are_unique():
    """Duplicates make a generated client with two methods of the same name,
    and which one you get depends on the generator."""
    ids = [op["operationId"]
           for ops in spec()["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids))


def test_the_security_scheme_matches_how_auth_actually_works():
    document = spec()
    schemes = document["components"]["securitySchemes"]
    assert schemes["ApiKey"]["name"] == "X-Api-Key"
    assert document["security"]


def test_the_spec_serialises():
    json.dumps(spec())
