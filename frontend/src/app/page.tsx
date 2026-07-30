"use client";

import { useEffect, useState, FormEvent } from "react";
import ReactMarkdown from 'react-markdown';

// --- Minimalist SVG Icons ---
const Icons = {
  Book: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
    </svg>
  ),
  Upload: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
    </svg>
  ),
  Search: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
    </svg>
  ),
  Key: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
    </svg>
  ),
  Settings: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  ),
  Check: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 13l4 4L19 7" />
    </svg>
  ),
  Copy: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
    </svg>
  ),
  Alert: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
    </svg>
  ),
  Loader: () => (
    <svg className="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
    </svg>
  )
};

// --- Minimalist Components ---
const Card = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => (
  <div className={`bg-[#0a0a0a] border border-zinc-800 rounded-lg overflow-hidden ${className}`}>
    {children}
  </div>
);

const Button = ({ children, onClick, type = "button", variant = "primary", className = "", disabled = false }: any) => {
  const variants = {
    primary: "bg-white text-black hover:bg-zinc-200 active:scale-[0.98]",
    secondary: "bg-zinc-900 text-zinc-300 border border-zinc-800 hover:bg-zinc-800 active:scale-[0.98]",
    ghost: "bg-transparent text-zinc-400 hover:text-white hover:bg-zinc-900 active:scale-[0.98]",
    danger: "bg-transparent text-red-500 hover:bg-red-500/10 active:scale-[0.98]"
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`px-4 py-2 text-sm font-medium rounded-md transition-all duration-200 ease-out flex items-center justify-center gap-2 ${variants[variant as keyof typeof variants]} ${disabled ? "opacity-50 cursor-not-allowed" : ""} ${className}`}
    >
      {children}
    </button>
  );
};

const Input = ({ ...props }) => (
  <input
    {...props}
    className={`w-full bg-transparent border border-zinc-800 text-zinc-100 placeholder-zinc-600 rounded-md px-3 py-2 text-sm focus:border-zinc-500 focus:outline-none transition-colors ${props.className || ""}`}
  />
);

const Select = ({ children, ...props }: any) => (
  <select
    {...props}
    className={`w-full bg-[#0a0a0a] border border-zinc-800 text-zinc-100 rounded-md px-3 py-2 text-sm focus:border-zinc-500 focus:outline-none transition-colors appearance-none ${props.className || ""}`}
  >
    {children}
  </select>
);

// --- Environment configuration ---
import { supabase } from "@/lib/supabase";
import { apiFetch, API_BASE } from "@/lib/api";

export default function Home() {
  const [session, setSession] = useState<any>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("documents");

  // Global state
  const [config, setConfig] = useState<any>({});

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session);
    });
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session);
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (session) {
      loadConfig();
    }
  }, [session]);

  async function loadConfig() {
    try {
      const data = await apiFetch("/api/v1/config");
      setConfig(data);
    } catch (e) {
      console.error(e);
    }
  }

  async function handleLogin() {
    setAuthLoading(true);
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
  }

  async function handleLogout() {
    await supabase.auth.signOut();
  }

  if (!session) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center p-6">
        <Card className="w-full max-w-sm p-8 text-center flex flex-col gap-6">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-zinc-900 border border-zinc-800 text-2xl">📚</div>
          <div className="space-y-1">
            <h1 className="text-xl font-medium text-white">OpenPDFSpecs</h1>
            <p className="text-sm text-zinc-500">
              Multi-tenant PDF knowledgebase over MCP. Sign in to manage your organization's documents and API keys.
            </p>
          </div>
          <Button onClick={handleLogin} disabled={authLoading} className="w-full py-2.5">
            {authLoading ? "Redirecting..." : "Sign in with Google"}
          </Button>
          <p className="text-[11px] text-zinc-600">
            One Google account = one organization. Your data is isolated by API key.
          </p>
        </Card>
      </div>
    );
  }

  const tabs = [
    { id: "documents", label: "Documents", icon: <Icons.Book /> },
    { id: "ingest", label: "Ingest", icon: <Icons.Upload /> },
    { id: "search", label: "Search", icon: <Icons.Search /> },
    { id: "keys", label: "API Keys", icon: <Icons.Key /> },
    { id: "settings", label: "Settings", icon: <Icons.Settings /> },
  ];

  return (
    <div className="min-h-screen bg-black text-zinc-300 font-sans selection:bg-zinc-800">
      <div className="max-w-5xl mx-auto px-6 py-12">
        {/* Header */}
        <header className="flex items-center justify-between mb-12">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-white rounded-md flex items-center justify-center">
              <div className="w-4 h-4 bg-black rounded-sm"></div>
            </div>
            <div>
              <h1 className="text-lg font-medium text-white leading-tight">OpenPDFSpecs</h1>
              <p className="text-xs text-zinc-500">{session.user.email}</p>
            </div>
          </div>
          <div className="flex items-center gap-6 text-sm">
            <Button variant="ghost" onClick={handleLogout} className="!px-2 !py-1 text-xs">Sign out</Button>
          </div>
        </header>

        {/* Navigation */}
        <nav className="flex gap-1 mb-8 border-b border-zinc-900 pb-px">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-2 text-sm transition-colors ${
                activeTab === tab.id 
                  ? "text-white border-b border-white" 
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </nav>

        {/* Content Area */}
        <main className="animate-in fade-in duration-300">
          {activeTab === "documents" && <DocumentsPanel />}
          {activeTab === "ingest" && <IngestPanel onIngest={() => {}} />}
          {activeTab === "search" && <SearchPanel />}
          {activeTab === "keys" && <KeysPanel />}
          {activeTab === "settings" && <SettingsPanel config={config} onSave={() => loadConfig()} />}
        </main>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------
// Panels
// ------------------------------------------------------------------

function DocumentsPanel() {
  const [docs, setDocs] = useState<any[]>([]);
  const [tasks, setTasks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const [docsData, tasksData] = await Promise.all([
          apiFetch<any[]>("/api/v1/documents"),
          apiFetch<any[]>("/api/v1/tasks")
        ]);
        setDocs(docsData);
        setTasks(tasksData);
      } catch (e) {
        console.error(e);
      } finally {
        setLoading(false);
      }
    }
    load();
    const int = setInterval(load, 5000);
    return () => clearInterval(int);
  }, []);

  return (
    <div className="space-y-8">
      {/* Active Tasks */}
      <section className="space-y-4">
        <h2 className="text-sm font-medium text-white flex items-center gap-2">
          Ingestion Queue {tasks.length > 0 && <span className="text-xs bg-zinc-900 px-2 py-0.5 rounded text-zinc-400">{tasks.length}</span>}
        </h2>
        {tasks.length === 0 ? (
          <p className="text-sm text-zinc-600">No active tasks in queue.</p>
        ) : (
          <div className="grid gap-3">
            {tasks.map(t => {
              const p = (function() { try { return JSON.parse(t.progress_info || "{}"); } catch { return {}; } })();
              const pct = p.progress != null ? Math.round(p.progress * 100) : 0;
              const statusMsg = p.status || t.status;
              return (
                <Card key={t.id} className="p-4 flex flex-col gap-3">
                  <div className="flex justify-between items-start">
                    <div>
                      <div className="text-sm text-zinc-300 font-medium">{t.original_filename || `task #${t.id}`}</div>
                      <div className="text-xs text-zinc-500 mt-0.5 truncate max-w-md">{statusMsg}</div>
                    </div>
                    <div className="text-xs font-mono text-zinc-500">
                      {t.status === 'processing' ? (
                        <span className="flex items-center gap-1.5 text-zinc-300"><Icons.Loader /> {pct}%</span>
                      ) : t.status === 'error' ? (
                        <span className="flex items-center gap-1.5 text-red-500"><Icons.Alert /> Error</span>
                      ) : (
                        <span className="flex items-center gap-1.5 text-zinc-500">{t.status}</span>
                      )}
                    </div>
                  </div>
                  {t.status === 'processing' && (
                    <div className="w-full bg-zinc-900 h-1 rounded-full overflow-hidden">
                      <div className="bg-zinc-500 h-full transition-all duration-500" style={{ width: `${pct}%` }}></div>
                    </div>
                  )}
                </Card>
              );
            })}
          </div>
        )}
      </section>

      {/* Catalog */}
      <section className="space-y-4">
        <h2 className="text-sm font-medium text-white">Knowledge Base</h2>
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm text-zinc-400">
              <thead className="text-xs text-zinc-500 border-b border-zinc-900">
                <tr>
                  <th className="px-4 py-3 font-medium">Filename</th>
                  <th className="px-4 py-3 font-medium">Chunks</th>
                  <th className="px-4 py-3 font-medium">Ingested At</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-900">
                {docs.length === 0 && (
                  <tr><td colSpan={3} className="px-4 py-8 text-center text-zinc-600">No documents cataloged.</td></tr>
                )}
                {docs.map(d => (
                  <tr key={d.id} className="hover:bg-zinc-900/50 transition-colors">
                    <td className="px-4 py-3 font-medium text-zinc-300">{d.filename}</td>
                    <td className="px-4 py-3">{d.chunk_count}</td>
                    <td className="px-4 py-3 font-mono text-xs">{new Date(d.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </section>
    </div>
  );
}

function IngestPanel({ onIngest }: { onIngest: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState("");

  async function handleUpload(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setUploading(true);
    setMsg("");
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await apiFetch("/api/v1/ingest", {
        method: "POST",
        body: formData
      });
      setMsg("Task queued successfully.");
      setFile(null);
      onIngest();
    } catch (err: any) {
      setMsg(`Error: ${err.message}`);
    }
    setUploading(false);
  }

  return (
    <div className="max-w-xl">
      <div className="mb-6 space-y-1">
        <h2 className="text-base font-medium text-white">Ingest Document</h2>
        <p className="text-sm text-zinc-500">Upload a PDF to parse, chunk, and embed into the graph.</p>
      </div>
      
      <form onSubmit={handleUpload} className="space-y-6">
        <div className="relative group">
          <input 
            type="file" 
            accept=".pdf" 
            onChange={(e) => setFile(e.target.files?.[0] || null)} 
            className="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-10" 
          />
          <div className={`border border-dashed rounded-lg p-12 text-center transition-colors ${file ? "border-zinc-500 bg-zinc-900" : "border-zinc-800 hover:border-zinc-600 bg-[#0a0a0a]"}`}>
            <div className="flex flex-col items-center gap-3">
              <div className={`p-3 rounded-full ${file ? "bg-white text-black" : "bg-zinc-900 text-zinc-500 group-hover:text-zinc-300"}`}>
                <Icons.Upload />
              </div>
              {file ? (
                <div>
                  <div className="text-sm text-white font-medium">{file.name}</div>
                  <div className="text-xs text-zinc-500 mt-1">{(file.size / 1024 / 1024).toFixed(2)} MB</div>
                </div>
              ) : (
                <div>
                  <div className="text-sm font-medium text-zinc-300">Click or drag PDF here</div>
                  <div className="text-xs text-zinc-500 mt-1">Up to 50MB per file</div>
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <Button type="submit" disabled={!file || uploading}>
            {uploading ? "Queuing..." : "Process Document"}
          </Button>
          {msg && (
            <span className={`text-sm ${msg.startsWith("Error") ? "text-red-500" : "text-zinc-400"}`}>
              {msg}
            </span>
          )}
        </div>
      </form>
    </div>
  );
}

function SearchPanel() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);

  async function handleSearch(e: FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    setSearching(true);
    try {
      const data: any = await apiFetch("/api/v1/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, limit: 5 })
      });
      setResults(data.results || []);
    } catch (e) {
      console.error(e);
    }
    setSearching(false);
  }

  return (
    <div className="space-y-6">
      <form onSubmit={handleSearch} className="relative max-w-2xl">
        <Icons.Search />
        <div className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500">
          <Icons.Search />
        </div>
        <input
          type="text"
          value={q}
          onChange={(e: any) => setQ(e.target.value)}
          placeholder="Semantic search across knowledge base..."
          className="w-full bg-[#0a0a0a] border border-zinc-800 text-zinc-100 rounded-lg pl-10 pr-4 py-3 text-sm focus:border-zinc-500 focus:outline-none transition-colors"
        />
        <Button type="submit" variant="ghost" disabled={searching} className="absolute right-1 top-1 !px-3 !py-2 text-xs">
          {searching ? "Searching..." : "Search"}
        </Button>
      </form>

      <div className="grid gap-4 max-w-4xl">
        {results.map((r, i) => (
          <Card key={i} className="p-5 flex flex-col gap-3 group hover:border-zinc-700 transition-colors">
            <div className="flex items-center gap-2">
              <span className="text-xs bg-zinc-900 text-zinc-400 px-2 py-0.5 rounded border border-zinc-800">Score: {r.score.toFixed(3)}</span>
              <span className="text-xs font-medium text-zinc-300">{r.filename}</span>
              <span className="text-xs text-zinc-600">p. {r.page_num}</span>
            </div>
            <div className="text-sm leading-relaxed text-zinc-300 prose prose-invert max-w-none">
              <ReactMarkdown>{r.text_content}</ReactMarkdown>
            </div>
          </Card>
        ))}
        {results.length === 0 && q && !searching && (
          <div className="text-sm text-zinc-600">No semantic matches found.</div>
        )}
      </div>
    </div>
  );
}

function KeysPanel() {
  const [keys, setKeys] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [newKey, setNewKey] = useState<any>(null);
  const [copied, setCopied] = useState("");

  useEffect(() => {
    loadKeys();
  }, []);

  async function loadKeys() {
    try {
      const data = await apiFetch<any[]>("/api/v1/keys");
      setKeys(data);
    } catch (e) { console.error(e); }
  }

  async function generateKey(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    try {
      const data: any = await apiFetch("/api/v1/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: name })
      });
      setNewKey(data);
      setName("");
      loadKeys();
    } catch (e) { console.error(e); }
    setLoading(false);
  }

  async function revokeKey(id: string) {
    if (!confirm("Revoke this key? MCP clients using it will immediately lose access.")) return;
    try {
      await apiFetch(`/api/v1/keys/${id}`, { method: "DELETE" });
      loadKeys();
    } catch (e) { console.error(e); }
  }

  const copyToClipboard = (text: string, type: string) => {
    navigator.clipboard.writeText(text);
    setCopied(type);
    setTimeout(() => setCopied(""), 2000);
  };

  return (
    <div className="max-w-3xl space-y-8">
      <div className="space-y-1">
        <h2 className="text-base font-medium text-white">API Keys</h2>
        <p className="text-sm text-zinc-500">Generate keys to connect external AI agents (like Cursor or Claude Code) via MCP.</p>
      </div>

      <form onSubmit={generateKey} className="flex gap-3">
        <Input 
          placeholder="Key name (e.g., 'Claude Desktop')" 
          value={name} onChange={(e: any) => setName(e.target.value)}
          className="max-w-xs"
        />
        <Button type="submit" disabled={loading || !name.trim()}>Generate</Button>
      </form>

      {newKey && (
        <Card className="p-5 border-zinc-700 bg-zinc-900/50 flex flex-col gap-4">
          <div className="flex items-center gap-2 text-white text-sm font-medium">
            <Icons.Check /> Key Generated Successfully
          </div>
          <p className="text-xs text-zinc-400">Copy these values now. You won't be able to see the secret token again.</p>
          
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-xs text-zinc-500">Secret Token</label>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-black border border-zinc-800 px-3 py-2 rounded text-xs text-zinc-300 font-mono truncate">
                  {newKey.plain_token}
                </code>
                <Button variant="secondary" onClick={() => copyToClipboard(newKey.plain_token, 'token')} className="!px-3">
                  {copied === 'token' ? <Icons.Check /> : <Icons.Copy />}
                </Button>
              </div>
            </div>
            
            <div className="space-y-1.5">
              <label className="text-xs text-zinc-500">MCP Client Configuration URL</label>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-black border border-zinc-800 px-3 py-2 rounded text-xs text-zinc-300 font-mono truncate">
                  {`${API_BASE}/mcp/sse?token=${newKey.plain_token}`}
                </code>
                <Button variant="secondary" onClick={() => copyToClipboard(`${API_BASE}/mcp/sse?token=${newKey.plain_token}`, 'url')} className="!px-3">
                  {copied === 'url' ? <Icons.Check /> : <Icons.Copy />}
                </Button>
              </div>
            </div>
          </div>
        </Card>
      )}

      <div className="grid gap-3">
        {keys.map(k => (
          <Card key={k.id} className="p-4 flex items-center justify-between group">
            <div>
              <div className="text-sm font-medium text-zinc-300">{k.name}</div>
              <div className="text-xs text-zinc-500 mt-1 flex items-center gap-3">
                <span>Created {new Date(k.created_at).toLocaleDateString()}</span>
                <span className="font-mono">...{k.token_hint}</span>
              </div>
            </div>
            <Button variant="danger" className="opacity-0 group-hover:opacity-100" onClick={() => revokeKey(k.id)}>
              Revoke
            </Button>
          </Card>
        ))}
        {keys.length === 0 && <p className="text-sm text-zinc-600">No active API keys.</p>}
      </div>
    </div>
  );
}

function SettingsPanel({ config, onSave }: any) {
  const [provider, setProvider] = useState(config.llm_provider || "gemini");
  const [geminiKey, setGeminiKey] = useState("");
  const [nvidiaKey, setNvidiaKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setMsg("");
    try {
      const payload = {
        llm_provider: provider,
        gemini_api_key: geminiKey || undefined,
        nvidia_api_key: nvidiaKey || undefined,
      };
      await apiFetch("/api/v1/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      setMsg("Settings saved.");
      setGeminiKey("");
      setNvidiaKey("");
      onSave();
    } catch (err: any) {
      setMsg(err.message);
    }
    setSaving(false);
  }

  return (
    <div className="max-w-xl">
      <div className="mb-8 space-y-1">
        <h2 className="text-base font-medium text-white">System Settings</h2>
        <p className="text-sm text-zinc-500">Configure LLM providers and database parameters.</p>
      </div>

      <form onSubmit={handleSave} className="space-y-8">
        <div className="space-y-4">
          <label className="text-sm font-medium text-zinc-300">LLM Provider</label>
          <div className="grid grid-cols-2 gap-3">
            <label className={`cursor-pointer rounded-lg border p-4 transition-colors ${provider === 'gemini' ? 'border-white bg-zinc-900' : 'border-zinc-800 bg-[#0a0a0a] hover:border-zinc-700'}`}>
              <div className="flex items-center gap-2 mb-1">
                <input type="radio" name="provider" value="gemini" checked={provider === 'gemini'} onChange={() => setProvider('gemini')} className="accent-white" />
                <span className="text-sm font-medium text-white">Google Gemini</span>
              </div>
              <p className="text-xs text-zinc-500 pl-5">Free tier extraction</p>
            </label>
            <label className={`cursor-pointer rounded-lg border p-4 transition-colors ${provider === 'nvidia' ? 'border-white bg-zinc-900' : 'border-zinc-800 bg-[#0a0a0a] hover:border-zinc-700'}`}>
              <div className="flex items-center gap-2 mb-1">
                <input type="radio" name="provider" value="nvidia" checked={provider === 'nvidia'} onChange={() => setProvider('nvidia')} className="accent-white" />
                <span className="text-sm font-medium text-white">Nvidia NIM</span>
              </div>
              <p className="text-xs text-zinc-500 pl-5">Developer program</p>
            </label>
          </div>
        </div>

        <div className="space-y-4 pt-4 border-t border-zinc-900">
          <div className="flex items-center justify-between">
            <label className="text-sm font-medium text-zinc-300">API Credentials</label>
            {config.gemini_has_key && provider === 'gemini' && <span className="text-xs text-zinc-400 bg-zinc-900 px-2 py-0.5 rounded">Configured</span>}
            {config.nvidia_has_key && provider === 'nvidia' && <span className="text-xs text-zinc-400 bg-zinc-900 px-2 py-0.5 rounded">Configured</span>}
          </div>
          
          {provider === 'gemini' ? (
            <div className="space-y-2">
              <label className="text-xs text-zinc-500">Update Gemini API Key</label>
              <Input type="password" value={geminiKey} onChange={(e: any) => setGeminiKey(e.target.value)} placeholder="AIzaSy..." />
            </div>
          ) : (
            <div className="space-y-2">
              <label className="text-xs text-zinc-500">Update Nvidia API Key</label>
              <Input type="password" value={nvidiaKey} onChange={(e: any) => setNvidiaKey(e.target.value)} placeholder="nvapi-..." />
            </div>
          )}
        </div>

        <div className="flex items-center gap-4 pt-4">
          <Button type="submit" disabled={saving}>
            {saving ? "Saving..." : "Save Settings"}
          </Button>
          {msg && <span className="text-sm text-zinc-400">{msg}</span>}
        </div>
      </form>
    </div>
  );
}
