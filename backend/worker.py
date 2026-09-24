"""Redis-backed repository indexing worker."""
import asyncio
import json
import logging
import os

import redis
from main import IndexRequest, perform_index

logging.basicConfig(level=logging.INFO)
client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

while True:
    _, raw = client.brpop("atlas:index:queue")
    job = json.loads(raw)
    key = f"atlas:job:{job['id']}"
    client.setex(key, 3600, json.dumps({"status": "running", "repo": job["repo"]}))
    try:
        result = asyncio.run(perform_index(IndexRequest(repo=job["repo"], branch=job["branch"]), job["token"]))
        client.setex(key, 3600, json.dumps({"status": "complete", "result": result}))
        logging.info("Indexed %s", job["repo"])
    except Exception as exc:
        client.setex(key, 3600, json.dumps({"status": "failed", "error": str(exc)[:300]}))
        logging.exception("Failed to index %s", job["repo"])
