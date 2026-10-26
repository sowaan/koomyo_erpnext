"""Error classes shared by the client and the event handlers.

A handler either finishes or raises one of the four classified errors below.
The event processor turns each class into an Event Log status:

- Retryable: try again later on its own (a parent not arrived yet, Koomyo down).
- NeedsMapping: a map row is missing; replays once the row exists.
- Skipped: nothing to do for this event (not an error).
- Fatal: will not succeed without a person looking at it.
"""


class KoomyoError(Exception):
	"""Base class for every error this app raises on purpose."""


class Retryable(KoomyoError):
	pass


class NeedsMapping(KoomyoError):
	pass


class Skipped(KoomyoError):
	pass


class Fatal(KoomyoError):
	pass


class KoomyoAPIError(KoomyoError):
	"""An answer from the Koomyo API that was not a success.

	`code` is Koomyo's stable error code (or a local one such as `network_error`
	or `bad_response`); `message` is for a person and never holds a secret.
	"""

	# Codes after which the key itself is the problem: the connection needs a person.
	KEY_PROBLEM_CODES = frozenset(
		{
			"missing_api_key",
			"invalid_api_key",
			"ip_not_allowed",
			"app_not_enabled",
			"too_many_failed_attempts",
		}
	)

	def __init__(
		self,
		status: int | None,
		code: str,
		message: str,
		request_id: str | None = None,
		retry_after: int | None = None,
	):
		self.status = status
		self.code = code
		self.message = message
		self.request_id = request_id
		self.retry_after = retry_after
		super().__init__(self.__str__())

	def __str__(self):
		parts = [f"Koomyo API {self.status or '-'} {self.code}: {self.message}"]
		if self.request_id:
			parts.append(f"(request {self.request_id})")
		return " ".join(parts)

	@property
	def is_retryable(self) -> bool:
		"""Docs (reference/errors): retry 429 rate_limited, 5xx, 409 request_in_progress and network errors."""
		if self.code in ("network_error", "request_in_progress", "rate_limited"):
			return True
		return self.status is not None and self.status >= 500

	@property
	def is_key_problem(self) -> bool:
		return self.code in self.KEY_PROBLEM_CODES
