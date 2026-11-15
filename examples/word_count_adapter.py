"""Minimal isolated adapter. Its own interpreter can have independent packages."""

import json
import sys

request = json.load(sys.stdin)
words = sum(len(block.get("text", "").split()) for block in request["document"]["blocks"])
print(json.dumps({"artifacts": [], "findings": [], "metrics": {"words": words}}))
