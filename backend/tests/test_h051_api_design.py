"""H051 M16: API Design consistency tests.

Verifies that every endpoint returning data uses a typed Pydantic response model,
uses ``status.HTTP_*`` constants for status codes, and serialises fields through
the schema rather than manually.
"""
from __future__ import annotations

import inspect
import io

import pytest
from fastapi.routing import APIRoute

from app.main import create_app


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def routes(app):
    return [r for r in app.routes if isinstance(r, APIRoute)]


# ---------------------------------------------------------------------------
# 1. No endpoint returns a bare dict — every one has a response_model
# ---------------------------------------------------------------------------


_DICT_RETURN_ALLOWLIST: set[str] = set()


class TestResponseModels:
    def test_no_endpoint_returns_bare_dict(self, routes):
        """Every endpoint should declare a Pydantic response_model, not return
        a raw dict."""
        violations = []
        for route in routes:
            endpoint = route.endpoint
            hints = getattr(endpoint, "__annotations__", {})
            ret = hints.get("return")
            if ret is None:
                continue
            ret_str = str(ret) if not isinstance(ret, type) else ret.__name__
            if "dict" in ret_str.lower() and route.name not in _DICT_RETURN_ALLOWLIST:
                violations.append(f"{route.methods} {route.path} -> {ret_str}")
        assert not violations, (
            f"Endpoints returning raw dict (should use a Pydantic model):\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# 2. Status codes use symbolic constants, not bare integers
# ---------------------------------------------------------------------------


class TestStatusCodeConstants:
    def test_uploads_uses_status_constants(self):
        from app.routers import uploads

        source = inspect.getsource(uploads)
        for code in ("status_code=400", "status_code=404", "status_code=503"):
            assert code not in source, (
                f"uploads.py uses bare {code} — should use status.HTTP_* constant"
            )

    def test_ai_generate_uses_status_constants(self):
        from app.routers import ai_generate

        source = inspect.getsource(ai_generate)
        for code in ("status_code=400", "status_code=503"):
            assert code not in source, (
                f"ai_generate.py uses bare {code} — should use status.HTTP_* constant"
            )


# ---------------------------------------------------------------------------
# 3. Upload endpoint returns a typed model, not dict
# ---------------------------------------------------------------------------

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestUploadResponseModel:
    def test_upload_returns_typed_model(self, client, auth):
        resp = client.post(
            "/api/v1/uploads",
            files={"file": ("cover.png", io.BytesIO(TINY_PNG), "image/png")},
            headers=auth,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert set(data.keys()) == {"url", "filename", "size"}
        assert isinstance(data["size"], int)

    def test_serve_image_validates_filename_length(self, client):
        long_name = "a" * 300 + ".png"
        resp = client.get(f"/api/v1/uploads/{long_name}")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 4. Viral endpoint serialises published_at as datetime, not string
# ---------------------------------------------------------------------------


class TestViralPublishedAtType:
    def test_published_at_is_datetime_field(self):
        from app.schemas.viral import PublicArticleOut

        field_info = PublicArticleOut.model_fields["published_at"]
        annotation = field_info.annotation
        ann_str = str(annotation)
        assert "datetime" in ann_str, (
            f"published_at should be datetime, got {ann_str}"
        )


# ---------------------------------------------------------------------------
# 5. AI generate endpoint doesn't shadow the response parameter
# ---------------------------------------------------------------------------


class TestAiGenerateNoShadow:
    def test_no_response_variable_shadow(self):
        from app.routers import ai_generate

        source = inspect.getsource(ai_generate.generate_fields)
        assert "response: dict" not in source, (
            "ai_generate.generate_fields shadows the 'response' parameter"
        )


# ---------------------------------------------------------------------------
# 6. Trigger check_now and inbound have response_model
# ---------------------------------------------------------------------------


class TestTriggerResponseModels:
    def test_check_now_has_response_model(self, routes):
        check_routes = [
            r for r in routes
            if "/check" in r.path and "trigger" in r.path and "POST" in r.methods
        ]
        for route in check_routes:
            assert route.response_model is not None, (
                f"{route.path} has no response_model"
            )

    def test_inbound_has_response_model(self, routes):
        inbound_routes = [
            r for r in routes
            if "/inbound/" in r.path and "POST" in r.methods
        ]
        for route in inbound_routes:
            assert route.response_model is not None, (
                f"{route.path} has no response_model"
            )


# ---------------------------------------------------------------------------
# 7. Calendar cadence endpoint returns typed model, not list[dict]
# ---------------------------------------------------------------------------


class TestCalendarCadenceModel:
    def test_cadence_guide_has_typed_response(self, routes):
        cadence_routes = [
            r for r in routes
            if r.path.endswith("/cadence") and "GET" in r.methods
        ]
        for route in cadence_routes:
            model = route.response_model
            assert model is not None, f"{route.path} has no response_model"
            model_str = str(model)
            assert "dict" not in model_str.lower(), (
                f"{route.path} response_model should not be list[dict]: {model_str}"
            )
