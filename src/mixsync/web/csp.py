from urllib.parse import urlsplit

# covers render as <img src="/media/..."> (same-origin, allowed by default-src)
CSP = (
    "default-src 'self'; object-src 'self'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def csp_with_form_target(url: str) -> str:
    """CSP that also lets a form post to `url`'s origin (the one off-origin form: the MB seed)."""
    u = urlsplit(url)
    return CSP.replace("form-action 'self'", f"form-action 'self' {u.scheme}://{u.netloc}")
