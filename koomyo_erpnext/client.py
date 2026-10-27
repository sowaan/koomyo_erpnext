"""The one HTTP client that talks to the Koomyo public API.

Contract: docs.koomyo.com (conventions, reference/errors, reference/rate-limits) and openapi.json.

- Auth: `Authorization: Bearer <key>`; the key is never logged or put in an error.
- Timeouts on every call.
- Retries with back-off for network errors and 5xx; waits `Retry-After` on 429 and
  a moment on 409 `request_in_progress`. No retry on any other 4xx.
- Every answer is checked: JSON object, error body shape on failure.
- Lists page with the opaque `next_cursor`, passed back untouched.
"""

import random
import time
from collections.abc import Iterator
from urllib.parse import quote, urlsplit

import frappe
import requests
from frappe import _

from koomyo_erpnext.exceptions import KoomyoAPIError
from koomyo_erpnext.utils import redact_text

DEFAULT_BASE_URL = "https://api.koomyo.com/api/public/v1"
API_PATH = "/api/public/v1"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_ATTEMPTS = 4
# A 429 asking us to wait longer than this is handed back to the caller (a job retries later)
# instead of holding a worker.
MAX_INLINE_WAIT = 30


def validate_base_url(url: str) -> str:
	"""Return the normalised base URL or raise frappe.ValidationError with a plain message."""
	url = (url or "").strip().rstrip("/")
	parts = urlsplit(url)
	host = (parts.hostname or "").lower()
	if parts.scheme not in ("https", "http") or not host:
		frappe.throw(
			_("The Koomyo API address must start with https://, for example {0}").format(DEFAULT_BASE_URL)
		)
	if parts.scheme == "http" and host not in LOCAL_HOSTS:
		frappe.throw(
			_("The Koomyo API address must use https. Plain http is only allowed for a local test server.")
		)
	if parts.username or parts.password:
		frappe.throw(_("The Koomyo API address must not contain a user name or password."))
	if parts.query or parts.fragment:
		frappe.throw(_("The Koomyo API address must not contain ? or #."))
	if not parts.path.endswith(API_PATH):
		frappe.throw(
			_("The Koomyo API address must end with {0}, for example {1}").format(API_PATH, DEFAULT_BASE_URL)
		)
	return url


def logger():
	return frappe.logger("koomyo_erpnext", allow_site=True)


class KoomyoClient:
	def __init__(
		self,
		base_url: str,
		api_key: str,
		*,
		session: requests.Session | None = None,
		max_attempts: int = MAX_ATTEMPTS,
		max_inline_wait: int = MAX_INLINE_WAIT,
		sleep=time.sleep,
		label: str = "",
	):
		if not api_key:
			raise KoomyoAPIError(None, "missing_api_key", _("No API key is saved for this connection."))
		self.base_url = base_url.rstrip("/")
		self._api_key = api_key
		self.session = session or requests.Session()
		self.max_attempts = max_attempts
		self.max_inline_wait = max_inline_wait
		self.sleep = sleep
		self.label = label
		self.last_rate_limit: dict = {}

	def __repr__(self):
		# Never show the key, even in a debugger or a traceback.
		return f"<KoomyoClient {self.label or self.base_url}>"

	@classmethod
	def for_connection(cls, connection) -> "KoomyoClient":
		"""Build a client from a Koomyo Connection document (or its name)."""
		if isinstance(connection, str):
			connection = frappe.get_doc("Koomyo Connection", connection)
		api_key = connection.get_password("api_key", raise_exception=False)
		return cls(connection.base_url, api_key, label=connection.name)

	# ------------------------------------------------------------------ calls

	def get(self, path: str, params: dict | None = None) -> dict:
		return self.request("GET", path, params=params)

	def request(
		self,
		method: str,
		path: str,
		*,
		params: dict | None = None,
		json: dict | None = None,
		idempotency_key: str | None = None,
	) -> dict:
		if not path.startswith("/"):
			raise ValueError("path must start with /")
		url = self.base_url + path
		headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
		if json is not None:
			headers["Content-Type"] = "application/json"
		if idempotency_key:
			headers["Idempotency-Key"] = idempotency_key

		attempt = 0
		while True:
			attempt += 1
			started = time.monotonic()
			try:
				response = self.session.request(
					method,
					url,
					params=params,
					json=json,
					headers=headers,
					timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
					allow_redirects=False,
				)
			except requests.RequestException as e:
				error = KoomyoAPIError(
					None, "network_error", _("Could not reach Koomyo: {0}").format(type(e).__name__)
				)
				self._log(method, path, None, None, started, error.code)
				if attempt >= self.max_attempts:
					raise error
				self.sleep(self._backoff(attempt))
				continue

			request_id = response.headers.get("X-Request-Id")
			self._remember_rate_limit(response)
			self._log(method, path, response.status_code, request_id, started)

			if 200 <= response.status_code < 300:
				return self._parse_success(response, request_id)

			error = self._parse_error(response, request_id)
			if not error.is_retryable or attempt >= self.max_attempts:
				raise error
			if error.code == "rate_limited":
				wait = error.retry_after if error.retry_after is not None else 2
				if wait > self.max_inline_wait:
					raise error
				self.sleep(wait + random.random())
			elif error.code == "request_in_progress":
				self.sleep(2)
			else:
				self.sleep(self._backoff(attempt))

	def iter_list(
		self, path: str, params: dict | None = None, max_pages: int | None = None
	) -> Iterator[dict]:
		"""Yield every record of a cursor-paged list. The cursor is passed back as given."""
		params = dict(params or {})
		pages = 0
		while True:
			page = self.get(path, params)
			check_list_page(page, cursor_field="next_cursor")
			yield from page["data"]
			pages += 1
			if not page["has_more"] or (max_pages and pages >= max_pages):
				return
			if not page.get("next_cursor"):
				raise KoomyoAPIError(
					None, "bad_response", _("Koomyo said more pages follow but sent no cursor.")
				)
			params["cursor"] = page["next_cursor"]

	# --------------------------------------------------------- named calls

	def get_account(self) -> dict:
		account = self.get("/account")
		check_account(account)
		return account

	def list_branches(self) -> list[dict]:
		return list(self.iter_list("/branches"))

	def get_events(
		self,
		*,
		after: int | None = None,
		since: str | None = None,
		types: list[str] | None = None,
		branch_id: str | None = None,
		limit: int = 500,
	) -> dict:
		"""One page of `GET /events` (docs: events/feed). Pages by `next_after`, not a cursor."""
		params: dict = {"limit": limit}
		if after is not None:
			params["after"] = int(after)
		if since:
			params["since"] = since
		if types:
			params["types"] = ",".join(types)
		if branch_id:
			params["branch_id"] = branch_id
		page = self.get("/events", params)
		check_list_page(page, cursor_field="next_after")
		if not isinstance(page["next_after"], int) or isinstance(page["next_after"], bool):
			raise KoomyoAPIError(
				None, "bad_response", _("Koomyo sent a page of events without a number to continue from.")
			)
		check_feed_position(page)
		return page

	def get_event(self, event_id: str) -> dict:
		return self.get("/events/" + quote(event_id, safe=""))

	# ------------------------------------------------------------ helpers

	@staticmethod
	def _backoff(attempt: int) -> float:
		return min(2 ** (attempt - 1), 8) + random.random()

	def _remember_rate_limit(self, response):
		limit = response.headers.get("X-RateLimit-Limit")
		remaining = response.headers.get("X-RateLimit-Remaining")
		if limit is not None or remaining is not None:
			self.last_rate_limit = {"limit": limit, "remaining": remaining}

	def _parse_success(self, response, request_id) -> dict:
		try:
			body = response.json()
		except ValueError:
			raise KoomyoAPIError(
				response.status_code,
				"bad_response",
				_("Koomyo answered with something that is not JSON."),
				request_id,
			)
		if not isinstance(body, dict):
			raise KoomyoAPIError(
				response.status_code,
				"bad_response",
				_("Koomyo answered with an unexpected shape."),
				request_id,
			)
		return body

	@staticmethod
	def _parse_error(response, request_id) -> KoomyoAPIError:
		code, message = f"http_{response.status_code}", response.reason or _("Request failed")
		try:
			body = response.json()
			err = body.get("error") if isinstance(body, dict) else None
			if isinstance(err, dict):
				code = str(err.get("code") or code)
				message = str(err.get("message") or message)
				request_id = err.get("request_id") or request_id
		except ValueError:
			pass
		retry_after = None
		if response.status_code == 429:
			try:
				retry_after = max(0, int(response.headers.get("Retry-After", "")))
			except ValueError:
				retry_after = None
			if code.startswith("http_"):
				code = "rate_limited"
		if 300 <= response.status_code < 400:
			code, message = (
				"unexpected_redirect",
				_("Koomyo answered with a redirect. Check the API address."),
			)
		return KoomyoAPIError(response.status_code, code, redact_text(message)[:500], request_id, retry_after)

	def _log(self, method, path, status, request_id, started, error_code=None):
		ms = int((time.monotonic() - started) * 1000)
		logger().info(
			"koomyo %s %s %s status=%s request_id=%s ms=%s%s",
			self.label,
			method,
			path,
			status if status is not None else "-",
			request_id or "-",
			ms,
			f" error={error_code}" if error_code else "",
		)


# ---------------------------------------------------------------- shape checks


def check_list_page(page: dict, cursor_field: str):
	if (
		page.get("object") != "list"
		or not isinstance(page.get("data"), list)
		or not isinstance(page.get("has_more"), bool)
	):
		raise KoomyoAPIError(None, "bad_response", _("Koomyo sent a list in an unexpected shape."))
	if cursor_field not in page:
		raise KoomyoAPIError(None, "bad_response", _("Koomyo sent a list without {0}.").format(cursor_field))
	if not all(isinstance(row, dict) for row in page["data"]):
		raise KoomyoAPIError(None, "bad_response", _("Koomyo sent a list with unexpected rows."))


def check_account(account: dict):
	"""Required fields of the Account schema (openapi.json components.schemas.Account)."""
	key = account.get("key")
	ok = (
		account.get("object") == "account"
		and all(
			isinstance(account.get(f), str) and account.get(f)
			for f in ("tenant_id", "name", "currency", "timezone")
		)
		and isinstance(key, dict)
		and isinstance(key.get("scopes"), list)
		and all(isinstance(s, str) for s in key["scopes"])
	)
	if not ok:
		raise KoomyoAPIError(None, "bad_response", _("Koomyo sent account details in an unexpected shape."))


def check_feed_position(page: dict):
	"""`oldest_seq`, `latest_seq` and `gap` say where the feed starts (docs: events/feed#how-long-events-are-kept).

	An answer from a Koomyo that does not send them has no gap signal: it is read as no gap, never guessed.
	"""
	for key in ("oldest_seq", "latest_seq"):
		value = page.get(key)
		if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
			raise KoomyoAPIError(None, "bad_response", _("Koomyo sent {0} that is not a number.").format(key))
		page.setdefault(key, None)
	if not isinstance(page.setdefault("gap", False), bool):
		raise KoomyoAPIError(None, "bad_response", _("Koomyo sent gap that is not true or false."))


EVENT_REQUIRED = ("id", "type", "seq", "tenant_id", "data")


def check_event(event) -> str | None:
	"""Return None if `event` has the envelope shape we rely on, else a reason (docs: events, schema Event)."""
	if not isinstance(event, dict):
		return "event is not an object"
	for field in EVENT_REQUIRED:
		if field not in event:
			return f"event has no {field}"
	if not isinstance(event["id"], str) or not event["id"] or len(event["id"]) > 140:
		return "event id is not valid"
	if not isinstance(event["type"], str) or not event["type"] or len(event["type"]) > 140:
		return "event type is not valid"
	if not isinstance(event["seq"], int) or isinstance(event["seq"], bool) or event["seq"] < 0:
		return "event seq is not a number"
	if not isinstance(event["data"], dict):
		return "event data is not an object"
	return None
