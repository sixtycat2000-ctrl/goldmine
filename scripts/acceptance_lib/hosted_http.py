"""Isolated standard-library HTTPS GET worker; stdin is never persisted.

The owning parent imposes an absolute wall deadline and reaps this process.
Credentials and signed artifact URLs do not appear in argv or stderr.
"""
import hashlib
import json
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    request = json.loads(sys.stdin.buffer.read(128 * 1024))
    url = request["url"]
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError("HTTPS URL required")
    api = request["api"]
    if api:
        if host != "api.github.com" or not parsed.path.startswith("/repos/"):
            raise ValueError("fixed GitHub repository API required")
    elif not (host.endswith(".blob.core.windows.net") or host.endswith(".actions.githubusercontent.com") or host in {"objects.githubusercontent.com", "github.com"}):
        raise ValueError("fixed artifact host required")
    headers = {"Accept": "application/vnd.github+json" if api else "application/octet-stream", "User-Agent": "goldmine-ci-readonly/1"}
    if api:
        headers["X-GitHub-Api-Version"] = request["api_version"]
        if request["token"]:
            headers["Authorization"] = "Bearer " + request["token"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    result = {"status": None, "headers": {}, "location": None, "error": None}
    try:
        try:
            response = opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response, Path(request["body"]).open("wb") as output:
            result["status"] = response.status
            result["headers"] = {k: response.headers[k] for k in ("Content-Type", "ETag", "X-GitHub-Request-Id") if k in response.headers}
            if response.status == 302 and request["redirect"]:
                result["location"] = response.headers.get("Location")
            count = 0
            while block := response.read(min(1024 * 1024, request["limit"] + 1 - count)):
                output.write(block)
                count += len(block)
                if count > request["limit"]:
                    raise ValueError("response exceeds fixed byte bound")
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # Never print exception text containing a presigned URL/credential.
        result["error"] = type(exc).__name__
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": None, "headers": {}, "location": None, "error": type(exc).__name__}))
        raise SystemExit(2)
