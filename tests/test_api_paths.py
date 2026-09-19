from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from pyfog.api import artifact_route_path, bearer_token, health_live, integer_header, iso_utc


def request_with_headers(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )


def test_artifact_route_accepts_bios_boot_sector():
    assert artifact_route_path("boot-sector.bin") == "boot-sector.bin"


def test_artifact_route_rejects_unscoped_paths():
    with pytest.raises(HTTPException):
        artifact_route_path("metadata.json")


def test_api_helpers_normalize_headers_dates_and_integer_fields():
    assert bearer_token(request_with_headers(authorization="Bearer agent-token")) == "agent-token"
    assert bearer_token(request_with_headers(authorization="Basic agent-token")) == ""
    assert bearer_token(request_with_headers(authorization="Bearer bad\nvalue")) == ""
    assert bearer_token(request_with_headers(authorization=f"Bearer {'x' * 257}")) == ""
    assert iso_utc(datetime(2026, 9, 19, 12, 30, tzinfo=UTC)) == "2026-09-19T12:30:00+00:00Z"
    assert integer_header("42", "X-Chunk") == 42
    with pytest.raises(HTTPException, match="X-Chunk"):
        integer_header("not-a-number", "X-Chunk")
    with pytest.raises(HTTPException, match="X-Chunk"):
        integer_header(None, "X-Chunk")
    assert health_live() == {"status": "ok", "service": "pyfog"}


def test_artifact_route_accepts_only_scoped_relative_paths():
    assert artifact_route_path("partitions/root.img") == "partitions/root.img"
    with pytest.raises(HTTPException):
        artifact_route_path("partitions/../secret")
    with pytest.raises(HTTPException):
        artifact_route_path("partitions/" + "x" * 241)
