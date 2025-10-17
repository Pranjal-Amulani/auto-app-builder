# student_api.py
# Requirements: fastapi, uvicorn, httpx, python-dotenv, pygit2 or GitPython (optional), pydantic
# Install: pip install fastapi uvicorn httpx python-dotenv pydantic

import os
import base64
import json
import shutil
import tempfile
import subprocess
import time
import uuid
from typing import List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, status
from pydantic import BaseModel
import httpx

# Load env vars: GITHUB_TOKEN (personal access token), SECRET_STORE_PATH or database
# Example .env:
#   GITHUB_TOKEN=ghp_...
#   SECRET_STORE=/secrets/student_secrets.json
#   GH_USER=your-gh-username

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_USER = os.getenv("GH_USER", None)
SECRET_STORE_PATH = os.getenv("SECRET_STORE", "secrets.json")
APP_BASE_DIR = Path(os.getenv("APP_BASE_DIR", "/tmp/student_apps"))

if not GITHUB_TOKEN or not GH_USER:
    print("Warning: Set GITHUB_TOKEN and GH_USER in env for repo creation to work.")

app = FastAPI(title="Student build endpoint")

# Pydantic models
class Attachment(BaseModel):
    name: str
    url: str  # data URI: data:<mime>;base64,...

class RequestPayload(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: List[str]
    evaluation_url: str
    attachments: List[Attachment] = []

# --- Helpers ------------------------------------------------

def load_secret_store(path=SECRET_STORE_PATH) -> Dict[str, str]:
    # Simple JSON mapping email -> secret_hash (in production: use DB + hashing)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def verify_secret(email: str, provided: str) -> bool:
    store = load_secret_store()
    expected = store.get(email)
    # For demo: secrets stored in plaintext; in prod compare hashes (bcrypt)
    return expected is not None and provided == expected

def create_workdir(task_name: str) -> Path:
    d = APP_BASE_DIR / task_name
    d.mkdir(parents=True, exist_ok=True)
    return d

def write_attachment(attach: Attachment, dest_dir: Path):
    # Accept data: URIs. Example: data:image/png;base64,iVBORw...
    if not attach.url.startswith("data:"):
        # support remote URL as future enhancement
        return
    header, b64 = attach.url.split(",", 1)
    data = base64.b64decode(b64)
    p = dest_dir / attach.name
    with open(p, "wb") as f:
        f.write(data)
    return p

def scaffold_minimal_app(workdir: Path, payload: RequestPayload) -> Dict[str,str]:
    """
    Create a minimal static site that implements the brief superficially.
    In production: call LLM to generate code (careful with prompts + sanitization).
    """
    # Create index.html that respects ?url= query param and displays image and a fake 'solved' text.
    index_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{payload.task}</title></head>
<body>
  <h1>{payload.task}</h1>
  <div id="app">
    <img id="captcha-img" alt="captcha" />
    <div id="solved">Solving...</div>
    <script>
      function qs(name){{
        const params = new URLSearchParams(window.location.search);
        return params.get(name);
      }}
      const defaultUrl = "{payload.attachments[0].name if payload.attachments else ''}";
      const src = qs('url') || defaultUrl;
      if(src.startsWith('data:')) {{
        document.getElementById('captcha-img').src = src;
      }} else {{
        // if a plain filename (attachment), load relative path
        document.getElementById('captcha-img').src = src;
      }}
      // naive "solver" simulation: show alt or filename after small delay
      setTimeout(()=> {{
        const img = document.getElementById('captcha-img');
        const solved = document.getElementById('solved');
        solved.textContent = 'SAMPLE-SOLVE-TEXT';
      }}, 2000);
    </script>
  </div>
</body>
</html>
"""
    (workdir / "index.html").write_text(index_html, encoding="utf-8")
    # Add a simple README
    readme = f"# {payload.task}\n\nAuto-generated student submission.\n\nSee index.html.\n"
    (workdir / "README.md").write_text(readme, encoding="utf-8")
    # Add MIT LICENSE
    mit = """MIT License

Copyright (c) {year} {owner}

Permission is hereby granted...
""".replace("{year}", str(time.gmtime().tm_year)).replace("{owner}", payload.email)
    (workdir / "LICENSE").write_text(mit, encoding="utf-8")
    return {"index":"index.html","readme":"README.md","license":"LICENSE"}

def git_init_and_push(workdir: Path, repo_name: str, gh_user: str = GH_USER):
    """
    Simplest approach uses `gh` cli (must be installed on the runner) or GitHub REST API + git commands.
    This function assumes `gh auth login --with-token` has been done if using gh.
    """
    # local git init
    run = subprocess.run
    run(["git","init"], cwd=str(workdir), check=True)
    run(["git","add","-A"], cwd=str(workdir), check=True)
    run(["git","commit","-m","Initial commit"], cwd=str(workdir), check=True)
    # Create remote repo via REST
    repo_full = f"{gh_user}/{repo_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept":"application/vnd.github.v3+json"}
    # Create repo via HTTP if not exists
    payload = {"name": repo_name, "private": False, "description":"Auto-generated student submission"}
    r = httpx.post("https://api.github.com/user/repos", json=payload, headers=headers, timeout=30.0)
    if r.status_code not in (201,422):  # 422 if repo exists
        raise Exception(f"Failed create repo: {r.status_code} {r.text}")
    # add remote and push
    run(["git","remote","add","origin", f"https://{GH_USER}:{GITHUB_TOKEN}@github.com/{gh_user}/{repo_name}.git"], cwd=str(workdir), check=True)
    run(["git","branch","-M","main"], cwd=str(workdir), check=True)
    run(["git","push","-u","origin","main"], cwd=str(workdir), check=True)
    # enable pages via REST API (create deployment branch to gh-pages or set pages source)
    pages_api = f"https://api.github.com/repos/{gh_user}/{repo_name}/pages"
    pages_payload = {"source": {"branch": "main", "path": "/"}}
    # GitHub Pages creation endpoint is special — use the pages API; sometimes requires additional calls
    r2 = httpx.post(pages_api, json=pages_payload, headers=headers, timeout=30.0)
    if r2.status_code not in (201, 204):
        # Some repos require enabling via settings; try the alternative: create workflow that publishes to gh-pages
        print("Warning: enabling pages returned", r2.status_code, r2.text)
    pages_url = f"https://{gh_user}.github.io/{repo_name}/"
    return {"repo_url": f"https://github.com/{gh_user}/{repo_name}", "pages_url": pages_url, "commit_sha": get_latest_commit_sha(workdir)}

def get_latest_commit_sha(workdir: Path) -> str:
    p = subprocess.run(["git","rev-parse","HEAD"], cwd=str(workdir), stdout=subprocess.PIPE, check=True)
    return p.stdout.decode().strip()

# Exponential backoff post
async def post_evaluation_with_backoff(evaluation_url: str, payload: dict, max_time_sec=600):
    delay = 1
    total = 0
    headers = {"Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        while total < max_time_sec:
            try:
                r = await client.post(evaluation_url, json=payload, headers=headers)
                if r.status_code == 200:
                    return True
                else:
                    # treat non-200 as retryable
                    print("Eval post non-200:", r.status_code, r.text)
            except Exception as e:
                print("Eval post exception:", e)
            # backoff
            await httpx.AsyncClient().aclose()
            time.sleep(delay)
            total += delay
            delay = min(delay*2, 60)
    return False

# --- API endpoint ------------------------------------------------

@app.post("/api-endpoint")
async def handle_request(req: Request):
    body = await req.json()
    try:
        payload = RequestPayload(**body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    # 1) verify secret:
    if not verify_secret(payload.email, payload.secret):
        raise HTTPException(status_code=403, detail="Secret mismatch")

    # 2) respond immediately with 200 JSON acknowledging receipt (spec requires 200)
    ack = {"status":"accepted", "task": payload.task, "round": payload.round}
    # continue processing (in this demo we will process synchronously; in production spawn background task)
    # but spec demands to POST eval within 10 minutes — ensure processing completes quickly.

    # 3) create workdir & write attachments
    repo_suffix = payload.task.replace("/", "-") + "-" + payload.nonce.split("-")[0]
    workdir = create_workdir(repo_suffix)
    # cleanup directory if exists
    for f in workdir.glob("*"):
        if f.is_file():
            f.unlink()

    for att in payload.attachments:
        write_attachment(att, workdir)

    # 4) scaffold minimal app (replace with LLM generation in prod)
    scaffold_minimal_app(workdir, payload)

    # 5) create repo + push + enable pages
    try:
        gh_resp = git_init_and_push(workdir, repo_suffix)
    except Exception as e:
        # critical failure; return 500
        raise HTTPException(status_code=500, detail=f"GitHub push/creation failed: {e}")

    # 6) prepare evaluation post payload
    eval_payload = {
        "email": payload.email,
        "task": payload.task,
        "round": payload.round,
        "nonce": payload.nonce,
        "repo_url": gh_resp["repo_url"],
        "commit_sha": gh_resp["commit_sha"],
        "pages_url": gh_resp["pages_url"]
    }

    # 7) POST to evaluation_url with exponential backoff (synchronous here)
    # NOTE: in production this should be background task; spec says post within 10 minutes.
    success = await post_evaluation_with_backoff(payload.evaluation_url, eval_payload, max_time_sec=600)
    if not success:
        # Log failure for later retry
        print("Failed to notify evaluation endpoint within allowed time")
        # still return 200 per spec? The spec requires ensuring 200; here we return 200 but log
    return ack
