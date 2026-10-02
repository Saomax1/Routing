"""Plain-English explanations of Housecall Pro API failures (shared by the Phase 0 probe and the go-live script)."""

from __future__ import annotations

from .http import HttpError

_EXPLANATIONS = {
    0: "Network problem (could not reach the API). Check HCP_BASE_URL and your internet / proxy.",
    401: "401 Unauthorized: the API key was rejected. Check HCP_API_KEY, and HCP_AUTH_SCHEME in .env "
         "(the default is 'Token'; try 'Bearer' if the docs say so).",
    403: "403 Forbidden: the key works but this account/plan may not include API access (reported as the MAX plan), "
         "or the key lacks permission for this endpoint.",
    404: "404 Not Found: the endpoint path is probably different. Check JOBS_PATH / EMPLOYEES_PATH in app/hcp/client.py "
         "against the docs.",
    422: "422/400: a query parameter name is probably different. Check the PARAM_* constants in app/hcp/client.py.",
    400: "400 Bad Request: a query parameter name or value is probably different. Check the PARAM_* constants.",
    429: "429 Rate limited. Wait a minute and retry.",
}


def explain_http_error(e: HttpError) -> str:
    return _EXPLANATIONS.get(e.status, f"HTTP {e.status}")
