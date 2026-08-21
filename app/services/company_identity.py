"""Conservative canonical-company identity from verified ad destinations."""

from dataclasses import dataclass

import tldextract

from app.models import AdRecord
from app.services.reviews import normalize_domain


_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
)
_UNSAFE_DESTINATIONS = frozenset(
    {
        "facebook.com", "instagram.com", "linktr.ee", "bit.ly", "tinyurl.com",
        "t.co", "goo.gl", "ow.ly", "rebrand.ly", "shorturl.at",
    }
)


@dataclass(frozen=True)
class CompanyDomainResolution:
    domain: str | None
    reason: str


def resolve_verified_company_domain(record: AdRecord) -> CompanyDomainResolution:
    """Return one PSL-normalized destination only when the returned ads agree."""

    roots: set[str] = set()
    for ad in record.ads:
        declared = _root(ad.landing_page_domain)
        url_domain = _root(ad.landing_page_url)
        if declared and url_domain and declared != url_domain:
            return CompanyDomainResolution(None, "conflicting landing domain and URL")
        candidate = declared or url_domain
        if candidate:
            roots.add(candidate)
    if not roots:
        return CompanyDomainResolution(None, "no verified landing destination")
    if len(roots) != 1:
        return CompanyDomainResolution(None, "conflicting advertiser landing domains")
    return CompanyDomainResolution(next(iter(roots)), "exact verified landing domain")


def _root(value: str | None) -> str | None:
    if not value:
        return None
    hostname = normalize_domain(value)
    if hostname is None:
        return None
    root = _EXTRACT(hostname).top_domain_under_public_suffix
    if not root or root in _UNSAFE_DESTINATIONS:
        return None
    return root.casefold()
