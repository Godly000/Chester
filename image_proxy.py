import logging
from urllib.parse import urlencode, urlsplit

IMAGE_PROXY_URL = "https://image-proxy.godly-proxy-26.workers.dev/"

DEFAULT_IMAGE_WIDTH = 200
UPGRADE_IMAGE_WIDTH = 100

log = logging.getLogger("loot_bot")

def proxy_image_url(url, width=DEFAULT_IMAGE_WIDTH):
    if not url:
        return url
    url = str(url).strip()
    if url.startswith("https://static.wikia.nocookie.net/"):
        url = url.split("/revision/", 1)[0].split("?", 1)[0] + f"/revision/latest/scale-to-width-down/{width}"
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return url
    if parsed.hostname == urlsplit(IMAGE_PROXY_URL).hostname:
        fetch_url = url.replace("%3D", "=").replace("%3d", "=")
    else:
        fetch_url = IMAGE_PROXY_URL + "?" + urlencode({"url": url}, safe="=/")
    # log.info("Image fetch URL: %s", fetch_url)
    return fetch_url


def proxy_upgrade_image_url(url):
    return proxy_image_url(url, width=UPGRADE_IMAGE_WIDTH)
