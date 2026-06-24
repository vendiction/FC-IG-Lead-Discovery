"""
Website analysis for Mason's gap detection.

Mason's named gaps:
- No website (disqualifier)
- No email capture (we check via form scan + lead magnet detection)
- No lead magnet
- No paid offer
- Local SEO weakness
- Homepage conversion killers (single weak line)
- Product page missing competitor-common element (e-com only)
- Email revenue underperformance (not directly detectable but inferred)
- Content struggle (inferred from missing blog/newsletter)

This module fetches the prospect's external_url and runs heuristic checks
against the landing page HTML. It does NOT crawl the full site (one-page
inspection only, for speed and to mirror Mason's "small flaw" approach).
"""
from __future__ import annotations
import re
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from app.core.logging import get_logger

log = get_logger(__name__)


EMAIL_CAPTURE_INDICATORS = [
    r"<input[^>]+type=['\"]?email['\"]?",
    r"subscribe", r"newsletter", r"join.{0,20}list",
    r"get.{0,20}(free|guide|ebook|pdf|cheat[\- ]?sheet)",
    r"download.{0,20}(free|guide|ebook|pdf)",
    r"mailchimp|convertkit|klaviyo|activecampaign|aweber|drip\.com",
]

LEAD_MAGNET_INDICATORS = [
    r"free (guide|ebook|pdf|checklist|template|cheatsheet|workbook)",
    r"download (the|your|our) (free|guide|ebook|checklist)",
    r"opt[\- ]?in",
]

PAID_OFFER_INDICATORS = [
    r"\$\d{2,5}", r"buy now", r"add to cart", r"book.{0,10}(call|consultation|strategy)",
    r"apply (now|here|to work)", r"enroll", r"join (the|now|today)",
    r"work with (me|us)", r"hire (me|us)",
]

ECOM_INDICATORS = [
    r"shopify|woocommerce|bigcommerce", r"add to cart", r"checkout",
    r"product[\- ]?page", r'data-product-id',
]

# Mason's "homepage line that could be killing conversions" — we detect:
# - vague hero headlines (single generic word, e.g. "welcome", "home")
# - missing H1
# - hero with no clear value prop verb
HOMEPAGE_WEAK_HERO_INDICATORS = [
    r"^welcome$", r"^home$", r"^about$", r"^hello$",
]


async def analyze_website(url: str) -> dict:
    """
    Fetch a URL and return gap-detection signals.

    Returns dict with all the gap_analysis fields:
    {
        'has_website': bool,
        'has_email_capture': bool,
        'has_lead_magnet': bool,
        'has_paid_offer': bool,
        'gap_local_seo': bool,
        'gap_homepage_conversion': bool,
        'gap_product_page_competitor': bool,
        'gap_lead_magnet_missing': bool,
        'gap_content_struggle': bool,
        'is_ecom': bool,
        'fetched_url': str (resolved),
        'evidence': dict (snippets backing each detection),
    }
    """
    out = {
        "has_website": False,
        "has_email_capture": False,
        "has_lead_magnet": False,
        "has_paid_offer": False,
        "gap_local_seo": False,
        "gap_homepage_conversion": False,
        "gap_product_page_competitor": False,
        "gap_lead_magnet_missing": False,
        "gap_content_struggle": False,
        "is_ecom": False,
        "fetched_url": None,
        "evidence": {},
    }

    if not url:
        return out

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"user-agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 Version/17.5 Mobile"
            )},
        ) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                log.info("m3.website.fetch_status", url=url, status=r.status_code)
                return out
            html = r.text
            out["fetched_url"] = str(r.url)
            out["has_website"] = True
    except Exception as e:
        log.info("m3.website.fetch_failed", url=url, err=str(e))
        return out

    html_low = html.lower()
    soup = BeautifulSoup(html, "lxml")

    # ----- Email capture -----
    email_hits = [p for p in EMAIL_CAPTURE_INDICATORS if re.search(p, html_low)]
    out["has_email_capture"] = bool(email_hits)
    if email_hits:
        out["evidence"]["email_capture"] = email_hits[:3]

    # ----- Lead magnet -----
    lm_hits = [p for p in LEAD_MAGNET_INDICATORS if re.search(p, html_low)]
    out["has_lead_magnet"] = bool(lm_hits)
    if lm_hits:
        out["evidence"]["lead_magnet"] = lm_hits[:3]
    out["gap_lead_magnet_missing"] = not out["has_lead_magnet"]

    # ----- Paid offer -----
    po_hits = [p for p in PAID_OFFER_INDICATORS if re.search(p, html_low)]
    out["has_paid_offer"] = bool(po_hits)
    if po_hits:
        out["evidence"]["paid_offer"] = po_hits[:3]

    # ----- E-com detection -----
    ecom_hits = [p for p in ECOM_INDICATORS if re.search(p, html_low)]
    out["is_ecom"] = bool(ecom_hits)

    # ----- Local SEO gap -----
    # Indicators of NEEDING local SEO: physical address, city, "near me", phone
    # Indicators of HAVING local SEO done: schema.org/LocalBusiness, NAP block
    has_address = bool(re.search(r"\d{1,5}\s+\w+(\s+\w+){0,3}\s+(st|ave|blvd|rd|way|street|avenue|road)", html_low))
    has_phone = bool(re.search(r"\(\d{3}\)\s*\d{3}[-.\s]?\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", html_low))
    has_local_schema = '"@type":"localbusiness"' in html_low or 'schema.org/localbusiness' in html_low
    needs_local = has_address or has_phone
    out["gap_local_seo"] = needs_local and not has_local_schema
    if out["gap_local_seo"]:
        out["evidence"]["local_seo"] = "address/phone present but no LocalBusiness schema"

    # ----- Homepage conversion gap -----
    h1 = soup.find("h1")
    h1_text = (h1.get_text(strip=True) if h1 else "").lower()
    if not h1 or not h1_text:
        out["gap_homepage_conversion"] = True
        out["evidence"]["homepage"] = "no h1 found"
    elif len(h1_text) < 8 or any(re.search(p, h1_text) for p in HOMEPAGE_WEAK_HERO_INDICATORS):
        out["gap_homepage_conversion"] = True
        out["evidence"]["homepage"] = f"weak hero: '{h1_text[:60]}'"
    elif not re.search(r"\b(help|grow|build|increase|boost|double|launch|create|win|stop|fix|solve|cut|save)\b", h1_text):
        # No action verb in hero = likely weak
        out["gap_homepage_conversion"] = True
        out["evidence"]["homepage"] = f"hero missing action verb: '{h1_text[:60]}'"

    # ----- Product page competitor gap (e-com only) -----
    # Looser check: does first product page (link with /product or /products/) have
    # reviews, urgency, social proof?
    if out["is_ecom"]:
        product_link = None
        for a in soup.find_all("a", href=True):
            if "/product" in a["href"]:
                product_link = urljoin(out["fetched_url"], a["href"])
                break
        if product_link:
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    pr = await client.get(product_link)
                    if pr.status_code < 400:
                        p_html = pr.text.lower()
                        has_reviews = bool(re.search(r"review|rating|stars", p_html))
                        has_urgency = bool(re.search(r"limited|only \d+ left|stock running|hurry", p_html))
                        has_proof = bool(re.search(r"as seen in|featured in|trusted by", p_html))
                        missing = sum(1 for x in [has_reviews, has_urgency, has_proof] if not x)
                        if missing >= 2:
                            out["gap_product_page_competitor"] = True
                            out["evidence"]["product_page"] = (
                                f"missing {missing}/3 of: reviews, urgency, social proof"
                            )
            except Exception as e:
                log.debug("m3.website.product_check_failed", err=str(e))

    # ----- Content struggle / no blog / no newsletter -----
    has_blog = bool(re.search(r"/blog|/articles|/posts|/insights", html_low))
    has_newsletter = bool(re.search(r"newsletter|substack|beehiiv", html_low))
    out["gap_content_struggle"] = not (has_blog or has_newsletter)

    return out
