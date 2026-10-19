"""Build application links without discarding deployment mount paths."""

from urllib.parse import urlsplit


def append_path(base, path):
    return base.rstrip('/') + '/' + path.lstrip('/')


def public_application_url(base, path):
    """Return an explicit HTTP(S) application link, or None for unsafe bases."""
    if not isinstance(base, str) or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char in '\\?#'
        for char in base
    ):
        return None
    try:
        url = urlsplit(base)
        if (
            url.scheme not in ('http', 'https')
            or not url.hostname
            or url.username is not None
            or url.password is not None
        ):
            return None
        # Accessing port also validates malformed/out-of-range port numbers.
        url.port
    except ValueError:
        return None
    return append_path(base, path)
