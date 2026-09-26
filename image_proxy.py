from urllib.parse import urlencode, urlsplit


IMAGE_PROXY_URL = "https://image-proxy.godly-proxy-26.workers.dev/"


def proxy_image_url(url):
    if not url:
        return url
    url = str(url).strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return url
    if parsed.hostname == urlsplit(IMAGE_PROXY_URL).hostname:
        return url
    return IMAGE_PROXY_URL + "?" + urlencode({"url": url})
