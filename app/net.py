"""Shared HTTP access: connections are reused (one session per thread) so repeated calls to the same
site skip the TCP/TLS handshake, GET requests get a couple of safe retries on gateway errors, and no
cookies are kept between requests."""
import threading
from http.cookiejar import DefaultCookiePolicy

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        retry = Retry(total=2, connect=2, read=0, backoff_factor=0.4, status_forcelist=(502, 503, 504),
                      allowed_methods=frozenset({"GET", "HEAD"}), raise_on_status=False)
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # never store cookies
        _local.session = session
    return session


def get(url: str, **kwargs) -> requests.Response:
    return _session().get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return _session().post(url, **kwargs)


def delete(url: str, **kwargs) -> requests.Response:
    return _session().delete(url, **kwargs)
