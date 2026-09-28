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
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            found = _product_jsonld(value.get(key))
            if found:
                return found
    return None


def _clean_price(value):
    if value is None:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d{1,2})?", str(value))
    return float(match.group().replace(",", "")) if match else None


def _clean_name(value) -> str:
    if not isinstance(value, str):
        return ""
    name = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    normalized = re.sub(r"[\W_]+", " ", name.casefold()).strip()
    placeholders = {
        "adding to cart", "add to cart", "added to cart", "loading", "please wait",
        "access denied", "request blocked", "general pdp template", "add item",
    }
    if normalized in placeholders or any(
        normalized.startswith(prefix + " ")
        for prefix in ("general pdp template", "access denied", "request blocked", "adding to cart")
    ):
        return ""
    return name


def _visible_heading(tag) -> bool:
    for node in [tag, *tag.parents]:
        if node.has_attr("hidden") or str(node.get("aria-hidden", "")).lower() == "true":
            return False
        style = re.sub(r"\s+", "", str(node.get("style", ""))).lower()
        if "display:none" in style or "visibility:hidden" in style:
            return False
    return True


def _embedded_product(soup: BeautifulSoup, url: str) -> dict:
    """Read JSON literals, never execute scripts or search arbitrary page prices."""
    if not url:
        return {}
    path = unquote(urlparse(url).path)
    assignment = re.compile(
        r"(?:\bwindow\.(?:__INITIAL_STATE__|__PRELOADED_STATE__|__NEXT_DATA__)\s*=\s*"
        r"|\b(?:const|let|var)\s+(?:data|product|productData)\s*=\s*)(?=\{)"
    )
    decoder = json.JSONDecoder()
    candidates = []

    def visit(value, depth=0):
        if not isinstance(value, dict) or depth > 6:
            return
        identifiers = [value.get(key) for key in ("sku", "productCode", "productId", "id")]
        matches = any(
            isinstance(identifier, (str, int)) and not isinstance(identifier, bool)
            and re.search(r"(?<![\w])" + re.escape(str(identifier)) + r"(?![\w])", path, re.I)
            for identifier in identifiers if identifier is not None
        )
        content = value.get("content")
        content = content if isinstance(content, dict) else {}
        name = _clean_name(value.get("name") or value.get("title") or content.get("productName"))
        if matches and name:
            candidates.append((value, content, name))
        # Only primary-product branches; never recommendations, reviews, or cart items.
        for key in ("props", "pageProps", "data", "product", "productData", "productDetails", "pdp"):
            visit(value.get(key), depth + 1)

    for script in soup.select("script:not([src])"):
        text = script.string or script.get_text()
        if script.get("type") == "application/json":
            try:
                visit(json.loads(text))
            except (ValueError, TypeError, RecursionError):
                pass
        for match in assignment.finditer(text):
            try:
                value, _ = decoder.raw_decode(text, match.end())
                visit(value)
            except (ValueError, TypeError, RecursionError):
                continue

    result = {}
    for value, content, name in candidates:
        price_data = value.get("price")
        if isinstance(price_data, dict):
            price_values = [price_data.get("salePrice"), price_data.get("price"), price_data.get("value")]
            currency = price_data.get("currency") or price_data.get("currencyCode")
        else:
            # Best Buy's current price includes environmental handling fees.
            price_values = [value.get("priceWithEhf"), value.get("salePrice"),
                            value.get("currentPrice"), price_data]
            currency = None
        price = next((_clean_price(item) for item in price_values
                      if isinstance(item, (str, int, float)) and not isinstance(item, bool)
                      and _clean_price(item) is not None), None)
        description = (content.get("productFullDescription") or value.get("longDescription")
                       or value.get("description") or value.get("shortDescription"))
        currency = currency or value.get("priceCurrency") or value.get("currency")
        fields = {"name": name, "description": description if isinstance(description, str) else "",
                  "price": price, "currency": currency if isinstance(currency, str) else ""}
        for key, field in fields.items():
            if result.get(key) in (None, "") and field not in (None, ""):
                result[key] = field
    return result


def _url_fallback(url: str) -> dict:
    """Keep a quote item usable when a retailer blocks automated page reads."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    slug = parts[-2] if len(parts) > 1 and re.fullmatch(r"[A-Z]?\d{7,}", parts[-1], re.I) else (parts[-1] if parts else "")
    slug = re.sub(r"\.html?$", "", unquote(slug), flags=re.I)
    name = re.sub(r"[-_]+", " ", slug).strip()
    if not name:
        raise HTTPException(422, "Could not read this product page or derive an item name from its URL")
    return {"name": name[:200], "description": "", "price": None, "currency": None,
            "source_url": url, "warning": "This store did not provide product details. Check the item name and enter the price manually."}


def parse_product_html(html: str, url: str = "") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    embedded = _embedded_product(soup, url)
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
    description_text = ""
    # Product content is more specific than site-wide SEO descriptions.
    for selector in ('[itemprop="description"]', '#product_description + p',
                     '.product.attribute.description .value', '.product-description',
                     '.PDPRichText', '.product-info-main .filter-option .content .prose'):
        tag = soup.select_one(selector)
        if tag:
            description_text = (tag.get("content") or tag.get_text(" ", strip=True)).strip()
            if description_text:
                break
    price_tag = soup.select_one('[itemprop="price"], .price_color, .product-price, .price')
    price_candidates = [offer.get("price"), offer.get("lowPrice"), embedded.get("price"),
                        meta('meta[property="product:price:amount"]', 'meta[itemprop="price"]',
                             'meta[property="og:price:amount"]'),
                        (price_tag.get("content") or price_tag.get_text(" ", strip=True) if price_tag else None)]
    raw_price = next((value for value in price_candidates if _clean_price(value) is not None), None)
    name_candidates = [(product or {}).get("name"), embedded.get("name")]
    # Separate selectors preserve priority; a combined CSS selector uses document order.
    for selector in ('h1[itemprop="name"]', 'h1.product-title', '[itemtype$="/Product"] [itemprop="name"]'):
        name_candidates.extend(tag.get("content") or tag.get_text(" ", strip=True)
                               for tag in soup.select(selector) if _visible_heading(tag))
    name_candidates.extend([meta('meta[property="og:title"]'), meta('meta[name="twitter:title"]')])
    name_candidates.extend(tag.get_text(" ", strip=True) for tag in soup.select("h1") if _visible_heading(tag))
    name_candidates.append(soup.title.get_text(" ", strip=True) if soup.title else "")
    name = next((cleaned for value in name_candidates if (cleaned := _clean_name(value))), "")
    description = str((product or {}).get("description") or embedded.get("description") or description_text or
                      meta('meta[name="description"]', 'meta[property="og:description"]')).strip()
    price = _clean_price(raw_price)
    currency = str(offer.get("priceCurrency") or embedded.get("currency") or meta('meta[property="product:price:currency"]',
                                                    'meta[itemprop="priceCurrency"]',
                                                    'meta[property="og:price:currency"]')).upper().strip()
    if not currency and isinstance(raw_price, str):
        currency = "GBP" if "£" in raw_price else "EUR" if "€" in raw_price else ""
    # A missing name must not discard a description or price recovered elsewhere.
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
                max_bytes = 10_000_000
                for chunk in response.iter_content():
                    size += len(chunk)
                    if size > max_bytes:
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
        result = parse_product_html(html, current)
        inferred_name = not result["name"]
        if inferred_name:
            result["name"] = _url_fallback(current)["name"]
        if result["price"] is not None and not result["currency"] and (urlparse(current).hostname or "").lower().endswith(".ca"):
            result["currency"] = "CAD"
        missing = [field for field in ("description", "price") if result[field] is None or result[field] == ""]
        warnings = ["The item name was inferred from its URL; please check it."] if inferred_name else []
        if missing:
            warnings.append("This store did not provide " + " and ".join(missing) + ". Check the item details and enter missing values manually.")
        if warnings:
            result["warning"] = " ".join(warnings)
        return {**result, "source_url": current}
    except HTTPException as exc:
        if exc.status_code in {403, 413, 422}:
            return _url_fallback(current)
        raise
