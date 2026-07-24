# M0 — Provisioning Checklist (your actions)

These steps require logging into external services, so **you** do them — I can't create
accounts or projects on your behalf. Everything here is free-tier. Once done, paste the
values into a local `.env` (see `.env.example`). **Delete this file once M7 deploy is done.**

## 1. Supabase project (DB + auth + storage)
- [ ] Create a free project at https://supabase.com (pick a region near you).
- [ ] **Enable pgvector:** SQL editor → run `create extension if not exists vector;`
      (or Database → Extensions → enable `vector`).
- [ ] **Run the schema:** SQL editor → paste & run `db/migrations/0001_init_schema.sql`.
- [ ] **Run RLS:** SQL editor → paste & run `db/migrations/0002_rls_policies.sql`.
- [ ] Grab these into `.env`:
      - Project Settings → Database → **Connection string** (Transaction pooler) → `DATABASE_URL`
      - Project Settings → API → **Project URL** → `SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_URL`
      - Project Settings → API → **anon key** → `NEXT_PUBLIC_SUPABASE_ANON_KEY`
      - Project Settings → API → **service_role key** → `SUPABASE_SERVICE_ROLE_KEY` (secret!)
      - Project Settings → API → **JWT secret** → `SUPABASE_JWT_SECRET`

## 2. Google OAuth (for admin login) — wired up in M3
- [ ] Google Cloud Console → create OAuth 2.0 credentials (Web application).
- [ ] Authorized redirect URI: `https://<project-ref>.supabase.co/auth/v1/callback`.
- [ ] Supabase → Authentication → Providers → **Google** → paste Client ID + Secret, enable.

## 3. Storage bucket — used in M4
- [ ] Supabase → Storage → create a **private** bucket named `pdf-uploads`
      (matches `SUPABASE_STORAGE_BUCKET`).

## 4. App secret
- [ ] Generate the Fernet key and put it in `.env` as `PDFSPECS_FERNET_KEY`:
```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 5. Hosting accounts — used in M7 (create now or later)
- [ ] **Vercel** (frontend) — sign in with GitHub; we deploy `frontend/` in M7.
- [ ] **Render** (backend Docker service) — sign in; we add the Dockerfile + service in M7.

---
When 1–4 are done and `.env` is filled, tell me and we move to **M2** (rewriting the data
layer onto Postgres). M2 doesn't need the hosting accounts yet.
