"""Extract a small product summary from public HTML without paid services."""

import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as browser_requests
from curl_cffi.requests.errors import RequestsError
from fastapi import HTTPException


def _public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "Enter a public HTTP or HTTPS product URL")
    if parsed.port not in {None, 80, 443}:
        raise HTTPException(422, "Product URL uses an unsupported port")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise HTTPException(422, "Product URL must be public")
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise HTTPException(422, "Product site could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise HTTPException(422, "Product URL must resolve to a public address")
    return parsed.geturl()


def _product_jsonld(value):
    if isinstance(value, list):
        for item in value:
            found = _product_jsonld(item)
            if found:
                return found
    if isinstance(value, dict):
        kind = value.get("@type", "")
        if kind == "Product" or isinstance(kind, list) and "Product" in kind:
            return value
        for key in ("@graph", "mainEntity", "itemListElement"):
            found = _product_jsonld(value.get(key))
            if found:
                return found
    return None


def _clean_price(value):
    if value is None:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d{1,2})?", str(value))
    return float(match.group().replace(",", "")) if match else None


def _url_fallback(url: str) -> dict:
    """Keep a quote item usable when a retailer blocks automated page reads."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    slug = parts[-2] if len(parts) > 1 and re.fullmatch(r"[A-Z]?\d{7,}", parts[-1], re.I) else (parts[-1] if parts else "")
    name = re.sub(r"[-_]+", " ", unquote(slug)).strip()
    if not name:
        raise HTTPException(422, "Could not read this product page or derive an item name from its URL")
    return {"name": name[:200], "description": "", "price": None, "currency": None,
            "source_url": url, "warning": "This store did not provide product details. Check the item name and enter the price manually."}


def parse_product_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    product = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            product = _product_jsonld(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
        if product:
            break

    def meta(*selectors):
        for selector in selectors:
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    offer = product.get("offers", {}) if product else {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    if not isinstance(offer, dict):
        offer = {}
    heading = soup.select_one('h1[itemprop="name"], h1.product-title, h1')
    description_tag = soup.select_one('[itemprop="description"], #product_description + p')
    price_tag = soup.select_one('[itemprop="price"], .price_color, .product-price, .price')
    raw_price = (offer.get("price") or offer.get("lowPrice") or
                 meta('meta[property="product:price:amount"]', 'meta[itemprop="price"]') or
                 (price_tag.get("content") or price_tag.get_text(" ", strip=True) if price_tag else None))
    name = str((product or {}).get("name") or meta('meta[property="og:title"]', 'meta[name="twitter:title"]') or
               (heading.get_text(" ", strip=True) if heading else "") or
               (soup.title.get_text(" ", strip=True) if soup.title else "")).strip()
    description = str((product or {}).get("description") or meta('meta[name="description"]', 'meta[property="og:description"]') or
                      (description_tag.get_text(" ", strip=True) if description_tag else "")).strip()
    price = _clean_price(raw_price)
    currency = str(offer.get("priceCurrency") or meta('meta[property="product:price:currency"]')).upper().strip()
    if not currency and isinstance(raw_price, str):
        currency = "GBP" if "£" in raw_price else "EUR" if "€" in raw_price else ""
    if not name:
        raise HTTPException(422, "Could not find an item name on this page")
    return {"name": name[:200], "description": BeautifulSoup(description, "html.parser").get_text(" ", strip=True)[:2000],
            "price": price, "currency": currency or None}


def _fetch_browser_html(url: str) -> tuple[str, str]:
    """Fetch public HTML with browser TLS headers without launching a browser."""
    current = url.strip()
    with browser_requests.Session(impersonate="chrome", headers={"Referer": "https://www.google.com/"}) as client:
        for _ in range(4):
            current = _public_url(current)
            try:
                response = client.get(current, timeout=12, allow_redirects=False, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        break
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    response.close()
                    if response.status_code in {403, 429}:
                        raise HTTPException(403, "Product site blocked the lookup")
                    raise HTTPException(422, "Product page could not be loaded")
                if "text/html" not in response.headers.get("content-type", ""):
                    response.close()
                    raise HTTPException(422, "Product URL is not an HTML page")
                chunks, size = [], 0
                for chunk in response.iter_content():
                    size += len(chunk)
                    if size > 2_000_000:
                        response.close()
                        raise HTTPException(413, "Product page is too large")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                response.close()
                return b"".join(chunks).decode(encoding, errors="replace"), current
            except RequestsError as exc:
                raise HTTPException(422, "Product site could not be reached") from exc
    raise HTTPException(422, "Product URL redirected too many times")


async def extract_product_info(url: str) -> dict:
    current = url.strip()
    try:
        html, current = await asyncio.to_thread(_fetch_browser_html, current)
        result = parse_product_html(html)
        # A block page can return HTTP 200; do not use its title as a quote item.
        if result["name"].strip().lower() in {"access denied", "request blocked", "general pdp template"}:
            return _url_fallback(current)
        if result["price"] is not None and not result["currency"] and (urlparse(current).hostname or "").lower().endswith(".ca"):
            result["currency"] = "CAD"
        return {**result, "source_url": current}
    except HTTPException as exc:
        if exc.status_code in {403, 422}:
            return _url_fallback(current)
        raise
