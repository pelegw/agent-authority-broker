"""Errors that map straight to an HTTP response.

`PolicyError` is raised anywhere below the routers (policy, engine, adapters'
callers) when a request must be refused with a specific status. main.py turns
it into a compact JSON body `{"error": message, "code": code}`: agents pay
tokens for every byte they read, so error bodies stay terse and stable.
"""


class PolicyError(Exception):
    """A refusal with the HTTP status (and machine-readable code) to return."""

    def __init__(self, status: int, message: str, code: str | None = None,
                 hint: str | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        # Default code is derived from the status so every error carries one.
        self.code = code or _DEFAULT_CODES.get(status, "error")
        # Optional: a short next step for the agent, and structured detail a
        # caller needs to act (e.g. the clipped capabilities of a request).
        self.hint = hint
        self.extra = extra or {}

    def body(self) -> dict:
        out = {"error": str(self), "code": self.code}
        if self.hint:
            out["hint"] = self.hint
        out.update({k: v for k, v in self.extra.items() if k not in out})
        return out


_DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    429: "rate_limited",
    502: "unknown_outcome",
    503: "unavailable",
}
