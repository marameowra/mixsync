from fastapi.testclient import TestClient

from mixsync.app import create_app

CSP = (
    "default-src 'self'; object-src 'self'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def test_csp_on_pages_static_errors_fragments_and_redirects() -> None:
    c = TestClient(create_app(), follow_redirects=False)
    responses = {
        "login page": c.get("/login"),
        "static file": c.get("/static/app.css"),
        "404": c.get("/no-such-page"),
        "fragment 404": c.get("/no-such-page", headers={"HX-Request": "true"}),
        "redirect": c.get("/"),
        "json route": c.get("/healthz"),
    }
    assert responses["login page"].status_code == 200
    assert responses["static file"].status_code == 200
    assert responses["404"].status_code == 404
    assert responses["redirect"].status_code == 303
    for kind, r in responses.items():
        assert r.headers.get("content-security-policy") == CSP, kind
