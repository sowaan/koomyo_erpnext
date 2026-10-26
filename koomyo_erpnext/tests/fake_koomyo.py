"""A stand-in for the Koomyo API in tests. No network.

Built from the docs examples in tests/fixtures/. `FakeKoomyo` behaves like one Koomyo
instance; `patch_koomyo(*fakes)` routes every KoomyoClient call to the fake whose
base URL matches, so two connections can talk to two "servers" that reuse the same ids.
"""

import copy
import json
import os
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str):
	with open(os.path.join(FIXTURES, name + ".json")) as fh:
		return json.load(fh)


class FakeResponse:
	def __init__(self, status_code=200, body=None, headers=None, raw_text=None):
		self.status_code = status_code
		self._body = body
		self._raw_text = raw_text
		self.headers = {"X-Request-Id": "req_test", "Koomyo-Api-Version": "v1", **(headers or {})}
		self.reason = "Test"

	def json(self):
		if self._raw_text is not None:
			return json.loads(self._raw_text)
		return copy.deepcopy(self._body)


def error_response(status, code, message="Test error", headers=None):
	return FakeResponse(
		status, {"error": {"code": code, "message": message, "request_id": "req_err"}}, headers
	)


class FakeKoomyo:
	def __init__(self, base_url: str, *, tenant_id: str | None = None, currency: str = "PKR", scopes=None):
		self.base_url = base_url.rstrip("/")
		self.account = load_fixture("account")
		if tenant_id:
			self.account["tenant_id"] = tenant_id
		self.account["currency"] = currency
		if scopes is not None:
			self.account["key"]["scopes"] = list(scopes)
		self.branches = load_fixture("branches")["data"]
		self.events: list[dict] = []
		self.down = False
		# path -> list of FakeResponse to return first (then normal routing)
		self.scripted: dict[str, list[FakeResponse]] = {}
		self.calls: list[dict] = []

	@property
	def tenant_id(self):
		return self.account["tenant_id"]

	def add_event(self, template: str = "event_shift_opened", **overrides) -> dict:
		event = load_fixture(template)
		event["tenant_id"] = self.tenant_id
		event.update(overrides)
		self.events.append(event)
		return event

	def handle(self, method, url, params=None, json_body=None, headers=None):
		path = urlsplit(url).path[len(urlsplit(self.base_url).path) :]
		self.calls.append(
			{"method": method, "path": path, "params": dict(params or {}), "headers": dict(headers or {})}
		)
		if self.down:
			raise requests.ConnectionError("fake koomyo is down")
		if self.scripted.get(path):
			return self.scripted[path].pop(0)
		if path == "/account":
			return FakeResponse(200, self.account)
		if path == "/branches":
			return FakeResponse(
				200, {"object": "list", "data": self.branches, "has_more": False, "next_cursor": None}
			)
		if path == "/events":
			return self._events(params or {})
		return error_response(404, "not_found")

	def _events(self, params):
		after = int(params.get("after") or 0)
		limit = int(params.get("limit") or 100)
		types = set(params["types"].split(",")) if params.get("types") else None
		since = params.get("since")
		rows = [e for e in sorted(self.events, key=lambda e: e["seq"]) if e["seq"] > after]
		if types:
			rows = [e for e in rows if e["type"] in types]
		if since:
			rows = [e for e in rows if e["created"][:10] >= since[:10]]
		page = rows[:limit]
		return FakeResponse(
			200,
			{
				"object": "list",
				"data": page,
				"has_more": len(rows) > limit,
				"next_after": page[-1]["seq"] if page else after,
			},
		)


class _Router:
	"""Plays requests.Session for KoomyoClient and forwards to the matching FakeKoomyo."""

	def __init__(self, fakes):
		self.fakes = fakes

	def request(self, method, url, params=None, json=None, headers=None, **_kwargs):
		for fake in self.fakes:
			if url.startswith(fake.base_url + "/"):
				return fake.handle(method, url, params, json, headers)
		raise requests.ConnectionError(f"no fake Koomyo at {url}")


@contextmanager
def patch_koomyo(*fakes: FakeKoomyo):
	with patch("koomyo_erpnext.client.requests.Session", lambda: _Router(list(fakes))):
		yield
