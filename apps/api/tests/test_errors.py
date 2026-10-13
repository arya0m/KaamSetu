"""P1-foundation: every error response uses {"error": {"code", "message"}}."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import AppError, register_error_handlers


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/app-error")
    def app_error() -> None:
        raise AppError("conflict", "Already exists", status_code=409)

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("database password is hunter2")

    @app.get("/items/{item_id}")
    def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    return app


client = TestClient(_app(), raise_server_exceptions=False)


def test_p1_not_found_uses_error_shape() -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_p1_method_not_allowed_uses_error_shape() -> None:
    resp = client.post("/crash")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"


def test_p1_validation_error_uses_error_shape() -> None:
    resp = client.get("/items/abc")
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert "path.item_id" in error["message"]


def test_p1_app_error_uses_its_code_and_status() -> None:
    resp = client.get("/app-error")
    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "conflict", "message": "Already exists"}}


def test_p1_unhandled_error_is_500_without_leaking_details() -> None:
    resp = client.get("/crash")
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}
    assert "hunter2" not in resp.text


def test_p1_real_app_404_uses_error_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
