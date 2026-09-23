"""PolicyError maps to a compact {"error", "code"} JSON body."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.errors import PolicyError


def _app_raising(exc: Exception) -> TestClient:
    from broker.main import policy_error
    app = FastAPI()
    app.add_exception_handler(PolicyError, policy_error)

    @app.get("/boom")
    def boom():
        raise exc

    return TestClient(app)


def test_policy_error_body_is_compact(env):
    r = _app_raising(PolicyError(403, "out of grant", "out_of_grant")).get("/boom")
    assert r.status_code == 403
    assert r.json() == {"error": "out of grant", "code": "out_of_grant"}


def test_policy_error_code_defaults_from_status(env):
    r = _app_raising(PolicyError(404, "no such chat")).get("/boom")
    assert r.status_code == 404
    assert r.json() == {"error": "no such chat", "code": "not_found"}


def test_unknown_status_still_has_a_code():
    assert PolicyError(418, "teapot").code == "error"
