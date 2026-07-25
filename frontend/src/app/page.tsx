"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "@/lib/supabase";
import { apiFetch, API_BASE } from "@/lib/api";

/* ---------------------------------- types --------------------------------- */
interface DocRow {
  id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
}
interface TaskRow {
  id: number;
  original_filename: string | null;
  status: "pending" | "processing" | "completed" | "failed";
  attempts: number;
  error_message: string | null;
  progress_info: string | null;
}
interface KeyRow {
  token_hash: string;
  description: string;
  created_at: string;
}
interface ConfigStatus {
  provider: string;
  has_gemini_key: boolean;
  has_nvidia_key: boolean;
  llm_model: string;
  embedding_model: string;
}
interface SearchHit {
  id: string;
  filename: string;
  page_start: number;
  page_end: number;
  score: number;
  summary: string;
  text_content: string;
}
interface Metrics {
  ingestion: { prompt: number; completion: number; total: number };
  sandbox: { prompt: number; completion: number; total: number };
}

/* ------------------------------- UI helpers ------------------------------- */
function Card({ title, subtitle, children }: { title?: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-white/10 bg-white/[0.02] p-6 shadow-xl">
      {title && <h2 className="text-sm font-semibold tracking-wide text-indigo-300">{title}</h2>}
      {subtitle && <p className="mt-1 text-xs text-gray-500">{subtitle}</p>}
      <div className={title ? "mt-4" : ""}>{children}</div>
    </section>
  );
}

function Button({
  children, onClick, disabled, variant = "primary", type = "button",
}: {
  children: React.ReactNode; onClick?: () => void; disabled?: boolean;
  variant?: "primary" | "ghost" | "danger"; type?: "button" | "submit";
}) {
  const styles = {
    primary: "bg-indigo-600 hover:bg-indigo-500 text-white",
    ghost: "border border-white/15 hover:bg-white/5 text-gray-200",
    danger: "border border-red-500/30 text-red-300 hover:bg-red-500/10",
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled}
      className={`rounded-lg px-4 py-2 text-sm font-medium transition disabled:opacity-40 disabled:cursor-not-allowed ${styles}`}>
      {children}
    </button>
  );
}

function useCopy() {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied(null), 1500);
  };
  return { copied, copy };
}

const fmtKB = (b: number) => `${(b / 1024).toFixed(1)} KB`;

/* ------------------------------- login view ------------------------------- */
function LoginScreen() {
  const [busy, setBusy] = useState(false);
  const signIn = async () => {
    setBusy(true);
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
  };
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-white/[0.02] p-8 text-center shadow-2xl">
        <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-600/20 text-2xl">📚</div>
        <h1 className="text-xl font-semibold">OpenPDFSpecs</h1>
        <p className="mt-2 text-sm text-gray-400">
          Multi-tenant PDF knowledgebase over MCP. Sign in to manage your organization&apos;s
          documents and API keys.
        </p>
        <div className="mt-6">
          <Button onClick={signIn} disabled={busy}>
            {busy ? "Redirecting…" : "Sign in with Google"}
          </Button>
        </div>
        <p className="mt-6 text-[11px] text-gray-600">
          One Google account = one organization. Your data is isolated by API key.
        </p>
      </div>
    </div>
  );
}

/* ------------------------------- panels ----------------------------------- */
function SettingsPanel({ onSaved }: { onSaved: () => void }) {
  const [cfg, setCfg] = useState<ConfigStatus | null>(null);
  const [provider, setProvider] = useState("gemini");
  const [geminiKey, setGeminiKey] = useState("");
  const [nvidiaKey, setNvidiaKey] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const c = await apiFetch<ConfigStatus>("/api/v1/config");
      setCfg(c);
      setProvider(c.provider);
    } catch (e) { setMsg((e as Error).message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true); setMsg("");
    try {
      const payload: Record<string, string> = { provider };
      if (geminiKey) payload.gemini_api_key = geminiKey;
      if (nvidiaKey) payload.nvidia_api_key = nvidiaKey;
      await apiFetch("/api/v1/config", { method: "POST", body: JSON.stringify(payload) });
      setGeminiKey(""); setNvidiaKey(""); setMsg("Saved.");
      await load(); onSaved();
    } catch (e) { setMsg((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <Card title="Provider settings (bring your own key)"
      subtitle="Your key is encrypted at rest and used only for your org's ingestion & search.">
      <div className="space-y-4">
        <div>
          <label className="text-xs text-gray-400">Provider</label>
          <select value={provider} onChange={(e) => setProvider(e.target.value)}
            className="mt-1 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm">
            <option value="gemini">Google Gemini</option>
            <option value="nvidia">NVIDIA NIM</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-gray-400">
            Gemini API key {cfg?.has_gemini_key && <span className="text-emerald-400">• configured</span>}
          </label>
          <input type="password" value={geminiKey} onChange={(e) => setGeminiKey(e.target.value)}
            placeholder={cfg?.has_gemini_key ? "•••••••• (leave blank to keep)" : "Enter key"}
            className="mt-1 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm" />
        </div>
        <div>
          <label className="text-xs text-gray-400">
            NVIDIA API key {cfg?.has_nvidia_key && <span className="text-emerald-400">• configured</span>}
          </label>
          <input type="password" value={nvidiaKey} onChange={(e) => setNvidiaKey(e.target.value)}
            placeholder={cfg?.has_nvidia_key ? "•••••••• (leave blank to keep)" : "Enter key"}
            className="mt-1 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm" />
        </div>
        <p className="text-[11px] text-gray-500">
          Note: embeddings use Gemini <code>text-embedding-004</code> platform-wide, so a Gemini key is
          recommended for semantic search.
        </p>
        <div className="flex items-center gap-3">
          <Button onClick={save} disabled={busy}>{busy ? "Saving…" : "Save settings"}</Button>
          {msg && <span className="text-xs text-gray-400">{msg}</span>}
        </div>
      </div>
    </Card>
  );
}

function IngestPanel({ onQueued }: { onQueued: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const upload = async () => {
    if (!file) return;
    setBusy(true); setMsg("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await apiFetch<{ task_id: number }>("/api/v1/ingest", { method: "POST", body: fd });
      setMsg(`Queued for processing (task #${r.task_id}).`);
      setFile(null); onQueued();
    } catch (e) { setMsg((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Card title="Ingest a PDF" subtitle="Uploads to your org's private storage and queues background processing.">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
        <input type="file" accept="application/pdf"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-indigo-600 file:px-3 file:py-2 file:text-white" />
        <Button onClick={upload} disabled={!file || busy}>{busy ? "Uploading…" : "Upload & ingest"}</Button>
      </div>
      {msg && <p className="mt-3 text-xs text-gray-400">{msg}</p>}
    </Card>
  );
}

function DocumentsPanel({ refreshKey }: { refreshKey: number }) {
  const [docs, setDocs] = useState<DocRow[]>([]);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const [d, t] = await Promise.all([
        apiFetch<DocRow[]>("/api/v1/documents"),
        apiFetch<TaskRow[]>("/api/v1/tasks"),
      ]);
      setDocs(d); setTasks(t);
    } catch (e) { setErr((e as Error).message); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);
  // Poll while anything is in flight.
  useEffect(() => {
    const active = tasks.some((t) => t.status === "pending" || t.status === "processing");
    if (!active) return;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [tasks, load]);

  const del = async (id: string) => {
    await apiFetch(`/api/v1/documents/${id}`, { method: "DELETE" });
    load();
  };
  const progressOf = (t: TaskRow): { progress?: number } => {
    try { return JSON.parse(t.progress_info || "{}"); } catch { return {}; }
  };
  const statusColor: Record<TaskRow["status"], string> = {
    completed: "text-emerald-400", failed: "text-red-400",
    processing: "text-indigo-300", pending: "text-gray-400",
  };

  return (
    <div className="space-y-6">
      <Card title="Queue" subtitle="Background ingestion status.">
        {tasks.length === 0 ? <p className="text-sm text-gray-500">No tasks yet.</p> : (
          <ul className="space-y-2">
            {tasks.slice(0, 8).map((t) => {
              const p = progressOf(t);
              return (
                <li key={t.id} className="flex items-center justify-between rounded-lg border border-white/5 bg-black/20 px-3 py-2 text-sm">
                  <span className="truncate">{t.original_filename || `task #${t.id}`}</span>
                  <span className="ml-3 flex items-center gap-2 text-xs">
                    <span className={statusColor[t.status]}>{t.status}</span>
                    {(t.status === "processing" || t.status === "pending") && p.progress != null &&
                      <span className="text-gray-500">{p.progress}%</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card title={`Documents (${docs.length})`}>
        {err && <p className="text-xs text-red-400">{err}</p>}
        {docs.length === 0 ? <p className="text-sm text-gray-500">No documents indexed yet.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-gray-500">
                <tr><th className="py-2">Filename</th><th>Size</th><th>Ingested</th><th></th></tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id} className="border-t border-white/5">
                    <td className="py-2 pr-4">{d.filename}</td>
                    <td className="pr-4 text-gray-400">{fmtKB(d.size_bytes)}</td>
                    <td className="pr-4 text-gray-500">{new Date(d.created_at).toLocaleString()}</td>
                    <td><button onClick={() => del(d.id)} className="text-xs text-red-400 hover:underline">delete</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function SearchPanel() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const run = async () => {
    if (!q.trim()) return;
    setBusy(true); setErr(""); setHits(null);
    try {
      const r = await apiFetch<{ results: SearchHit[] }>("/api/v1/search", {
        method: "POST", body: JSON.stringify({ query: q, limit: 8 }),
      });
      setHits(r.results);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Card title="Search your knowledgebase" subtitle="Hybrid lexical + semantic (RRF).">
      <div className="flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="e.g. treatment for fever"
          className="flex-1 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm" />
        <Button onClick={run} disabled={busy}>{busy ? "Searching…" : "Search"}</Button>
      </div>
      {err && <p className="mt-3 text-xs text-red-400">{err}</p>}
      {hits && hits.length === 0 && <p className="mt-4 text-sm text-gray-500">No matches.</p>}
      {hits && hits.length > 0 && (
        <ul className="mt-4 space-y-3">
          {hits.map((h, i) => (
            <li key={h.id} className="rounded-lg border border-white/5 bg-black/20 p-4">
              <div className="flex items-center justify-between text-xs text-gray-500">
                <span>{i + 1}. {h.filename} · pages {h.page_start}–{h.page_end}</span>
                <span>score {h.score.toFixed(4)}</span>
              </div>
              {h.summary && <p className="mt-1 text-sm text-indigo-200">{h.summary}</p>}
              <p className="mt-1 text-xs text-gray-400 line-clamp-3">{h.text_content?.slice(0, 300)}…</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function KeysPanel() {
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [fresh, setFresh] = useState<string | null>(null);
  const [desc, setDesc] = useState("");
  const { copied, copy } = useCopy();

  const load = useCallback(async () => {
    setKeys(await apiFetch<KeyRow[]>("/api/v1/keys"));
  }, []);
  useEffect(() => { load(); }, [load]);

  const gen = async () => {
    const r = await apiFetch<{ token: string }>("/api/v1/keys", {
      method: "POST", body: JSON.stringify({ description: desc || "Agent Access Key" }),
    });
    setFresh(r.token); setDesc(""); load();
  };
  const revoke = async (hash: string) => {
    await apiFetch(`/api/v1/keys/${hash}`, { method: "DELETE" });
    load();
  };
  const mcpUrl = fresh ? `${API_BASE}/mcp?token=${fresh}` : "";

  return (
    <div className="space-y-6">
      <Card title="API keys" subtitle="Give these to colleagues / MCP clients. They scope access to THIS org only.">
        <div className="flex gap-2">
          <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Description (e.g. Amit's Cursor)"
            className="flex-1 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm" />
          <Button onClick={gen}>Generate key</Button>
        </div>

        {fresh && (
          <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/5 p-4">
            <p className="text-xs text-amber-300">Copy this now — it is shown only once.</p>
            <div className="mt-2 flex items-center gap-2">
              <code className="flex-1 truncate rounded bg-black/40 px-3 py-2 text-xs">{fresh}</code>
              <Button variant="ghost" onClick={() => copy(fresh, "key")}>{copied === "key" ? "Copied" : "Copy"}</Button>
            </div>
            <p className="mt-3 text-xs text-gray-400">Paste this URL into your AI tool (Claude, Cursor, …) as a custom MCP connector:</p>
            <div className="mt-1 flex items-center gap-2">
              <code className="flex-1 truncate rounded bg-black/40 px-3 py-2 text-xs">{mcpUrl}</code>
              <Button variant="ghost" onClick={() => copy(mcpUrl, "url")}>{copied === "url" ? "Copied" : "Copy"}</Button>
            </div>
          </div>
        )}

        <div className="mt-5">
          {keys.length === 0 ? <p className="text-sm text-gray-500">No keys yet.</p> : (
            <ul className="space-y-2">
              {keys.map((k) => (
                <li key={k.token_hash} className="flex items-center justify-between rounded-lg border border-white/5 bg-black/20 px-3 py-2 text-sm">
                  <div>
                    <span>{k.description}</span>
                    <span className="ml-2 text-xs text-gray-600">…{k.token_hash.slice(-8)}</span>
                  </div>
                  <button onClick={() => revoke(k.token_hash)} className="text-xs text-red-400 hover:underline">revoke</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>
    </div>
  );
}

/* ------------------------------ dashboard --------------------------------- */
const TABS = ["Documents", "Ingest", "Search", "API Keys", "Settings"] as const;
type Tab = (typeof TABS)[number];

function Dashboard({ session }: { session: Session }) {
  const [tab, setTab] = useState<Tab>("Documents");
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const email = session.user.email ?? "";

  const loadMetrics = useCallback(async () => {
    try { setMetrics(await apiFetch<Metrics>("/api/v1/metrics")); } catch { /* ignore */ }
  }, []);
  useEffect(() => { loadMetrics(); }, [loadMetrics, refreshKey]);

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-600/20 text-lg">📚</div>
          <div>
            <h1 className="text-lg font-semibold leading-tight">OpenPDFSpecs</h1>
            <p className="text-xs text-gray-500">{email}</p>
          </div>
        </div>
        <div className="flex items-center gap-4">
          {metrics && (
            <span className="hidden text-xs text-gray-500 sm:inline">
              tokens: {metrics.ingestion.total.toLocaleString()} ingest · {metrics.sandbox.total.toLocaleString()} search
            </span>
          )}
          <Button variant="ghost" onClick={() => supabase.auth.signOut()}>Sign out</Button>
        </div>
      </header>

      <nav className="mt-8 flex gap-1 border-b border-white/10">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm transition ${
              tab === t ? "border-b-2 border-indigo-500 text-white" : "text-gray-400 hover:text-gray-200"
            }`}>{t}</button>
        ))}
      </nav>

      <main className="mt-6">
        {tab === "Documents" && <DocumentsPanel refreshKey={refreshKey} />}
        {tab === "Ingest" && <IngestPanel onQueued={() => setRefreshKey((k) => k + 1)} />}
        {tab === "Search" && <SearchPanel />}
        {tab === "API Keys" && <KeysPanel />}
        {tab === "Settings" && <SettingsPanel onSaved={() => setRefreshKey((k) => k + 1)} />}
      </main>
    </div>
  );
}

/* -------------------------------- root ------------------------------------ */
export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setLoading(false);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  if (loading) {
    return <div className="flex min-h-screen items-center justify-center text-sm text-gray-500">Loading…</div>;
  }
  return session ? <Dashboard session={session} /> : <LoginScreen />;
}
