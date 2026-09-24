"""The one exception a plugin adapter raises to refuse or fail a call.

The status is part of the contract with the broker (docs/plugin-api.md):
4xx = the request is wrong or the resource is absent (404 also covers
"hidden": an adapter must answer exactly as if a denied resource did not
exist), 503 = definitely not performed, safe to retry, 502 = the call may
have reached the target and the outcome is unknown, never retry blindly.
"""


class AdapterError(Exception):
    """Refusal or failure with the HTTP status the runtime must answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        # Anything outside 400..599 would break the broker's 503/502 contract;
        # an out-of-range status is treated as an unknown outcome.
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 502
        self.message = message
