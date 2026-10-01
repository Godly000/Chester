import logging
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

IMAGE_PROXY_URL = "https://image-proxy.godly-proxy-26.workers.dev/"
DEFAULT_IMAGE_WIDTH = 100
UPGRADE_IMAGE_WIDTH = 100
MAX_PROXY_LAYERS = 5
FANDOM_IMAGE_DOMAINS = ("wikia.nocookie.net", "wikia.com")

log = logging.getLogger("loot_bot")


def proxy_image_url(url, width=DEFAULT_IMAGE_WIDTH):
    if not url:
        return None
    original = str(url).strip()
    url = original
    try:
        proxy_host = urlsplit(IMAGE_PROXY_URL).hostname
        for _ in range(MAX_PROXY_LAYERS):
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
                raise ValueError("Only absolute HTTP image URLs are supported")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Image URLs cannot contain credentials")
            if parsed.hostname != proxy_host:
                break
            sources = parse_qs(parsed.query).get("url", [])
            if len(sources) != 1 or not sources[0].strip():
                raise ValueError("Proxy image URL is missing a unique source")
            url = sources[0].strip()
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("Only absolute HTTP image URLs are supported")
        if parsed.hostname == proxy_host:
            raise ValueError("Too many nested image proxy URLs")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Image URLs cannot contain credentials")
        host = parsed.hostname.lower()
        if any(host == domain or host.endswith("." + domain) for domain in FANDOM_IMAGE_DOMAINS):
            path = parsed.path.split("/revision/", 1)[0].rstrip("/")
            path += f"/revision/latest/scale-to-width-down/{width}"
            url = urlunsplit(("https", parsed.netloc, path, "", ""))
        return IMAGE_PROXY_URL + "?" + urlencode({"url": url}, safe=":=/")
    except ValueError as error:
        log.warning("Image omitted because it cannot use the proxy: %s", error)
        return None


def proxy_upgrade_image_url(url):
    return proxy_image_url(url, width=UPGRADE_IMAGE_WIDTH)
