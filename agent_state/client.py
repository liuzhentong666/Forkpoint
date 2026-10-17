"""Bounded HTTP read/publish interface for local maintenance and remote state."""
import json
import os
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from .engine import identifier
from .objects import encode
from .storage import StateError, Missing, Conflict, Invalid, TooLarge, Corrupt, BudgetError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StateClient:
    """No disk state, implicit retries, credential forwarding or version policy."""
    def __init__(self, base_url, token=None, timeout=45):
        url = urlsplit(base_url)
        if (url.scheme not in {"https", "http"} or not url.hostname or
                url.username is not None or url.password is not None or
                url.path not in {"", "/"} or url.query or url.fragment or
                (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})):
            raise Invalid("State URL must be an HTTPS origin or loopback HTTP origin")
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self.token = token if token is not None else os.environ.get("AGENTDB_STATE_TOKEN")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _request(self, method, path, body=None):
        data = encode(body) if body is not None else None
        if data is not None and len(data) > 1500000:
            raise TooLarge("State request exceeds HTTP size limit")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                raw = response.read(262145)
            if len(raw) > 262144:
                raise TooLarge("State response exceeds 256 KiB")
        except (URLError, OSError, HTTPException) as error:
            raise StateError("State service unreachable; publication outcome may be unknown. "
                             "Reconnect and check state or retry the identical command.") from error
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise StateError("State service returned a non-JSON response") from error
        if not isinstance(value, dict):
            raise StateError("State service returned an invalid response")
        if status != 200:
            errors = {(404, Missing.code): Missing, (409, Conflict.code): Conflict,
                      (400, Invalid.code): Invalid, (422, Invalid.code): Invalid,
                      (422, TooLarge.code): TooLarge, (422, BudgetError.code): BudgetError,
                      (500, Corrupt.code): Corrupt}
            error_type = errors.get((status, value.get("error")), StateError)
            raise error_type(value.get("message") or f"State service HTTP {status}")
        return value

    def read(self, task_id, fresh=False):
        path = "/v1/tasks/" + identifier(task_id, "task_id")
        return self._request("GET", path + ("?fresh=true" if fresh else ""))

    def publish(self, task_id, **command):
        method = "POST" if command.get("expected_version") is None else "PUT"
        return self._request(method, "/v1/tasks/" + identifier(task_id, "task_id"), command)
