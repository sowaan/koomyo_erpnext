"""A stand-in for the Koomyo API in tests. No network.

Built from the docs examples in tests/fixtures/. `FakeKoomyo` behaves like one Koomyo
instance; `patch_koomyo(*fakes)` routes every KoomyoClient call to the fake whose
base URL matches, so two connections can talk to two "servers" that reuse the same ids.
"""

import base64
import copy
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str):
	with open(os.path.join(FIXTURES, name + ".json")) as fh:
		return json.load(fh)


def _decimal(value):
	from decimal import Decimal

	return Decimal(str(value))


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


# Docs: reference/stock-movement-reasons. A reason that is not a system code is free text, always manual.
MANUAL_REASONS = ("stock_count", "non_sale_usage", "opening_stock")
SYSTEM_REASONS = (
	"order_deduction",
	"order_cancel_restore",
	"packaging",
	"po_received",
	"production_consume",
	"production_output",
	"transfer_out",
	"transfer_in",
	"transfer_cancelled",
	"transfer_return",
	"transfer_loss",
	"transfer_shrinkage",
	"import_undo",
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
		self.orders: dict[str, dict] = {}
		self.ingredients: dict[str, dict] = {}
		self.suppliers: dict[str, dict] = {}
		self.purchase_orders: dict[str, dict] = {}
		self.receive_calls: list[dict] = []
		self.cancel_calls: list[dict] = []
		self.mark_calls: list[dict] = []
		self.patch_calls: list[dict] = []
		self.reverse_calls: list[dict] = []
		self.restate_calls: list[dict] = []
		self.return_calls: list[dict] = []
		self.close_calls: list[dict] = []
		self.stock_records: dict[
			str, dict
		] = {}  # adjustments, waste, supply issues and counts made through POST
		self.stock_calls: list[tuple] = []  # (path, body) of each accepted stock write
		self.warehouse_records: dict[str, dict] = {}  # warehouses made through POST /warehouses
		self.branch_limited_key = False  # True plays a key limited to one branch (403 place_not_allowed)
		self.supplies_on = True  # False plays a Koomyo whose Supplies switch is off
		self.transfers: dict[str, dict] = {}  # stock transfers made through POST /stock-transfers
		self.transfer_calls: list[tuple] = []  # (action, body) of each accepted transfer call
		self.destination_required = False
		self.cost_rule = "ask"  # the restaurant's ingredient cost rule: ask, auto or off
		self.distributed: set[str] = set()  # delivery ids part of which went on by transfer
		self.returned: set[str] = set()  # delivery ids part of which went back to the supplier
		self.below_zero = False
		self.stale_once = False  # the next PATCH finds the order changed (409 stale_version)
		self.places_incomplete = False  # mark-ordered answers 409 delivery_places_incomplete
		self.purchase_bodies: list[dict] = []  # every POST /purchase-orders body that was accepted
		self.handled_by = "external"  # who orders purchase requests, as GET /account says
		self.account_purchase_requests = True  # False plays a Koomyo whose /account has no purchase_requests
		self.purchase_requests_off = False  # GET /purchase-requests answers 403 feature_not_available
		self.unsettled_days: set[str] = set()  # GET /stock-usage answers 409 day_not_settled for these
		self.refs: list[dict] = []
		self.deleted_refs: list[tuple] = []  # (entity_type, id) of each DELETE /refs call
		self.writes: list[dict] = []  # every write call: method, path, key, body
		# GET list endpoints for the backfill: path -> records (opaque cursors, docs filters).
		self.lists: dict[str, list[dict]] = {}
		self.page_cap = 200
		self.rate_limit: tuple[int, int] | None = None  # (limit, remaining) headers on every answer
		self._idempotent: dict[str, tuple] = {}
		self.down = False
		# The feed's start, as GET /events answers it. By default the oldest event it holds and no gap;
		# set `deleted_through` to drop events up to that number (30-day retention), `head` for latest_seq.
		self.deleted_through: int | None = None
		self.head: int | None = None
		self.send_position = True  # False plays a Koomyo that does not answer oldest_seq/latest_seq/gap
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

	def add_ingredient(
		self, ingredient_id: str, name: str, unit: str, cost: str = "12.5000", refs=None
	) -> dict:
		self.ingredients[ingredient_id] = {
			"id": ingredient_id,
			"object": "ingredient",
			"name": name,
			"unit": unit,
			"cost_per_unit": cost,
			"currency": "PKR",
			"low_stock_threshold": "0",
			"active": True,
			"made_in_house": False,
			"created_at": "2026-10-01T00:00:00Z",
			"updated_at": "2026-10-01T00:00:00Z",
			"external_refs": refs or {},
		}
		return self.ingredients[ingredient_id]

	def handle(self, method, url, params=None, json_body=None, headers=None):
		path = urlsplit(url).path[len(urlsplit(self.base_url).path) :]
		self.calls.append(
			{"method": method, "path": path, "params": dict(params or {}), "headers": dict(headers or {})}
		)
		if self.down:
			raise requests.ConnectionError("fake koomyo is down")
		if self.scripted.get(path):
			return self.scripted[path].pop(0)
		if method in ("POST", "PATCH", "PUT", "DELETE"):
			return self._write(method, path, json_body, (headers or {}).get("Idempotency-Key"))
		if path.startswith("/purchase-orders/") and path[len("/purchase-orders/") :] in self.purchase_orders:
			return FakeResponse(200, self.purchase_orders[path[len("/purchase-orders/") :]])
		if (
			path.startswith("/stock-transfers/")
			and path.count("/") == 2
			and path.split("/")[2] in self.transfers
		):
			return FakeResponse(200, self.transfers[path.split("/")[2]])
		if path.startswith("/purchase-requests"):
			if self.purchase_requests_off:
				return error_response(403, "feature_not_available", "purchase requests are not enabled")
			if path.startswith("/purchase-requests/"):
				for card in self.lists.get("/purchase-requests", []):
					if card["id"] == path.split("/")[2]:
						return FakeResponse(200, card)
				return error_response(404, "not_found")
			if (params or {}).get("system") and (params or {}).get("external_id"):
				rows = [
					c
					for c in self.lists.get("/purchase-requests", [])
					if (c.get("external_refs") or {}).get(params["system"]) == params["external_id"]
				]
				return FakeResponse(
					200, {"object": "list", "data": rows, "has_more": False, "next_cursor": None}
				)
		if path == "/stock-usage":
			day = (params or {}).get("business_date")
			if not day:
				return error_response(400, "invalid_parameter", "business_date is required")
			if day in self.unsettled_days:
				return error_response(409, "day_not_settled", "the day is not over yet")
		if path.startswith("/suppliers/"):
			sid = path.split("/")[2]
			for card in [*self.lists.get("/suppliers", []), *self.suppliers.values()]:
				if card["id"] == sid:
					return FakeResponse(200, card)
			return error_response(404, "not_found")
		if path.startswith("/option-groups/"):
			for group in self.lists.get("/option-groups", []):
				if group["id"] == path.split("/")[2]:
					return FakeResponse(200, group)
			return error_response(404, "not_found")
		if path == "/account":
			account = copy.deepcopy(self.account)
			if self.account_purchase_requests:
				account["purchase_requests"] = {
					"enabled": not self.purchase_requests_off,
					"handled_by": None if self.purchase_requests_off else self.handled_by,
				}
			return FakeResponse(200, account)
		if path == "/branches":
			return FakeResponse(
				200, {"object": "list", "data": self.branches, "has_more": False, "next_cursor": None}
			)
		if path == "/events":
			return self._events(params or {})
		if path in self.lists:
			return self._list(path, params or {})
		if path.startswith("/ingredients/") and path[len("/ingredients/") :] in self.ingredients:
			return FakeResponse(200, self.ingredients[path[len("/ingredients/") :]])
		if path.startswith("/orders/") and path[len("/orders/") :] in self.orders:
			return FakeResponse(200, self.orders[path[len("/orders/") :]])
		return error_response(404, "not_found")

	def _list(self, path, params):
		"""A paged list as the docs describe it: ascending, opaque next_cursor, filters."""
		rows = list(self.lists[path])

		def when(r, *fields):
			return next((r.get(f) for f in fields if r.get(f)), "") or ""

		filters = {
			"business_date": lambda r, v: r.get("business_date") == v,
			"payment_status": lambda r, v: (r.get("payment") or {}).get("status") == v,
			"status": lambda r, v: r.get("status") in v.split(","),
			"reason": lambda r, v: r.get("reason") == v,
			"reference_id": lambda r, v: r.get("reference_id") == v,
			"active": lambda r, v: str(r.get("active", True)).lower() == v,
			"kind": lambda r, v: (r.get("reason") in MANUAL_REASONS or r.get("reason") not in SYSTEM_REASONS)
			== (v == "manual"),
			"branch_id": lambda r, v: v in (r.get("branch_id"), (r.get("location") or {}).get("id")),
			"created_after": lambda r, v: when(r, "created_at") > v,
			"created_before": lambda r, v: when(r, "created_at") < v,
			"opened_after": lambda r, v: when(r, "opened_at") > v,
			"opened_before": lambda r, v: when(r, "opened_at") < v,
			"sent_after": lambda r, v: when(r, "sent_at") > v,
			"updated_since": lambda r, v: when(r, "updated_at") >= v,
		}
		for key, test in filters.items():
			if params.get(key) is not None:
				rows = [r for r in rows if test(r, str(params[key]))]
		offset = 0
		if params.get("cursor"):
			try:
				offset = int(base64.b64decode(params["cursor"]).decode().split(":")[1])
			except Exception:
				return error_response(400, "invalid_cursor")
		limit = min(int(params.get("limit") or 50), self.page_cap)
		page = rows[offset : offset + limit]
		more = offset + limit < len(rows)
		cursor = base64.b64encode(f"o:{offset + limit}:{path}".encode()).decode() if more else None
		return FakeResponse(
			200, {"object": "list", "data": copy.deepcopy(page), "has_more": more, "next_cursor": cursor}
		)

	def _write(self, method, path, body, key):
		"""Writes as the docs describe them, with Idempotency-Key replay."""
		self.writes.append({"method": method, "path": path, "key": key, "body": copy.deepcopy(body)})
		if key and key in self._idempotent:
			old_body, response = self._idempotent[key]
			if old_body != body:
				return error_response(422, "idempotency_key_reused")
			response.headers["Idempotent-Replayed"] = "true"
			return response
		response = self._do_write(method, path, body or {})
		if key and response.status_code < 500:
			self._idempotent[key] = (copy.deepcopy(body), response)
		return response

	def _do_write(self, method, path, body):
		def ref_key(b):
			ref = b.get("external_ref") or {}
			return (ref.get("system"), ref.get("external_id")) if ref else None

		if method == "POST" and path == "/suppliers":
			for supplier in self.suppliers.values():
				if ref_key(body) and supplier["_ref"] == ref_key(body):
					supplier.update({k: v for k, v in body.items() if k != "external_ref"})
					return FakeResponse(200, supplier)
			sid = f"sup-{len(self.suppliers) + 1}"
			supplier = {
				"id": sid,
				"object": "supplier",
				"name": body.get("name"),
				"_ref": ref_key(body),
				"phone": body.get("phone"),
				"email": body.get("email"),
				"active": body.get("active", True),
				"external_refs": {body["external_ref"]["system"]: body["external_ref"]["external_id"]}
				if body.get("external_ref")
				else {},
			}
			self.suppliers[sid] = supplier
			return FakeResponse(201, supplier)
		if method == "POST" and path == "/purchase-orders":
			for order in self.purchase_orders.values():
				if ref_key(body) and order["_ref"] == ref_key(body):
					return FakeResponse(200, order)
			if body.get("supplier_id") not in self.suppliers:
				return error_response(400, "invalid_parameter", "supplier_id")
			allocated = self._allocations(body)
			if isinstance(allocated, FakeResponse):
				return allocated
			priced = self._price(body)
			if isinstance(priced, FakeResponse):
				return priced
			pid = f"po-{len(self.purchase_orders) + 1}"
			order = {
				"id": pid,
				"object": "purchase_order",
				"status": "draft",
				"_ref": ref_key(body),
				"supplier": {"id": body["supplier_id"], "name": self.suppliers[body["supplier_id"]]["name"]},
				"branch_id": body.get("branch_id"),
				"currency": "PKR",
				"business_date": "2026-10-12",
				"external_refs": {body["external_ref"]["system"]: body["external_ref"]["external_id"]}
				if body.get("external_ref")
				else {},
				"number": f"PO-FAKE-{len(self.purchase_orders) + 1:04d}",
				"created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
				"updated_at": "2026-10-12T10:00:00Z",
				"receipts": [],
				"lines": [
					{
						"id": f"{pid}-line-{i}",
						"ingredient_id": line["ingredient_id"],
						"quantity_ordered": line["quantity_ordered"],
						"quantity_received": "0",
						"unit_cost": line["unit_cost"],
						"allocations": allocated[i],
					}
					for i, line in enumerate(body.get("lines") or [])
				],
			}
			order.update(priced)
			self.purchase_bodies.append(body)
			self.purchase_orders[pid] = order
			return FakeResponse(201, order)
		if method == "PATCH" and path.startswith("/purchase-orders/") and path.count("/") == 2:
			return self._patch_order(path.split("/")[2], body or {})
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/mark-ordered"):
			order = self.purchase_orders.get(path.split("/")[2])
			if not order:
				return error_response(404, "not_found")
			if order["status"] != "draft":
				return error_response(409, "not_a_draft", "only a draft can be marked")
			sent = (body or {}).get("updated_at")
			if sent and sent != order["updated_at"]:
				return error_response(409, "stale_version", "the order changed")
			if self.places_incomplete:
				return error_response(409, "delivery_places_incomplete", "the shares are incomplete")
			self.mark_calls.append(body or {})
			order["status"] = "ordered"
			order["updated_at"] = "2026-10-12T10:30:00Z"
			if (body or {}).get("expected_date"):
				order["expected_date"] = body["expected_date"]
			return FakeResponse(200, order)
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/close-short"):
			return self._close_short(path.split("/")[2], body or {})
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/returns"):
			return self._returns(path.split("/")[2], body or {})
		if method == "POST" and "/receipts/" in path and path.endswith("/restate-costs"):
			parts = path.split("/")
			return self._restate(parts[2], parts[4], body or {})
		if method == "POST" and "/receipts/" in path and path.endswith("/reverse"):
			parts = path.split("/")
			return self._reverse(parts[2], parts[4], body or {})
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/cancel"):
			return self._cancel(path.split("/")[2], body or {})
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/receive"):
			return self._receive(path.split("/")[2], body or {})
		if path in ("/stock-adjustments", "/waste", "/supply-issues", "/stock-counts") and method == "POST":
			return self._stock_create(path, body or {})
		if path.startswith(("/supply-issues/", "/stock-counts/")) and method in ("POST", "PUT"):
			return self._stock_action(path, body or {})
		if path == "/stock-transfers" and method == "POST":
			return self._transfer_create(body or {})
		if path.startswith("/stock-transfers/"):
			parts = path.split("/")
			if method == "GET" and len(parts) == 3:
				found = self.transfers.get(parts[2])
				return FakeResponse(200, found) if found else error_response(404, "not_found")
			if method == "POST" and len(parts) == 4:
				return self._transfer_action(parts[2], parts[3], body or {})
		if path.startswith("/ingredients") or path.startswith("/warehouses"):
			answer = self._ingredient_or_warehouse(method, path, body)
			if answer is not None:
				return answer
		if method == "PATCH" and path.startswith("/ingredients/"):
			ingredient = self.ingredients.get(path.split("/")[2])
			if not ingredient:
				return error_response(404, "not_found")
			ingredient["cost_per_unit"] = body["cost_per_unit"]
			return FakeResponse(200, ingredient)
		if method == "DELETE" and path.startswith("/refs/"):
			_, _, entity_type, entity_id = path.split("/")
			self.deleted_refs.append((entity_type, entity_id))
			if entity_type == "purchase_order" and entity_id in self.purchase_orders:
				order = self.purchase_orders[entity_id]
				order["external_refs"] = {}
				order["_ref"] = None
			return FakeResponse(200, {"object": "external_ref", "deleted": True})
		if method == "PUT" and path.startswith("/refs/"):
			_, _, entity_type, entity_id = path.split("/")
			ref = {"object": "external_ref", "entity_type": entity_type, "entity_id": entity_id, **body}
			self.refs.append(ref)
			return FakeResponse(200, ref)
		return error_response(404, "not_found")

	def _price(self, body):
		"""The order's tax, discounts and charges as Koomyo calculates them (simplified), or a 400 for a bad input."""
		from decimal import ROUND_HALF_UP, Decimal

		def num(value, low=0, high=None):
			try:
				number = Decimal(str(value))
			except Exception:
				return None
			if number < low or (high is not None and number > high):
				return None
			return number

		bad = lambda why: error_response(400, "invalid_parameter", why)  # noqa: E731
		cents = Decimal("0.01")
		rate = num(body.get("tax_rate", 0), 0, 100)
		if rate is None:
			return bad("tax_rate")
		mode = body.get("tax_mode", "exclusive")
		if mode not in ("exclusive", "inclusive"):
			return bad("tax_mode")
		charges = body.get("charges") or []
		if len(charges) > 20:
			return bad("charges")
		items = Decimal(0)
		for line in body.get("lines") or []:
			discount = num(line.get("discount_percent", 0), 0, 100)
			if discount is None or num(line.get("tax_percent", 0), 0, 100) is None:
				return bad("line")
			items += Decimal(line["quantity_ordered"]) * Decimal(line["unit_cost"]) * (100 - discount) / 100
		tax = (items * rate / 100) if mode == "exclusive" else (items - items / (1 + rate / 100))
		tax = tax.quantize(cents, rounding=ROUND_HALF_UP)
		out_charges, added, withheld = [], Decimal(0), Decimal(0)
		for charge in charges:
			label = charge.get("label")
			if not isinstance(label, str) or not 1 <= len(label) <= 60:
				return bad("label")
			if charge.get("spread_over_cost", "none") not in ("none", "value", "quantity"):
				return bad("spread_over_cost")
			if charge.get("kind", "amount") not in ("amount", "percent"):
				return bad("kind")
			if charge.get("role", "charge") not in ("charge", "withholding"):
				return bad("role")
			value = num(charge.get("value", 0), 0)
			if value is None:
				return bad("value")
			amount = value if charge.get("kind", "amount") == "amount" else items * value / 100
			amount = amount.quantize(cents, rounding=ROUND_HALF_UP)
			deduct = charge.get("direction") == "deduct" or charge.get("role") == "withholding"
			if charge.get("role") == "withholding":
				withheld += amount
			else:
				added += -amount if deduct else amount
			out_charges.append({**charge, "amount": str(-amount if deduct else amount), "tax_amount": "0.00"})
		total = items + (tax if mode == "exclusive" else 0) + added - withheld
		return {
			"tax_name": body.get("tax_name"),
			"tax_rate": str(rate),
			"tax_mode": mode,
			"tax_recoverable": body.get("tax_recoverable", True),
			"tax_total": str(tax),
			"items_without_tax": str(items.quantize(cents)),
			"withholding_total": str(-withheld),
			"charges": out_charges,
			"total_ordered": str(total.quantize(cents, rounding=ROUND_HALF_UP)),
		}

	def _allocations(self, body):
		"""Checks the shares of an order the way the docs say (one transaction: a refusal changes nothing)."""
		from decimal import Decimal

		requests = self.lists.get("/purchase-requests", [])
		by_line = {l["id"]: (card, l) for card in requests for l in card["lines"]}
		out, taken = [], {}
		for index, line in enumerate(body.get("lines") or []):
			shares = line.get("allocations") or []
			made = []
			if shares:
				if self.purchase_requests_off:
					return error_response(403, "feature_not_available", "purchase requests are not enabled")
				if sum(Decimal(s["quantity"]) for s in shares) != Decimal(line["quantity_ordered"]):
					return error_response(
						400, "invalid_parameter", "the shares do not add up to quantity_ordered"
					)
			for number, share in enumerate(shares):
				card, request_line = by_line.get(share.get("request_line_id"), (None, None))
				if not request_line:
					return error_response(400, "invalid_parameter", "unknown request line")
				if request_line["ingredient"]["id"] != line["ingredient_id"]:
					return error_response(400, "invalid_parameter", "request line of another ingredient")
				if request_line["status"] not in ("approved", "ordered"):
					return error_response(
						409, "request_line_not_orderable", "the request line is not approved"
					)
				room = Decimal(request_line["approved_quantity"]) - Decimal(request_line["ordered_quantity"])
				room -= taken.get(request_line["id"], Decimal(0))
				if Decimal(share["quantity"]) > room:
					return error_response(409, "request_quantity_exceeded", "more than approved")
				taken[request_line["id"]] = taken.get(request_line["id"], Decimal(0)) + Decimal(
					share["quantity"]
				)
				kind = share.get("destination_type") or (
					"warehouse" if body.get("deliver_to") == "warehouse" else "branch"
				)
				warehouse = next((w for w in self.lists.get("/warehouses", []) if w.get("is_main")), None)
				made.append(
					{
						"id": f"alloc-{index}-{number}",
						"branch_id": card["branch_id"],
						"branch_name": card["branch_name"],
						"quantity": share["quantity"],
						"destination_type": kind,
						"destination_id": (share.get("destination_id") or (warehouse or {}).get("id"))
						if kind == "warehouse"
						else card["branch_id"],
						"request_line_id": request_line["id"],
						"request_id": card["id"],
						"request_number": card.get("number"),
						"received_quantity": "0",
						"landed_quantity": "0",
						"distributed_quantity": "0",
						"lost_quantity": "0",
						"closed_short": False,
					}
				)
			out.append(made)
		# nothing is refused any more: the request lines take the quantities
		for line_id, qty in taken.items():
			request_line = by_line[line_id][1]
			request_line["ordered_quantity"] = str(Decimal(request_line["ordered_quantity"]) + qty)
			if Decimal(request_line["ordered_quantity"]) >= Decimal(request_line["approved_quantity"]):
				request_line["status"] = "ordered"
		return out

	def _patch_order(self, order_id, body):
		"""PATCH /purchase-orders/{id} (docs: guides/stock-purchasing, Edit, reverse and return a purchase order)."""
		from decimal import Decimal

		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		if order["status"] in ("received", "cancelled"):
			return error_response(409, "po_locked", "locked")
		if self.stale_once:
			self.stale_once = False
			order["updated_at"] = "2026-10-30T09:59:00Z"  # someone changed it meanwhile
		sent = body.get("updated_at")
		if sent and sent != order["updated_at"]:
			return error_response(409, "stale_version", "the order changed")
		if order["status"] == "partial" and set(body) - {"notes", "expected_date", "updated_at"}:
			return error_response(409, "partially_received", "only notes and expected date")
		if "lines" in body:
			by_id = {l["id"]: l for l in order["lines"]}
			for line in body["lines"]:
				if line.get("id") and line["id"] not in by_id:
					return error_response(400, "invalid_parameter", "unknown line id")
			# the request lines get back what the order held, then the new shares are checked as on create
			requests = self.lists.get("/purchase-requests", [])
			by_request = {l["id"]: l for card in requests for l in card["lines"]}
			held = {}
			for line in order["lines"]:
				for share in line.get("allocations") or []:
					held[share["request_line_id"]] = held.get(share["request_line_id"], Decimal(0)) + Decimal(
						share["quantity"]
					)
			for request_line_id, qty in held.items():
				if request_line_id in by_request:
					by_request[request_line_id]["ordered_quantity"] = str(
						Decimal(by_request[request_line_id]["ordered_quantity"]) - qty
					)
					if by_request[request_line_id]["status"] == "ordered":
						by_request[request_line_id]["status"] = "approved"
			allocated = self._allocations(body)
			priced = None
			if not isinstance(allocated, FakeResponse):
				priced = self._price({**body, "tax_rate": body.get("tax_rate", 0)})
			refused = (
				allocated
				if isinstance(allocated, FakeResponse)
				else priced
				if isinstance(priced, FakeResponse)
				else None
			)
			if refused is not None:
				for request_line_id, qty in held.items():  # a refusal changes nothing
					if request_line_id in by_request:
						by_request[request_line_id]["ordered_quantity"] = str(
							Decimal(by_request[request_line_id]["ordered_quantity"]) + qty
						)
						by_request[request_line_id]["status"] = "ordered"
				return refused
			order["lines"] = [
				{
					"id": line.get("id") or f"{order['id']}-line-new-{len(self.patch_calls)}-{i}",
					"ingredient_id": line["ingredient_id"],
					"quantity_ordered": str(line["quantity_ordered"]),
					"quantity_received": by_id.get(line.get("id"), {}).get("quantity_received", "0"),
					"unit_cost": str(line["unit_cost"]),
					"allocations": allocated[i],
				}
				for i, line in enumerate(body["lines"])
			]
			order.update(priced)
		for key in ("notes", "expected_date", "reference"):
			if key in body:
				order[key] = body[key] or None
		order["updated_at"] = f"2026-10-30T10:{len(self.patch_calls):02d}:00Z"
		self.patch_calls.append(body)
		return FakeResponse(200, order)

	def _ingredient_or_warehouse(self, method, path, body):
		"""POST/PATCH/DELETE /ingredients, PUT .../default-supplier and .../low-stock-levels, POST/PATCH /warehouses."""
		bad = lambda why: error_response(400, "invalid_parameter", why)  # noqa: E731
		parts = path.split("/")
		units = ("kg", "g", "L", "ml", "pcs")
		if path == "/ingredients" and method == "POST":
			ref = body.get("external_ref")
			for found in self.ingredients.values():
				if ref and found.get("external_refs", {}).get(ref["system"]) == ref["external_id"]:
					return FakeResponse(200, found)
			if not body.get("name") or body.get("unit") not in units:
				return bad("name and a unit from GET /units are required")
			if any(i["name"].lower() == body["name"].lower() for i in self.ingredients.values()):
				return error_response(409, "name_in_use", "that name is used")
			if body.get("kind") == "supply" and not self.supplies_on:
				return error_response(403, "feature_not_available", "Supplies is switched off")
			iid = f"ing-new-{len(self.ingredients) + 1}"
			record = self.add_ingredient(iid, body["name"], body["unit"], str(body.get("cost_per_unit", "0")))
			record.update(
				{
					"kind": body.get("kind", "food"),
					"active": body.get("active", True),
					"default_supplier_id": None,
					"low_stock_levels": [],
					"external_refs": {ref["system"]: ref["external_id"]} if ref else {},
				}
			)
			self.stock_calls.append((path, body))
			return FakeResponse(201, record)
		if parts[1] == "ingredients" and len(parts) >= 3:
			ingredient = self.ingredients.get(parts[2])
			if not ingredient:
				return error_response(404, "not_found")
			if method == "DELETE":
				if ingredient.get("_in_use"):
					return FakeResponse(
						409,
						{"error": {"code": "in_use", "message": "a recipe uses it", "deactivate": True}},
					)
				del self.ingredients[parts[2]]
				self.stock_calls.append((path, body))
				return FakeResponse(200, {"id": parts[2], "object": "ingredient", "deleted": True})
			if method == "PATCH":
				if "name" in body and any(
					i["name"].lower() == body["name"].lower() and i["id"] != parts[2]
					for i in self.ingredients.values()
				):
					return error_response(409, "name_in_use", "that name is used")
				if "unit" in body:
					if body["unit"] not in units:
						return bad("unit")
					if ingredient.get("_in_use") and body["unit"] != ingredient["unit"]:
						return error_response(409, "unit_in_use", "counted in the old unit")
				for field in ("name", "unit", "active", "kind", "cost_per_unit"):
					if field in body:
						ingredient[field] = body[field]
				self.stock_calls.append((path, body))
				return FakeResponse(200, ingredient)
			if method == "PUT" and path.endswith("/default-supplier"):
				if body.get("supplier_id") and body["supplier_id"] not in self.suppliers:
					return error_response(404, "not_found")
				supplier = self.suppliers.get(body.get("supplier_id") or "")
				if supplier and supplier.get("active") is False:
					return error_response(409, "supplier_inactive", "switched off")
				ingredient["default_supplier_id"] = body.get("supplier_id")
				self.stock_calls.append((path, body))
				return FakeResponse(200, ingredient)
			if method == "PUT" and path.endswith("/low-stock-levels"):
				for entry in body.get("levels") or []:
					entry_place = entry["place"]
					rows = [
						r
						for r in ingredient.setdefault("low_stock_levels", [])
						if r["place"]["id"] != entry_place["id"]
					]
					if _decimal(entry["level"]) > 0:
						rows.append({"place": {**entry_place, "name": None}, "level": str(entry["level"])})
					ingredient["low_stock_levels"] = rows
				self.stock_calls.append((path, body))
				return FakeResponse(200, ingredient)
		if path == "/warehouses" and method == "POST":
			ref = body.get("external_ref")
			for found in self.warehouse_records.values():
				if ref and found["external_refs"].get(ref["system"]) == ref["external_id"]:
					return FakeResponse(200, found)
			if not body.get("name") or len(body["name"]) > 100:
				return bad("name")
			if self.branch_limited_key:
				return error_response(403, "place_not_allowed", "limited to a branch")
			if any(w["name"].lower() == body["name"].lower() for w in self.warehouse_records.values()):
				return error_response(409, "name_in_use", "that name is used")
			wid = f"wh-new-{len(self.warehouse_records) + 1}"
			record = {
				"id": wid,
				"object": "warehouse",
				"name": body["name"],
				"address": body.get("address"),
				"active": True,
				"is_main": False,
				"external_refs": {ref["system"]: ref["external_id"]} if ref else {},
			}
			self.warehouse_records[wid] = record
			self.stock_calls.append((path, body))
			return FakeResponse(201, record)
		if parts[1] == "warehouses" and len(parts) == 3 and method == "PATCH":
			record = self.warehouse_records.get(parts[2])
			if not record:
				return error_response(404, "not_found")
			if body.get("active") is False:
				if record.get("is_main"):
					return error_response(409, "main_warehouse", "never")
				if record.get("holds_stock"):
					return error_response(409, "warehouse_has_stock", "still holds stock")
			for field in ("name", "address", "active"):
				if field in body:
					record[field] = body[field]
			self.stock_calls.append((path, body))
			return FakeResponse(200, record)
		return None

	def _find_ref(self, store, ref):
		for found in store.values():
			if ref and found.get("external_refs", {}).get(ref["system"]) == ref["external_id"]:
				return found
		return None

	def _stock_create(self, path, body):
		"""POST /stock-adjustments, /waste, /supply-issues, /stock-counts (the docs' rules, simplified)."""
		from decimal import Decimal

		bad = lambda why: error_response(400, "invalid_parameter", why)  # noqa: E731
		place = body.get("place") or {}
		if path != "/stock-counts" and (
			place.get("type") not in ("branch", "warehouse") or not place.get("id")
		):
			return bad("place is required")
		ref = body.get("external_ref")
		n = len(self.stock_records) + 1
		if path == "/stock-adjustments":
			found = self._find_ref(self.stock_records, ref)
			if found:
				return FakeResponse(200, found)
			if body.get("ingredient_id") not in self.ingredients:
				return bad("unknown ingredient")
			try:
				qty = Decimal(str(body.get("quantity")))
			except Exception:
				return bad("quantity")
			if qty == 0 or qty != qty.quantize(Decimal("0.0001")):
				return bad("quantity: never 0, up to 4 decimals")
			reason = body.get("reason")
			if not isinstance(reason, str) or not 1 <= len(reason) <= 100:
				return bad("reason is required")
			if reason in ("order_deduction", "po_received", "supply_issue"):
				return error_response(400, "invalid_reason", "that code is written by the system")
			record = {
				"id": f"mv-{n}",
				"object": "stock_movement",
				"ingredient_id": body["ingredient_id"],
				"quantity_change": str(qty),
				"reason": reason,
				"location": {**place, "name": None},
				"note": body.get("note"),
				"below_zero": self.below_zero,
				"external_refs": {ref["system"]: ref["external_id"]} if ref else {},
			}
			self.stock_records[record["id"]] = record
			self.stock_calls.append((path, body))
			return FakeResponse(201, record)
		if path == "/waste":
			if body.get("reason") not in ("staff_meal", "comp", "tasting", "spoilage", "other"):
				return bad("reason")
			lines = body.get("lines") or []
			if not 1 <= len(lines) <= 50:
				return bad("lines: 1 to 50")
			made = []
			for line in lines:
				found = self._find_ref(self.stock_records, line.get("external_ref"))
				if found:
					made.append(found)
					continue
				ingredient = self.ingredients.get(line.get("ingredient_id"))
				if not ingredient:
					return bad("unknown ingredient")
				if ingredient.get("kind") == "supply":
					return error_response(400, "supply_not_allowed", "use POST /supply-issues")
			if made and len(made) == len(lines):
				return FakeResponse(200, made[0] if len(made) == 1 else {"object": "list", "data": made})
			for line in lines:
				lref = line.get("external_ref")
				record = {
					"id": f"waste-{len(self.stock_records) + 1}",
					"object": "waste",
					"reason": body["reason"],
					"quantity": str(line["quantity"]),
					"ingredient": {"id": line["ingredient_id"]},
					"location": {**place, "name": None},
					"external_refs": {lref["system"]: lref["external_id"]} if lref else {},
				}
				self.stock_records[record["id"]] = record
				made.append(record)
			self.stock_calls.append((path, body))
			return FakeResponse(201, made[0] if len(made) == 1 else {"object": "list", "data": made})
		if path == "/supply-issues":
			if not self.supplies_on:
				return error_response(403, "feature_not_available", "Supplies is switched off")
			found = self._find_ref(self.stock_records, ref)
			if found:
				return FakeResponse(200, found)
			ingredient = self.ingredients.get(body.get("ingredient_id"))
			if not ingredient or ingredient.get("kind") != "supply":
				return bad("not a supply")
			if body.get("reason") not in ("used", "damaged", "lost", "expired"):
				return bad("reason")
			record = {
				"id": f"sup-{n}",
				"object": "supply_issue",
				"entry_kind": "issue",
				"quantity": str(body["quantity"]),
				"reason": body["reason"],
				"undone": False,
				"external_refs": {ref["system"]: ref["external_id"]} if ref else {},
				"below_zero": self.below_zero,
			}
			self.stock_records[record["id"]] = record
			self.stock_calls.append((path, body))
			return FakeResponse(201, record)
		# /stock-counts
		found = self._find_ref(self.stock_records, ref)
		if found:
			return FakeResponse(200, found)
		record = {
			"id": f"count-{n}",
			"object": "stock_count",
			"status": "open",
			"place": place,
			"notes": body.get("notes"),
			"lines": [
				{"ingredient_id": i, "ingredient": v["name"], "expected_qty": "0", "counted_qty": None}
				for i, v in self.ingredients.items()
			],
			"external_refs": {ref["system"]: ref["external_id"]} if ref else {},
		}
		self.stock_records[record["id"]] = record
		self.stock_calls.append((path, body))
		return FakeResponse(201, record)

	def _stock_action(self, path, body):
		parts = path.split("/")
		record = self.stock_records.get(parts[2])
		if not record:
			return error_response(404, "not_found")
		action = parts[3] if len(parts) > 3 else ""
		if parts[1] == "supply-issues" and action == "undo":
			if record["undone"]:
				return error_response(409, "already_undone", "already undone")
			record["undone"] = True
			record["reversal_id"] = f"{record['id']}-undo"
			self.stock_calls.append((path, body))
			return FakeResponse(200, record)
		if parts[1] == "stock-counts":
			if action == "lines":
				if record["status"] == "closed":
					return error_response(409, "count_closed", "closed counts are locked")
				known = {l["ingredient_id"] for l in record["lines"]}
				if any(l["ingredient_id"] not in known for l in body.get("lines") or []):
					return error_response(404, "line_not_found", "not on the count")
				for sent in body["lines"]:
					for line in record["lines"]:
						if line["ingredient_id"] == sent["ingredient_id"]:
							line["counted_qty"] = str(sent["counted_quantity"])
				self.stock_calls.append((path, body))
				return FakeResponse(200, record)
			if action == "close":
				if record["status"] == "closed":
					return error_response(409, "already_closed", "closed")
				record["status"] = "closed"
				self.stock_calls.append((path, body))
				return FakeResponse(200, record)
		return error_response(404, "not_found")

	def _transfer_create(self, body):
		"""POST /stock-transfers: sent at once (the stock leaves the source) unless `draft`."""
		from decimal import Decimal

		for end in ("from", "to"):
			place = body.get(end) or {}
			if place.get("type") not in ("branch", "warehouse") or not place.get("id"):
				return error_response(400, "invalid_parameter", f"{end} is required")
		if body["from"] == body["to"]:
			return error_response(400, "invalid_parameter", "from and to are the same place")
		if not body.get("lines"):
			return error_response(400, "invalid_parameter", "lines are required")
		ref = body.get("external_ref")
		if ref:
			for found in self.transfers.values():
				if found["external_refs"].get(ref["system"]) == ref["external_id"]:
					return FakeResponse(200, found)
		reference = body.get("reference")
		if reference and any(
			(t.get("reference") or "").lower() == reference.lower() for t in self.transfers.values()
		):
			return error_response(409, "reference_in_use", "that reference is used")
		merged: dict[str, Decimal] = {}
		for line in body["lines"]:
			qty = Decimal(str(line["quantity"]))
			if qty <= 0 or qty != qty.quantize(Decimal("0.001")):
				return error_response(
					400, "invalid_parameter", "quantity: above zero, three decimals at most"
				)
			if line["ingredient_id"] not in self.ingredients:
				return error_response(400, "invalid_parameter", "unknown ingredient")
			merged[line["ingredient_id"]] = merged.get(line["ingredient_id"], Decimal(0)) + qty
		counted = {}
		for line in body["lines"]:
			if not line.get("request_line_id"):
				continue
			if self.purchase_requests_off:
				return error_response(403, "feature_not_available", "purchase requests are off")
			card, request_line = self._request_line(line["request_line_id"])
			if not request_line:
				return error_response(400, "invalid_parameter", "no such request line")
			if (request_line.get("ingredient") or {}).get("id") != line["ingredient_id"]:
				return error_response(400, "invalid_parameter", "that request line is another ingredient")
			if body["to"]["type"] != "branch" or body["to"]["id"] != card.get("branch_id"):
				return error_response(
					400, "invalid_parameter", "the transfer must go to the request's branch"
				)
			if request_line.get("status") not in ("approved", "ordered", "partly_received"):
				return error_response(409, "request_line_not_orderable", "the line is not open")
			asked = request_line.get("approved_quantity") or request_line.get("quantity")
			needed = Decimal(str(asked)) - Decimal(str(request_line.get("ordered_quantity") or 0))
			if Decimal(str(line["quantity"])) > needed:
				return error_response(409, "request_quantity_exceeded", "the line needs less than that")
			counted[line["request_line_id"]] = Decimal(str(line["quantity"]))
		n = len(self.transfers) + 1
		tid = f"tr-{n}"
		draft = bool(body.get("draft"))
		transfer = {
			"id": tid,
			"object": "stock_transfer",
			"number": f"TR-FAKE-{n:04d}",
			"status": "draft" if draft else "in_transit",
			"currency": "PKR",
			"from": {**body["from"], "name": None},
			"to": {**body["to"], "name": None},
			"reference": reference,
			"expected_date": body.get("expected_date"),
			"closed_short": False,
			"notes": body.get("notes"),
			"created_at": "2026-10-30T10:00:00Z",
			"sent_at": None if draft else "2026-10-30T10:00:00Z",
			"received_at": None,
			"cancel_reason": None,
			"business_date": "2026-10-30",
			"lines": [
				{
					"id": f"{tid}-line-{i}",
					"ingredient_id": ing,
					"ingredient": self.ingredients[ing]["name"],
					"unit": self.ingredients[ing]["unit"],
					"quantity_sent": "0.000" if draft else str(qty),
					"quantity_planned": str(qty),
					"quantity_received": "0.000",
					"quantity_returned": "0.000",
					"quantity_lost": "0.000",
					"quantity_open": "0.000" if draft else str(qty),
					"unit_cost": self.ingredients[ing]["cost_per_unit"],
					"note": None,
				}
				for i, (ing, qty) in enumerate(merged.items())
			],
			"receipts": [],
			"resolutions": [],
			"external_refs": {ref["system"]: ref["external_id"]} if ref else {},
			"updated_at": "2026-10-30T10:00:00Z",
		}
		for original in body["lines"]:
			if original.get("request_line_id"):
				for line in transfer["lines"]:
					if line["ingredient_id"] == original["ingredient_id"]:
						line["request_line_id"] = original["request_line_id"]
		for request_line_id, qty in counted.items():
			card, request_line = self._request_line(request_line_id)
			ordered = Decimal(str(request_line.get("ordered_quantity") or 0)) + qty
			request_line["ordered_quantity"] = str(ordered)
			asked = Decimal(str(request_line.get("approved_quantity") or request_line.get("quantity")))
			if ordered >= asked:
				request_line["status"] = "ordered"
		self.transfers[tid] = transfer
		self.transfer_calls.append(("create", body))
		return FakeResponse(201, transfer)

	def _request_line(self, line_id):
		for card in self.lists.get("/purchase-requests", []):
			for request_line in card.get("lines") or []:
				if request_line.get("id") == line_id:
					return card, request_line
		return None, None

	def _transfer_action(self, tid, action, body):
		from decimal import Decimal

		transfer = self.transfers.get(tid)
		if not transfer:
			return error_response(404, "not_found")
		D = Decimal
		lines = {l["id"]: l for l in transfer["lines"]}

		def settle():
			for l in transfer["lines"]:
				l["quantity_open"] = str(
					D(l["quantity_sent"])
					- D(l["quantity_received"])
					- D(l["quantity_returned"])
					- D(l["quantity_lost"])
				)
			done = all(D(l["quantity_open"]) == 0 for l in transfer["lines"])
			got = any(D(l["quantity_received"]) > 0 for l in transfer["lines"])
			transfer["status"] = "received" if done else ("partial" if got else "in_transit")
			transfer["updated_at"] = f"2026-10-30T10:{len(self.transfer_calls):02d}:00Z"

		if action == "cancel":
			reason = body.get("reason")
			if not isinstance(reason, str) or not 1 <= len(reason) <= 200:
				return error_response(400, "invalid_parameter", "reason is required, 1 to 200 characters")
			if transfer["status"] == "cancelled":
				return error_response(409, "transfer_locked", "already cancelled")
			if transfer["status"] in ("partial", "received") or any(
				D(l["quantity_received"]) > 0 for l in transfer["lines"]
			):
				return error_response(409, "transfer_partially_received", "resolve the rest instead")
			transfer["status"] = "cancelled"
			transfer["cancel_reason"] = reason
			for line in transfer["lines"]:
				if line.get("request_line_id"):
					_card, request_line = self._request_line(line["request_line_id"])
					request_line["ordered_quantity"] = str(
						max(
							D(str(request_line.get("ordered_quantity") or 0)) - D(line["quantity_sent"]), D(0)
						)
					)
					request_line["status"] = "approved"
		elif action == "receive":
			if transfer["status"] == "draft":
				return error_response(409, "transfer_not_sent", "a draft cannot be received")
			if transfer["status"] in ("received", "cancelled"):
				return error_response(409, "transfer_locked", "finished")
			rows = []
			for item in body.get("lines") or []:
				line = lines.get(item["line_id"])
				if not line:
					return error_response(400, "invalid_parameter", "that line is not on this transfer")
				qty = D(str(item["quantity"]))
				if qty <= 0 or qty > D(line["quantity_open"]):
					return error_response(409, "exceeds_in_transit", "more than is on its way")
				rows.append((line, qty))
			if not rows:
				return error_response(400, "invalid_parameter", "lines are required")
			for line, qty in rows:
				line["quantity_received"] = str(D(line["quantity_received"]) + qty)
			transfer["receipts"].append(
				{
					"id": f"{tid}-rcpt-{len(transfer['receipts']) + 1}",
					"received_at": "2026-10-30T11:00:00Z",
					"note": body.get("note"),
					"lines": [
						{
							"line_id": l["id"],
							"ingredient_id": l["ingredient_id"],
							"ingredient": l["ingredient"],
							"unit": l["unit"],
							"quantity": str(q),
							"unit_cost": l["unit_cost"],
						}
						for l, q in rows
					],
				}
			)
		elif action == "resolve":
			if transfer["status"] in ("draft", "cancelled"):
				return error_response(409, "transfer_not_sent", "nothing to resolve")
			for item in body.get("resolutions") or []:
				line = lines.get(item["line_id"])
				if not line:
					return error_response(400, "invalid_parameter", "that line is not on this transfer")
				kind = item["kind"]
				if kind == "lost" and not item.get("reason_code"):
					return error_response(400, "invalid_parameter", "reason_code is required for lost")
				qty = D(str(item.get("quantity") or 0)) or D(line["quantity_open"])
				if kind == "still_expected":
					continue
				if qty > D(line["quantity_open"]):
					return error_response(409, "exceeds_in_transit", "more than is still open")
				key = "quantity_returned" if kind == "returned" else "quantity_lost"
				line[key] = str(D(line[key]) + qty)
				transfer["resolutions"].append(
					{
						"id": f"{tid}-res-{len(transfer['resolutions']) + 1}",
						"line_id": line["id"],
						"ingredient_id": line["ingredient_id"],
						"ingredient": line["ingredient"],
						"unit": line["unit"],
						"kind": kind,
						"quantity": str(qty),
						"unit_cost": line["unit_cost"],
						"value": str(qty * D(line["unit_cost"])),
						"reason_code": item.get("reason_code"),
						"note": item.get("note"),
						"resolved_at": "2026-10-30T12:00:00Z",
					}
				)
		else:
			return error_response(404, "not_found")
		self.transfer_calls.append((action, body))
		if transfer["status"] != "cancelled":
			settle()
		return FakeResponse(200, transfer)

	def _close_short(self, order_id, body):
		"""POST /purchase-orders/{id}/close-short: no stock, no lines; ends what is still expected at ONE place."""
		from decimal import Decimal

		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		if body.get("reason_code") not in ("short", "damaged", "wrong_item", "other"):
			return error_response(400, "invalid_parameter", "reason_code is required")
		if len(body.get("note") or "") > 200:
			return error_response(400, "invalid_parameter", "note is at most 200 characters")
		sent = body.get("updated_at")
		if sent and sent != order["updated_at"]:
			return error_response(409, "stale_version", "the order changed")
		if order["status"] in ("received", "cancelled"):
			return error_response(409, "nothing_to_receive", "complete, closed or cancelled")
		shared = any(l.get("allocations") for l in order["lines"])
		receive = {"close_short": True, "reason_code": body["reason_code"], "lines": []}
		if shared:
			for key in ("destination_type", "destination_id"):
				if body.get(key):
					receive[key] = body[key]
			kind, place = body.get("destination_type"), body.get("destination_id")
			for line in order["lines"]:
				have = sum(
					(
						Decimal(a["received_quantity"])
						for a in line["allocations"]
						if (a["destination_type"], a["destination_id"]) == (kind, place) or not kind
					),
					Decimal(0),
				)
				receive["lines"].append({"line_id": line["id"], "quantity_received": str(have)})
		else:
			receive["lines"] = [
				{"line_id": l["id"], "quantity_received": l["quantity_received"]} for l in order["lines"]
			]
		answer = self._receive(order_id, receive)
		if answer.status_code == 200:
			self.receive_calls.pop()  # a close is not a delivery
			self.close_calls.append(body)
			order["updated_at"] = f"2026-10-30T15:{len(self.close_calls):02d}:00Z"
			order["receipts"][-1]["closed_reason"] = body["reason_code"]
		return answer

	def _returns(self, order_id, body):
		"""POST /purchase-orders/{id}/returns: goods back to the supplier, never more than arrived, less returned."""
		from decimal import Decimal

		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		if body.get("reason_code") not in ("damaged", "wrong_item", "quality", "other"):
			return error_response(400, "invalid_parameter", "reason_code is required")
		if len(body.get("note") or "") > 200:
			return error_response(400, "invalid_parameter", "note is at most 200 characters")
		if not body.get("lines"):
			return error_response(400, "invalid_parameter", "lines are required")
		places = {(r.get("location_type"), r.get("location_id")) for r in order["receipts"]}
		if (len(places) > 1 or self.destination_required) and not body.get("destination_id"):
			return error_response(409, "destination_required", "say where the goods go back from")
		sent = body.get("updated_at")
		if sent and sent != order["updated_at"]:
			return error_response(409, "stale_version", "the order changed")
		if self.stale_once:
			self.stale_once = False
			order["updated_at"] = "2026-10-30T09:59:00Z"
			return error_response(409, "stale_version", "the order changed")
		rows = []
		for item in body["lines"]:
			line = next((l for l in order["lines"] if l["id"] == item["line_id"]), None)
			if not line:
				return error_response(400, "invalid_parameter", "that line is not on this order")
			qty = Decimal(str(item["quantity"]))
			room = Decimal(line["quantity_received"]) - Decimal(line.get("returned_quantity") or "0")
			if qty <= 0 or qty > room:
				return error_response(409, "return_exceeds_received", "more than arrived")
			rows.append((line, qty))
		for line, qty in rows:
			line["returned_quantity"] = str(Decimal(line.get("returned_quantity") or "0") + qty)
		self.return_calls.append(body)
		order.setdefault("returns", []).append(
			{
				"id": f"{order_id}-ret-{len(order['returns']) + 1}",
				"reason_code": body["reason_code"],
				"note": body.get("note"),
				"lines": [
					{"line_id": l["id"], "quantity": str(q), "unit_cost": l["unit_cost"]} for l, q in rows
				],
			}
		)
		order["updated_at"] = f"2026-10-30T14:{len(self.return_calls):02d}:00Z"
		return FakeResponse(200, {**order, "below_zero": self.below_zero})

	def _restate(self, order_id, receipt_id, body):
		"""POST .../receipts/{receipt_id}/restate-costs: the unit cost of listed lines of one delivery, nothing else."""
		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		reason = body.get("reason")
		if not isinstance(reason, str) or not 1 <= len(reason) <= 300:
			return error_response(400, "invalid_parameter", "reason is required, 1 to 300 characters")
		if not body.get("lines"):
			return error_response(400, "invalid_parameter", "lines are required")
		entry = next((r for r in order["receipts"] if r["id"] == receipt_id), None)
		if not entry:
			return error_response(404, "not_found")
		if entry.get("reversed_at"):
			return error_response(409, "receipt_reversed", "a reversed delivery cannot be restated")
		if self.stale_once:
			self.stale_once = False
			order["updated_at"] = "2026-10-30T09:59:00Z"  # someone changed it meanwhile
		sent = body.get("updated_at")
		if sent and sent != order["updated_at"]:
			return error_response(409, "stale_version", "the order changed")
		restated = []
		for change in body["lines"]:
			done = next((l for l in entry["lines"] if l["line_id"] == change["line_id"]), None)
			if not done:
				return error_response(400, "invalid_parameter", "that line is not on this delivery")
			before = entry.setdefault("costs", {}).get(change["line_id"]) or next(
				l["unit_cost"] for l in order["lines"] if l["id"] == change["line_id"]
			)
			entry["costs"][change["line_id"]] = change["unit_cost"]
			restated.append(
				{
					"line_id": change["line_id"],
					"ingredient_id": done["ingredient_id"],
					"unit_cost_before": before,
					"unit_cost_after": change["unit_cost"],
				}
			)
		self.restate_calls.append({"receipt_id": receipt_id, **body})
		order["updated_at"] = f"2026-10-30T13:{len(self.restate_calls):02d}:00Z"
		rule = {"mode": self.cost_rule, "applied": [], "suggested": []}
		ids = [r["ingredient_id"] for r in restated]
		rule["applied" if self.cost_rule == "auto" else "suggested"] = ids if self.cost_rule != "off" else []
		return FakeResponse(200, {**order, "restated": restated, "cost_rule": rule})

	def _reverse(self, order_id, receipt_id, body):
		"""POST .../receipts/{receipt_id}/reverse: only the latest un-reversed delivery, with a reason."""
		from decimal import Decimal

		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		reason = body.get("reason")
		if not isinstance(reason, str) or not 1 <= len(reason) <= 300:
			return error_response(400, "invalid_parameter", "reason is required, 1 to 300 characters")
		entry = next((r for r in order["receipts"] if r["id"] == receipt_id), None)
		if not entry:
			return error_response(404, "not_found")
		if entry.get("reversed_at"):
			return error_response(409, "already_reversed", "already reversed")
		live = [r for r in order["receipts"] if not r.get("reversed_at")]
		if live[-1]["id"] != receipt_id:
			return error_response(409, "not_latest_receipt", "reverse the newer one first")
		if receipt_id in self.distributed:
			return error_response(409, "receipt_distributed", "cancel or resolve the transfers first")
		if receipt_id in self.returned:
			return error_response(409, "receipt_returned", "part went back to the supplier")
		sent = body.get("updated_at")
		if sent and sent != order["updated_at"]:
			return error_response(409, "stale_version", "the order changed")
		self.reverse_calls.append({"receipt_id": receipt_id, **body})
		for item in entry["lines"]:
			for line in order["lines"]:
				if line["id"] == item["line_id"]:
					line["quantity_received"] = str(
						Decimal(line["quantity_received"]) - Decimal(item["quantity"])
					)
					for share in line.get("allocations") or []:
						if (share["destination_type"], share["destination_id"]) == (
							entry.get("location_type"),
							entry.get("location_id"),
						):
							share["received_quantity"] = str(
								max(
									Decimal(share["received_quantity"]) - Decimal(item["quantity"]),
									Decimal(0),
								)
							)
							share["landed_quantity"] = share["received_quantity"]
		entry["reversed_at"] = "2026-10-30T12:00:00Z"
		entry["reverse_reason"] = reason
		live_left = [r for r in order["receipts"] if not r.get("reversed_at")]
		order["status"] = "partial" if live_left else "ordered"
		order["closed_short"] = False
		order["updated_at"] = f"2026-10-30T12:{len(self.reverse_calls):02d}:00Z"
		return FakeResponse(200, {**order, "below_zero": self.below_zero})

	def _cancel(self, order_id, body):
		"""POST /purchase-orders/{id}/cancel (docs: a draft or ordered order; the quantities go back to the requests)."""
		from decimal import Decimal

		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		reason = body.get("reason")
		if not isinstance(reason, str) or not 1 <= len(reason) <= 300:
			return error_response(400, "invalid_parameter", "reason is required, 1 to 300 characters")
		if order["status"] == "cancelled":
			return error_response(409, "already_cancelled", "already cancelled")
		if order["status"] == "partial":
			return error_response(409, "partially_received", "close it short instead")
		if order["status"] == "received":
			return error_response(409, "already_received", "already received")
		self.cancel_calls.append(body)
		order["status"] = "cancelled"
		order["cancel_reason"] = reason
		order["cancelled_at"] = "2026-10-29T09:00:00Z"
		order["updated_at"] = "2026-10-29T09:00:00Z"
		by_line = {l["id"]: l for card in self.lists.get("/purchase-requests", []) for l in card["lines"]}
		for line in order["lines"]:
			for share in line.get("allocations") or []:
				request_line = by_line.get(share.get("request_line_id"))
				if request_line:
					left = Decimal(request_line["ordered_quantity"]) - Decimal(share["quantity"])
					request_line["ordered_quantity"] = str(max(left, Decimal(0)))
					if request_line["status"] == "ordered":
						request_line["status"] = "approved"
		return FakeResponse(200, order)

	def _receive(self, order_id, body):
		"""POST /purchase-orders/{id}/receive: one delivery; lines carry the line's TOTAL so far."""
		from decimal import Decimal

		order = self.purchase_orders.get(order_id)
		if not order:
			return error_response(404, "not_found")
		if order["status"] == "cancelled":
			return error_response(409, "cancelled")
		if order["status"] == "received":
			return error_response(409, "already_received")
		if any(line.get("allocations") for line in order["lines"]):
			return self._receive_at(order, body)
		wanted = {x["line_id"]: Decimal(x["quantity_received"]) for x in body.get("lines") or []}
		if not body.get("lines"):
			wanted = {l["id"]: Decimal(l["quantity_ordered"]) for l in order["lines"]}
		arrived = []
		for line in order["lines"]:
			have = Decimal(line["quantity_received"])
			total = wanted.get(line["id"], have)
			if total < have:
				return error_response(400, "invalid_parameter", "quantity_received is below what is received")
			if total > have:
				arrived.append((line, total - have, total))
		if not arrived and not body.get("close_short"):
			return error_response(409, "nothing_to_receive")
		self.receive_calls.append(body)
		n = len(order["receipts"]) + 1
		for line, _diff, total in arrived:
			line["quantity_received"] = str(total)
		order["receipts"].append(
			{
				"id": f"{order['id']}-rcpt-{n}",
				"received_at": "2026-10-12T11:00:00Z",
				"received_by": body.get("received_by") or "key",
				"delivery_note": body.get("delivery_note"),
				"closed_short": bool(body.get("close_short")),
				"lines": [
					{"line_id": l["id"], "ingredient_id": l["ingredient_id"], "quantity": str(diff)}
					for l, diff, _t in arrived
				],
			}
		)
		complete = all(
			Decimal(l["quantity_received"]) >= Decimal(l["quantity_ordered"]) for l in order["lines"]
		)
		order["closed_short"] = bool(body.get("close_short")) and not complete
		order["status"] = "received" if complete or body.get("close_short") else "partial"
		order["updated_at"] = f"2026-10-12T11:0{n}:00Z"
		return FakeResponse(200, order)

	def _receive_at(self, order, body):
		"""An order with branch shares is received once for each place (docs: guides/purchase-requests)."""
		from decimal import Decimal

		places = {
			(a["destination_type"], a["destination_id"]): a
			for line in order["lines"]
			for a in line["allocations"]
		}
		kind, place = body.get("destination_type"), body.get("destination_id")
		if not kind:
			if len(places) > 1:
				return error_response(409, "destination_required", "name the destination")
			kind, place = next(iter(places))
		if (kind, place) not in places:
			return error_response(400, "invalid_parameter", "no share goes there")
		self.receive_calls.append(body)
		arrived, lines_out = [], []
		wanted = {x["line_id"]: x for x in body.get("lines") or []}
		for line in order["lines"]:
			shares = [
				a
				for a in line["allocations"]
				if (a["destination_type"], a["destination_id"]) == (kind, place)
			]
			if not shares:
				continue
			have = sum((Decimal(a["received_quantity"]) for a in shares), Decimal(0))
			expected = sum((Decimal(a["quantity"]) for a in shares), Decimal(0))
			if line["id"] in wanted:
				total = Decimal(wanted[line["id"]]["quantity_received"])
			elif not body.get("lines"):
				total = expected  # no lines: everything still expected there arrived
			else:
				total = have
			if total < have:
				return error_response(400, "invalid_parameter", "quantity_received is below what is received")
			if total > have:
				room = total - have
				for a in shares:
					take = (
						min(room, Decimal(a["quantity"]) - Decimal(a["received_quantity"]))
						if a is not shares[-1]
						else room
					)
					a["received_quantity"] = str(Decimal(a["received_quantity"]) + take)
					if a["destination_type"] == "branch":
						a["landed_quantity"] = a["received_quantity"]
					room -= take
				line["quantity_received"] = str(Decimal(line["quantity_received"]) + (total - have))
				arrived.append(line["id"])
				entry = {
					"line_id": line["id"],
					"ingredient_id": line["ingredient_id"],
					"quantity": str(total - have),
				}
				for key in ("reason_code", "note"):
					if wanted.get(line["id"], {}).get(key):
						entry[key] = wanted[line["id"]][key]
				lines_out.append(entry)
		if not arrived and not body.get("close_short"):
			return error_response(409, "nothing_to_receive")
		still_open = [
			a
			for line in order["lines"]
			for a in line["allocations"]
			if (a["destination_type"], a["destination_id"]) == (kind, place)
			and not a["closed_short"]
			and Decimal(a["received_quantity"]) < Decimal(a["quantity"])
		]
		if not arrived and body.get("close_short") and not still_open:
			return error_response(409, "nothing_to_receive", "nothing left to close at this destination")
		if body.get("close_short"):
			for line in order["lines"]:
				for a in line["allocations"]:
					if (a["destination_type"], a["destination_id"]) == (kind, place) and Decimal(
						a["received_quantity"]
					) < Decimal(a["quantity"]):
						a["closed_short"] = True
						a["short_reason"] = body.get("reason_code") or "other"
		n = len(order["receipts"]) + 1
		order["receipts"].append(
			{
				"id": f"{order['id']}-rcpt-{n}",
				"received_at": "2026-10-12T11:00:00Z",
				"received_by": body.get("received_by") or "key",
				"delivery_note": body.get("delivery_note"),
				"closed_short": bool(body.get("close_short")),
				"location_type": kind,
				"location_id": place,
				"location_name": places[(kind, place)].get("destination_name"),
				"lines": lines_out,
			}
		)
		done = all(
			a["closed_short"] or Decimal(a["received_quantity"]) >= Decimal(a["quantity"])
			for line in order["lines"]
			for a in line["allocations"]
		)
		order["status"] = "received" if done else "partial"
		order["updated_at"] = f"2026-10-12T11:0{n}:00Z"
		return FakeResponse(200, order)

	def _events(self, params):
		after = int(params.get("after") or 0)
		limit = int(params.get("limit") or 100)
		types = set(params["types"].split(",")) if params.get("types") else None
		since = params.get("since")
		present = [e for e in sorted(self.events, key=lambda e: e["seq"])]
		if self.deleted_through is not None:
			present = [e for e in present if e["seq"] > self.deleted_through]
		rows = [e for e in present if e["seq"] > after]
		if types:
			rows = [e for e in rows if e["type"] in types]
		if since:
			rows = [e for e in rows if e["created"][:10] >= since[:10]]
		page = rows[:limit]
		body = {
			"object": "list",
			"data": page,
			"has_more": len(rows) > limit,
			"next_after": page[-1]["seq"] if page else after,
		}
		if self.send_position:
			oldest = present[0]["seq"] if present else None
			latest = (
				self.head if self.head is not None else max((e["seq"] for e in self.events), default=None)
			)
			deleted = self.deleted_through is not None and after > 0 and after < self.deleted_through
			body.update({"oldest_seq": oldest, "latest_seq": latest, "gap": deleted})
		return FakeResponse(200, body)


class _Router:
	"""Plays requests.Session for KoomyoClient and forwards to the matching FakeKoomyo."""

	def __init__(self, fakes):
		self.fakes = fakes

	def request(self, method, url, params=None, json=None, headers=None, **_kwargs):
		for fake in self.fakes:
			if url.startswith(fake.base_url + "/"):
				response = fake.handle(method, url, params, json, headers)
				if fake.rate_limit:
					response.headers.update(
						{
							"X-RateLimit-Limit": str(fake.rate_limit[0]),
							"X-RateLimit-Remaining": str(fake.rate_limit[1]),
						}
					)
				return response
		raise requests.ConnectionError(f"no fake Koomyo at {url}")


@contextmanager
def patch_koomyo(*fakes: FakeKoomyo):
	with patch("koomyo_erpnext.client.requests.Session", lambda: _Router(list(fakes))):
		yield
