"""Run deterministic retrieval checks without calling an external LLM."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi.testclient import TestClient
from main import app, memory

memory["eval/sample"] = {
    "repo": "eval/sample", "branch": "main", "indexed_at": 1,
    "files": [
        {"path": "src/auth.py", "content": "def authenticate(token):\n    verify_signature(token)\n", "lines": 2, "symbols": [{"name": "authenticate", "kind": "function", "line": 1}], "imports": []},
        {"path": "src/payments.py", "content": "def charge_card(amount):\n    payment_gateway.charge(amount)\n", "lines": 2, "symbols": [{"name": "charge_card", "kind": "function", "line": 1}], "imports": []},
        {"path": "src/cache.py", "content": "def cache_result(key, value):\n    redis.set(key, value)\n", "lines": 2, "symbols": [{"name": "cache_result", "kind": "function", "line": 1}], "imports": []},
    ],
}
cases = [
    ("where is token authentication", "src/auth.py"),
    ("how does charge_card work", "src/payments.py"),
    ("redis cache result", "src/cache.py"),
]
client = TestClient(app)
correct = 0
for question, expected in cases:
    actual = client.get("/search/eval/sample", params={"q": question}).json()[0]["path"]
    correct += actual == expected
    print(f"{'PASS' if actual == expected else 'FAIL'} {question}: {actual}")
print(f"Recall@1: {correct}/{len(cases)}")
raise SystemExit(0 if correct == len(cases) else 1)
