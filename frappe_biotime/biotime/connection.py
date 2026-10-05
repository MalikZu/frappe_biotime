"""Build a pybiotime client from a BioTime Server record."""

from typing import TYPE_CHECKING

from pybiotime import BasicAuth, BioTimeClient, JWTAuth, TokenAuth

if TYPE_CHECKING:
	from frappe_biotime.biotime.doctype.biotime_server.biotime_server import BioTimeServer

_AUTH = {"Token": TokenAuth, "JWT": JWTAuth, "Basic": BasicAuth}

#: BioTime serves 1000 rows in about 3 seconds; the client's default is 100.
PAGE_SIZE = 1000


def connect(server: "BioTimeServer") -> BioTimeClient:
	"""A client for `server`. Use it as a context manager so its connections close."""
	auth = _AUTH[server.auth_method or "Token"](
		server.username or "", server.get_password("password", raise_exception=False) or ""
	)
	return BioTimeClient(
		server.url,
		auth=auth,
		timezone=server.timezone or None,
		timeout=server.timeout_seconds or 60,
		verify=bool(server.verify_tls),
		page_size=PAGE_SIZE,
	)
