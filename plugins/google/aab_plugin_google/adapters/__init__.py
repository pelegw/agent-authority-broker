"""The three hosted adapters (gmail, gcal, gdrive) over one shared connection."""

from .gcal import GcalAdapter
from .gdrive import GdriveAdapter
from .gmail import GmailAdapter

__all__ = ["GcalAdapter", "GdriveAdapter", "GmailAdapter"]
