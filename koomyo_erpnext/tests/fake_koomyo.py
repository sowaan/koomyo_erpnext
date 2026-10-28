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
		self.purchase_bodies: list[dict] = []  # every POST /purchase-orders body that was accepted
		self.purchase_requests_off = False  # GET /purchase-requests answers 403 feature_not_available
		self.unsettled_days: set[str] = set()  # GET /stock-usage answers 409 day_not_settled for these
		self.refs: list[dict] = []
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
		if method in ("POST", "PATCH", "PUT"):
			return self._write(method, path, json_body, (headers or {}).get("Idempotency-Key"))
		if path.startswith("/purchase-orders/") and path[len("/purchase-orders/") :] in self.purchase_orders:
			return FakeResponse(200, self.purchase_orders[path[len("/purchase-orders/") :]])
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
			return FakeResponse(200, self.account)
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
				"status": "ordered",
				"_ref": ref_key(body),
				"supplier": {"id": body["supplier_id"], "name": self.suppliers[body["supplier_id"]]["name"]},
				"branch_id": body.get("branch_id"),
				"currency": "PKR",
				"business_date": "2026-10-12",
				"external_refs": {body["external_ref"]["system"]: body["external_ref"]["external_id"]}
				if body.get("external_ref")
				else {},
				"number": f"PO-FAKE-{len(self.purchase_orders) + 1:04d}",
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
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/cancel"):
			return self._cancel(path.split("/")[2], body or {})
		if method == "POST" and path.startswith("/purchase-orders/") and path.endswith("/receive"):
			return self._receive(path.split("/")[2], body or {})
		if method == "PATCH" and path.startswith("/ingredients/"):
			ingredient = self.ingredients.get(path.split("/")[2])
			if not ingredient:
				return error_response(404, "not_found")
			ingredient["cost_per_unit"] = body["cost_per_unit"]
			return FakeResponse(200, ingredient)
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
