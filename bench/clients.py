"""Host-owned, fixed-destination API clients."""

from http.client import HTTPException, HTTPSConnection
import json


def request_json(host, method, path, headers, body=None):
    connection = HTTPSConnection(host, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"{host} returned HTTP {response.status}")
            return json.load(response)
    except (OSError, HTTPException, ValueError):
        raise RuntimeError(f"{host} request or response failed") from None
    finally:
        connection.close()


class AnthropicMessages:
    def __init__(self, key):
        self.key = key

    def __call__(self, **payload):
        return request_json(
            "api.anthropic.com", "POST", "/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01",
             "content-type": "application/json"}, json.dumps(payload),
        )


def github_snapshot(token):
    """Read a bounded slice of the fixed repository once per benchmark session."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "wyrit-bench"}
    root = "/repos/jej2k5/mcpxy-test/issues"

    def get(path):
        return request_json("api.github.com", "GET", path, headers)

    issues = []
    for page in range(1, 11):
        batch = get(f"{root}?state=all&sort=created&direction=asc&per_page=100&page={page}")
        issues.extend(int(row["number"]) for row in batch if "pull_request" not in row)
        if len(issues) >= 5 or len(batch) < 100:
            break
    else:
        raise RuntimeError("GitHub issue scan exceeded 10 pages")
    return {
        number: [(row.get("body") or "")[:240] for row in
                 get(f"{root}/{number}/comments?per_page=10")]
        for number in issues[:5]
    }
