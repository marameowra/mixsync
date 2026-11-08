from urllib.parse import urlsplit

# object-src stays 'self': covers render as <object data="/media/..."> (same-origin)
CSP = (
    "default-src 'self'; object-src 'self'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def csp_with_form_target(url: str) -> str:
    """CSP that also lets a form post to `url`'s origin (the one off-origin form: the MB seed)."""
    u = urlsplit(url)
    return CSP.replace("form-action 'self'", f"form-action 'self' {u.scheme}://{u.netloc}")
