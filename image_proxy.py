import logging
from urllib.parse import urlencode, urlsplit

IMAGE_PROXY_URL = "https://image-proxy.godly-proxy-26.workers.dev/"

log = logging.getLogger("loot_bot")

def proxy_image_url(url):
    if not url:
        return url
    url = str(url).strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return url
    if parsed.hostname == urlsplit(IMAGE_PROXY_URL).hostname:
        fetch_url = url.replace("%3D", "=").replace("%3d", "=")
    else:
        fetch_url = IMAGE_PROXY_URL + "?" + urlencode({"url": url}, safe="=/")
    # log.info("Image fetch URL: %s", fetch_url)
    return fetch_url
