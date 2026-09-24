"""Local-first repository analysis API. No model provider is contacted until configured."""
import ast
import io
import json
import os
import re
import tarfile
import time
import math
import uuid
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import redis

app = FastAPI(title="Atlas Developer Workspace", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("WEB_ORIGIN", "http://localhost:3000").split(","), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
telemetry = Counter()


@app.middleware("http")
async def record_request(request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    telemetry[f"requests:{request.url.path}:{response.status_code}"] += 1
    telemetry["request_duration_ms_total"] += round((time.perf_counter() - started) * 1000)
    return response

# The in-process store makes the app usable without infrastructure. PostgreSQL is
# used when DATABASE_URL is provided; Redis caches search results when available.
memory: dict[str, dict[str, Any]] = {}
MAX_FILE = 180_000
MAX_ARCHIVE = 35_000_000
TEXT_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".json", ".md", ".css", ".html", ".sql", ".yml", ".yaml", ".toml", ".sh"}


def vectorize(value: str) -> Counter:
    """Sparse lexical vectors keep search local and require no embedding API."""
    words = re.findall(r"[a-z][a-z0-9_]{2,}", value.lower())
    return Counter(words)


def similarity(left: Counter, right: Counter) -> float:
    dot = sum(amount * right[word] for word, amount in left.items())
    mag_left = math.sqrt(sum(v * v for v in left.values()))
    mag_right = math.sqrt(sum(v * v for v in right.values()))
    return dot / (mag_left * mag_right) if mag_left and mag_right else 0.0


def database():
    if not os.getenv("DATABASE_URL"):
        return None
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.execute("CREATE TABLE IF NOT EXISTS indexes (name text PRIMARY KEY, payload jsonb NOT NULL, updated_at timestamptz DEFAULT now())")
    conn.commit()
    return conn


def save_index(name: str, payload: dict):
    memory[name] = payload
    conn = database()
    if conn:
        with conn:
            conn.execute("INSERT INTO indexes (name,payload,updated_at) VALUES (%s,%s::jsonb,now()) ON CONFLICT (name) DO UPDATE SET payload=excluded.payload,updated_at=now()", (name, json.dumps(payload)))
        conn.close()


def get_index(name: str):
    if name in memory:
        return memory[name]
    conn = database()
    if conn:
        row = conn.execute("SELECT payload FROM indexes WHERE name=%s", (name,)).fetchone()
        conn.close()
        if row:
            memory[name] = row[0]
            return row[0]
    raise HTTPException(404, "Repository has not been indexed")


def github_headers(token: str):
    if not token:
        raise HTTPException(401, "Connect GitHub with a personal access token")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def symbols(path: str, content: str):
    found = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
            found = [{"name": node.name, "kind": "class" if isinstance(node, ast.ClassDef) else "function", "line": node.lineno} for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        except SyntaxError:
            pass
    elif path.endswith((".ts", ".tsx", ".js", ".jsx", ".go")):
        pattern = r"(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const|func|struct)\s+(\w+)"
        found = [{"name": m.group(1), "kind": "symbol", "line": content.count("\n", 0, m.start()) + 1} for m in re.finditer(pattern, content)]
    return found[:150]


def imports(path: str, content: str):
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
            return list(dict.fromkeys(n.module or n.names[0].name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))))[:100]
        except SyntaxError:
            return []
    pattern = r"(?:import\s+.*?\s+from\s+|require\(|from\s+)[\"']([^\"']+)"
    return list(dict.fromkeys(re.findall(pattern, content)))[:100]


class IndexRequest(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    branch: str = ""


class AskRequest(BaseModel):
    repo: str
    question: str = Field(min_length=2, max_length=4000)
    mode: str = "ask"
    provider: str = "ollama"
    endpoint: str = "http://localhost:11434"
    model: str = "llama3.2"
    api_key: str = ""


@app.get("/health")
def health():
    return {"ok": True, "indexed": len(memory)}


@app.get("/metrics")
def metrics():
    return dict(telemetry)


@app.get("/github/user")
async def user(authorization: str = Header(default="")):
    async with httpx.AsyncClient(timeout=20) as client:
        result = await client.get("https://api.github.com/user", headers=github_headers(authorization.removeprefix("Bearer ")))
    if result.status_code != 200:
        raise HTTPException(result.status_code, "GitHub authentication failed")
    data = result.json()
    return {"login": data["login"], "avatar_url": data["avatar_url"], "name": data.get("name")}


@app.get("/github/repos")
async def repos(authorization: str = Header(default="")):
    async with httpx.AsyncClient(timeout=30) as client:
        result = await client.get("https://api.github.com/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member", headers=github_headers(authorization.removeprefix("Bearer ")))
    if result.status_code != 200:
        raise HTTPException(result.status_code, "Could not load repositories")
    return [{"name": r["name"], "full_name": r["full_name"], "description": r.get("description"), "language": r.get("language"), "private": r["private"], "updated_at": r["updated_at"], "default_branch": r["default_branch"]} for r in result.json()]


def queue_client():
    return redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


@app.post("/index")
async def index_repository(req: IndexRequest, authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ")
    github_headers(token)
    if os.getenv("REDIS_URL"):
        job_id = uuid.uuid4().hex
        client = queue_client()
        client.setex(f"atlas:job:{job_id}", 3600, json.dumps({"status": "queued", "repo": req.repo}))
        client.lpush("atlas:index:queue", json.dumps({"id": job_id, "repo": req.repo, "branch": req.branch, "token": token}))
        return {"job_id": job_id, "status": "queued"}
    return await perform_index(req, token)


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not os.getenv("REDIS_URL"):
        raise HTTPException(404, "Job not found")
    data = queue_client().get(f"atlas:job:{job_id}")
    if not data:
        raise HTTPException(404, "Job not found or expired")
    return json.loads(data)


async def perform_index(req: IndexRequest, token: str):
    headers = github_headers(token)
    branch = req.branch or "HEAD"
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        response = await client.get(f"https://api.github.com/repos/{req.repo}/tarball/{branch}", headers=headers)
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Could not download repository")
    if len(response.content) > MAX_ARCHIVE:
        raise HTTPException(413, "Repository archive exceeds 35 MB limit")
    files = []
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        for member in archive:
            if len(files) >= 1200:
                break
            path = PurePosixPath(member.name)
            relative = str(PurePosixPath(*path.parts[1:]))
            if not member.isfile() or path.suffix.lower() not in TEXT_EXT or member.size > MAX_FILE or not relative or any(part.startswith(".") for part in path.parts[1:]):
                continue
            stream = archive.extractfile(member)
            if not stream:
                continue
            content = stream.read(MAX_FILE + 1).decode("utf-8", errors="replace")
            files.append({"path": relative, "content": content, "lines": content.count("\n") + 1, "symbols": symbols(relative, content), "imports": imports(relative, content)})
    payload = {"repo": req.repo, "branch": branch, "indexed_at": int(time.time()), "files": files}
    save_index(req.repo, payload)
    languages = Counter(PurePosixPath(f["path"]).suffix.lstrip(".") for f in files)
    return {"repo": req.repo, "files": len(files), "symbols": sum(len(f["symbols"]) for f in files), "languages": languages, "indexed_at": payload["indexed_at"]}


@app.get("/index/{owner}/{repo}")
def overview(owner: str, repo: str):
    data = get_index(f"{owner}/{repo}")
    files = data["files"]
    return {"repo": data["repo"], "branch": data["branch"], "indexed_at": data["indexed_at"], "file_count": len(files), "symbol_count": sum(len(f["symbols"]) for f in files), "files": [{k: f[k] for k in ("path", "lines", "symbols", "imports")} for f in files]}


@app.get("/file/{owner}/{repo}")
def file_content(owner: str, repo: str, path: str):
    files = get_index(f"{owner}/{repo}")["files"]
    match = next((f for f in files if f["path"] == path), None)
    if not match:
        raise HTTPException(404, "File not found")
    return match


@app.get("/search/{owner}/{repo}")
def search(owner: str, repo: str, q: str):
    terms = [x.lower() for x in re.findall(r"[\w./-]+", q) if len(x) > 2]
    query_vector = vectorize(q)
    scored = []
    for file in get_index(f"{owner}/{repo}")["files"]:
        content = file["content"].lower()
        score = similarity(query_vector, vectorize(file["path"] + " " + content[:10000])) * 100
        score += sum((15 if t in file["path"].lower() else 0) + 8 * sum(t in s["name"].lower() for s in file["symbols"]) for t in terms)
        if score:
            scored.append((score, file))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"path": f["path"], "score": score, "excerpt": f["content"][:1200]} for score, f in scored[:20]]


@app.get("/github/branches/{owner}/{repo}")
async def branches(owner: str, repo: str, authorization: str = Header(default="")):
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100", headers=github_headers(authorization.removeprefix("Bearer ")))
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Could not load branches")
    return [b["name"] for b in response.json()]


@app.get("/github/compare/{owner}/{repo}")
async def compare(owner: str, repo: str, base: str, head: str, authorization: str = Header(default="")):
    from urllib.parse import quote
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}", headers=github_headers(authorization.removeprefix("Bearer ")))
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Could not compare branches")
    data = response.json()
    return {"status": data["status"], "ahead_by": data["ahead_by"], "behind_by": data["behind_by"], "commits": len(data["commits"]), "files": [{"filename": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"], "patch": f.get("patch", "")} for f in data.get("files", [])]}


@app.get("/github/prs/{owner}/{repo}")
async def pull_requests(owner: str, repo: str, authorization: str = Header(default="")):
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=30", headers=github_headers(authorization.removeprefix("Bearer ")))
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Could not load pull requests")
    return [{"number": p["number"], "title": p["title"], "author": p["user"]["login"], "url": p["html_url"], "created_at": p["created_at"]} for p in response.json()]


@app.get("/github/pr/{owner}/{repo}/{number}")
async def pull_request_files(owner: str, repo: str, number: int, authorization: str = Header(default="")):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files?per_page=100", headers=github_headers(authorization.removeprefix("Bearer ")))
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Could not load pull request files")
    return [{"filename": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"], "patch": f.get("patch", "")} for f in response.json()]


PROMPTS = {
    "ask": "Answer the user's architecture or codebase question. Be precise and cite file paths.",
    "bugs": "Identify likely bugs, edge cases, and security flaws in the provided code. Give file paths and concrete fixes. Do not invent findings.",
    "tests": "Generate useful tests for the relevant files. Explain what each test verifies and provide executable code when possible.",
    "plan": "Create a practical implementation plan grounded in this repository, naming files and verification steps.",
    "trace": "Trace the requested behavior across files in execution order, citing file paths and symbol names.",
    "review": "Review the relevant code as a pull request reviewer. Prioritize actionable correctness issues and cite locations.",
}


@app.post("/ask")
async def ask(req: AskRequest):
    files = get_index(req.repo)["files"]
    terms = [x.lower() for x in re.findall(r"[\w./-]+", req.question) if len(x) > 2]
    ranked = sorted(files, key=lambda f: sum((t in f["content"].lower()) + 8 * (t in f["path"].lower()) + 4 * any(t in s["name"].lower() for s in f["symbols"]) for t in terms), reverse=True)
    context = "\n\n".join(f"FILE: {f['path']}\n{f['content'][:7000]}" for f in ranked[:8])[:28000]
    messages = [{"role": "system", "content": PROMPTS.get(req.mode, PROMPTS["ask"]) + "\nUse only the supplied repository context. State uncertainty clearly."}, {"role": "user", "content": f"Repository: {req.repo}\nQuestion: {req.question}\n\n{context}"}]
    endpoint = req.endpoint.rstrip("/")
    # A browser on the host calls this API inside Docker; localhost there is
    # the container, so route default local model addresses to the host.
    if os.getenv("DATABASE_URL") and (endpoint.startswith("http://localhost:") or endpoint.startswith("http://127.0.0.1:")):
        endpoint = endpoint.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
    if req.provider == "ollama":
        url = endpoint + "/api/chat"
        body = {"model": req.model, "messages": messages, "stream": False}
        headers = {}
    else:
        url = endpoint + "/chat/completions" if not endpoint.endswith("/chat/completions") else endpoint
        body = {"model": req.model, "messages": messages, "stream": False}
        headers = {"Authorization": f"Bearer {req.api_key}"} if req.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        answer = payload["message"]["content"] if req.provider == "ollama" else payload["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        raise HTTPException(502, f"Model request failed: {str(exc)[:250]}") from exc
    return {"answer": answer, "sources": [f["path"] for f in ranked[:8]], "model": req.model}
