"""Small helpers with no Frappe state: redaction, money parsing, signatures."""

import hashlib
import hmac
import re
import time
from decimal import Decimal, InvalidOperation

REDACTED = "[redacted]"

# Keys whose values are secrets wherever they appear (payloads, headers, errors).
SECRET_KEYS = frozenset(
	{
		"authorization",
		"api_key",
		"apikey",
		"secret",
		"webhook_secret",
		"password",
		"token",
		"access_token",
		"koomyo-signature",
		"signature",
		"idempotency-key",
	}
)

# Customer fields never copied into ERPNext, not even into the Event Log (CLAUDE.md rule 9).
PRIVATE_CUSTOMER_KEYS = frozenset({"notes", "tags", "do_not_contact", "allergies"})

# Patterns that look like Koomyo credentials inside free text.
_SECRET_PATTERNS = (
	re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
	re.compile(r"kyo_(live|test)_[A-Za-z0-9_-]+"),
	re.compile(r"whsec_[A-Za-z0-9_-]+"),
	re.compile(r"(?i)\bv1=[0-9a-f]{16,}"),
)

# Money and quantities arrive as decimal text (docs: conventions#money, OpenAPI pattern on amounts).
_DECIMAL_TEXT = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


def redact(value, _in_customer: bool = False):
	"""Return a copy of `value` with secrets and private customer fields removed.

	Works on any JSON-like structure (dicts, lists, scalars). Strings are scanned for
	key and secret patterns as well, so an error text that quotes a header is safe.
	"""
	if isinstance(value, dict):
		is_customer = _in_customer or value.get("object") == "customer"
		out = {}
		for key, item in value.items():
			lowered = str(key).lower()
			if lowered in SECRET_KEYS:
				out[key] = REDACTED
			elif is_customer and lowered in PRIVATE_CUSTOMER_KEYS:
				continue
			else:
				out[key] = redact(item, _in_customer=lowered == "customer")
		return out
	if isinstance(value, list):
		return [redact(item, _in_customer=_in_customer) for item in value]
	if isinstance(value, str):
		return redact_text(value)
	return value


def redact_text(text: str | None) -> str:
	if not text:
		return text or ""
	for pattern in _SECRET_PATTERNS:
		text = pattern.sub(REDACTED, text)
	return text


def parse_money(value) -> Decimal:
	"""Parse Koomyo decimal text into a Decimal. Never goes through float.

	Raises ValueError for anything that is not plain decimal text, including
	numbers sent as JSON numbers (the docs promise text, so a number is a contract break).
	"""
	if isinstance(value, bool) or not isinstance(value, str) or not _DECIMAL_TEXT.match(value):
		raise ValueError(f"Not Koomyo decimal text: {value!r}")
	try:
		return Decimal(value)
	except InvalidOperation:
		raise ValueError(f"Not Koomyo decimal text: {value!r}")


def parse_signature_header(header: str | None) -> tuple[str | None, list[str]]:
	"""Split `Koomyo-Signature: t=<unix>,v1=<hex>[,v1=<hex>]` into (t, [v1, ...])."""
	timestamp = None
	signatures = []
	for item in (header or "").split(","):
		key, _, value = item.strip().partition("=")
		if key == "t":
			timestamp = value
		elif key == "v1" and value:
			signatures.append(value)
	return timestamp, signatures


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
	"""HMAC-SHA256 of "<t>.<raw body>" with the endpoint secret, lowercase hex (docs: webhooks/signatures)."""
	return hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()


def verify_signature(
	raw_body: bytes,
	header: str | None,
	secret: str,
	tolerance_seconds: int = 300,
	now: float | None = None,
) -> tuple[bool, str]:
	"""Check a webhook signature. Returns (ok, reason); reason is safe to log.

	Accepts the message if any v1 value matches, as the docs ask. The timestamp
	must be within `tolerance_seconds` of our clock in either direction.
	"""
	if not secret:
		return False, "no secret configured"
	timestamp, signatures = parse_signature_header(header)
	if not timestamp or not timestamp.isdigit() or not signatures:
		return False, "malformed signature header"
	now = time.time() if now is None else now
	if abs(now - int(timestamp)) > tolerance_seconds:
		return False, "timestamp outside the allowed window"
	expected = compute_signature(secret, timestamp, raw_body)
	if any(hmac.compare_digest(expected, given) for given in signatures):
		return True, "ok"
	return False, "signature mismatch"
