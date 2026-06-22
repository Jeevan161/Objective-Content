"""Reusable authenticated client for portal interactions.

Handles session creation, login, CSRF extraction, and generic GET. The target
environment (PROD/BETA) is chosen at construction time and exposed via
``self.config`` so callers can build environment-correct URLs.
"""
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from .constants import (
    DEFAULT_ENVIRONMENT,
    PortalConfig,
    USER_AGENT,
    CSRF_TOKEN_INPUT_NAME,
)

# Connection-pool size for the shared session. Sized generously so concurrent
# fetches (see portal.fetch) reuse connections instead of discarding them.
_POOL_SIZE = 32


class PortalClient:
    """Session-based client for one portal environment.

    Usage:
        client = PortalClient(environment="BETA")
        client.login()
        response = client.get(client.config.course_version_list_url, params={"q": cid})
    """

    def __init__(self, environment=DEFAULT_ENVIRONMENT):
        self.config = PortalConfig(environment)
        self.session = requests.Session()
        # requests.Session is safe to share across threads for concurrent GETs;
        # widen the pool so parallel fetches don't exhaust it.
        adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._logged_in = False

    @property
    def environment(self):
        return self.config.environment

    def login(self):
        """Login and establish a session. Returns True on success."""
        login_url = self.config.login_url
        headers = {"Referer": login_url, "User-Agent": USER_AGENT}

        login_page = self.session.get(login_url, headers=headers, allow_redirects=True)
        if login_page.status_code != 200:
            raise RuntimeError(f"Failed to load login page (status {login_page.status_code})")

        csrf_token = self._extract_csrf_from_html(login_page.text)
        if not csrf_token:
            raise RuntimeError("CSRF token not found on login page")

        login_data = {
            "username": self.config.username,
            "password": self.config.password,
            "csrfmiddlewaretoken": csrf_token,
        }
        self.session.post(login_url, data=login_data, headers=headers, allow_redirects=True)

        if "sessionid" in self.session.cookies.get_dict():
            self._logged_in = True
            return True

        raise RuntimeError(
            f"Login failed for {self.environment} - session cookie missing (check credentials)"
        )

    def get(self, url, **kwargs):
        """Authenticated GET. Returns the requests.Response."""
        self._ensure_logged_in()
        response = self.session.get(url, allow_redirects=True, **kwargs)
        response.raise_for_status()
        return response

    def _ensure_logged_in(self):
        if not self._logged_in:
            self.login()

    def _extract_csrf_from_html(self, html):
        soup = BeautifulSoup(html, "html.parser")
        csrf_input = soup.find("input", {"name": CSRF_TOKEN_INPUT_NAME})
        if csrf_input and csrf_input.get("value"):
            return csrf_input["value"]
        return None
