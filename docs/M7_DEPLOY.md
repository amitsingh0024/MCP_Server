# M7 — Backend Deploy Checklist (Render)

Deploys the FastAPI + MCP + worker backend as a Docker web service on Render's free tier.
The Dockerfile, `.dockerignore`, and `render.yaml` are already in the repo and the runtime
is smoke-tested locally. **Delete this file once deploy is confirmed.**

> Free-tier note: the service **sleeps after ~15 min idle** and cold-starts on the next
> request (~30–60s). The ingestion worker runs in-process, so it also pauses while asleep
> and resumes on wake (stuck tasks are auto-reset on startup). Fine for a demo/MVP.

## 1. Push the repo to GitHub (needed for Render to build)
This repo isn't on GitHub yet. Create a **private** repo and push:
```bash
gh repo create openpdfspecs --private --source . --remote origin --push
```
(or create it in the GitHub UI and `git remote add origin … && git push -u origin main`).

## 2. Create the Render service
- Render dashboard → **New → Web Service** → connect the GitHub repo.
- Render detects `render.yaml` (Blueprint) → it proposes the `openpdfspecs-api` Docker
  service. Accept it. (Or create a Docker web service manually pointing at `./Dockerfile`.)
- Plan: **Free**. Region: **Singapore** (closest free region to the Supabase Tokyo DB).

## 3. Set the environment variables (Render → the service → Environment)
Copy the values from your local `.env`:
- [ ] `DATABASE_URL` — the Supabase pooler connection string
- [ ] `SUPABASE_URL`
- [ ] `SUPABASE_SECRET_KEY` (`sb_secret_…`)
- [ ] `SUPABASE_JWKS_URL`
- [ ] `PDFSPECS_FERNET_KEY` — **must be stable** (encrypts org provider keys). Reuse the
      one from `.env`, or generate once and never change it, or all stored keys become
      undecryptable:
      `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- [ ] `SUPABASE_STORAGE_BUCKET=pdf-uploads` (already defaulted in render.yaml)
- [ ] `PDFSPECS_EMBEDDING_MODEL=text-embedding-004` (defaulted)
- [ ] `CORS_ORIGINS` — set to the Vercel frontend origin once we deploy the frontend
      (temporarily `*` is OK for a first smoke test, then lock it down).

## 4. Deploy + verify
- Render builds the image (installs Tesseract + deps) and starts it. First build ~3–5 min.
- Health check path `/health` must go green.
- Smoke-test the public URL (replace <URL>):
```bash
curl -s https://<URL>/health                      # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" https://<URL>/api/v1/documents   # 401
curl -s -o /dev/null -w "%{http_code}\n" https://<URL>/mcp/sse            # 401
```

## 5. After it's up
- Note the public `https://<URL>` — the frontend (`NEXT_PUBLIC_API_BASE_URL`) and MCP
  clients will point at it.
- This is where the **live MCP-client test** (deferred from M6) becomes possible: connect
  Claude/Cursor to `https://<URL>/mcp/sse?token=<org-api-key>`.

---
When the service is live and `/health` is green, tell me the URL and we move to the
**frontend** (Google-login UI + dashboard) — the other half of M7.
