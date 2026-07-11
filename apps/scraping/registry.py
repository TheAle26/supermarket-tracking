"""
scraping/registry.py
====================
Maps a Store to its concrete scraper. The orchestrator asks the registry for a
scraper by store platform; adding a new target is a one-line registration.
"""

from catalog.models import Store

from .base import BaseScraper
from .vtex import VTEXScraper

# Import lazily-defined scrapers where they exist. Coto (legacy) and Sodimac
# (Next.js + Datadome, headless) live in their own modules; stubs referenced here.
try:
    from .coto import CotoScraper
except ImportError:  # pragma: no cover - implemented per rollout phase
    CotoScraper = None
try:
    from .sodimac import SodimacScraper
except ImportError:  # pragma: no cover
    SodimacScraper = None


# Registry keyed by store slug first (specific), falling back to platform.
_BY_SLUG: dict[str, type[BaseScraper]] = {}
if CotoScraper:
    _BY_SLUG["coto"] = CotoScraper
if SodimacScraper:
    _BY_SLUG["sodimac"] = SodimacScraper

_BY_PLATFORM: dict[str, type[BaseScraper]] = {
    Store.Platform.VTEX: VTEXScraper,   # carrefour, changomas
}


def get_scraper(store: Store) -> BaseScraper:
    cls = _BY_SLUG.get(store.slug) or _BY_PLATFORM.get(store.platform)
    if cls is None:
        raise NotImplementedError(f"No scraper registered for store={store.slug!r}")
    return cls(store)
