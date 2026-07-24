"use client";

import React, { useState, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";

let API_BASE = "http://127.0.0.1:8000";
if (typeof window !== "undefined") {
  API_BASE = `http://${window.location.hostname}:8000`;
}

const fetchWithTimeout = async (url: string, options: RequestInit = {}, timeoutMs = 0) => {
  if (!timeoutMs) {
    return fetch(url, options);
  }
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal
    });
    clearTimeout(id);
    return response;
  } catch (error) {
    clearTimeout(id);
    throw error;
  }
};

// MarkdownRenderer replaced by react-markdown

interface Task {
  id: number;
  file_path: string;
  status: "pending" | "processing" | "completed" | "failed";
  attempts: number;
  error_message: string | null;
  progress_info: string | null;
  created_at: string;
}

interface Document {
  id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
}

interface AgentToken {
  token: string;
  description: string;
  created_at: string;
}

// Custom SVG Icons (Heroicons styled)
const Icon = {
  Library: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 0 0 6 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 0 1 6 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 0 1 6-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0 0 18 18a8.967 8.967 0 0 0-6 2.292m0-14.25v14.25" />
    </svg>
  ),
  Search: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.637 10.637Z" />
    </svg>
  ),
  Key: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 5.25a3 3 0 0 1 3 3m3 0a6 6 0 0 1-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1 1 21.75 8.25Z" />
    </svg>
  ),
  Settings: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.43l-1.003.828c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.43l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
    </svg>
  ),
  Upload: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V9.75m0 0 3 3m-3-3-3 3M6.75 19.5a4.5 4.5 0 0 1-1.41-8.775 5.25 5.25 0 0 1 10.233-2.33 3 3 0 0 1 3.758 3.848A3.752 3.752 0 0 1 18 19.5H6.75Z" />
    </svg>
  ),
  Trash: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
    </svg>
  ),
  Check: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
    </svg>
  ),
  Error: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z" />
    </svg>
  ),
  Copy: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 0 1-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H5.25m11.9-3.664A2.251 2.251 0 0 0 15 2.25h-3a2.251 2.251 0 0 0-2.15 1.586m5.8 0c.065.21.1.433.1.664v.75h-6V4.5c0-.231.035-.454.1-.664M6.75 7.5H4.875c-.621 0-1.125.504-1.125 1.125v12c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V16.5a9 9 0 0 0-9-9Z" />
    </svg>
  ),
  CopySuccess: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12c0 1.268-.63 2.39-1.593 3.068a3.745 3.745 0 0 1-1.043 3.296 3.745 3.745 0 0 1-3.296 1.043A3.745 3.745 0 0 1 12 21c-1.268 0-2.39-.63-3.068-1.593a3.746 3.746 0 0 1-3.296-1.043 3.745 3.745 0 0 1-1.043-3.296A3.745 3.745 0 0 1 3 12c0-1.268.63-2.39 1.593-3.068a3.745 3.745 0 0 1 1.043-3.296 3.746 3.746 0 0 1 3.296-1.043A3.746 3.746 0 0 1 12 3c1.268 0 2.39.63 3.068 1.593a3.746 3.746 0 0 1 3.296 1.043 3.746 3.746 0 0 1 1.043 3.296A3.745 3.745 0 0 1 21 12Z" />
    </svg>
  ),
  DoubleChevronDown: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 5.25 7.5 7.5 7.5-7.5m-15 6 7.5 7.5 7.5-7.5" />
    </svg>
  ),
  ArrowPath: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99" />
    </svg>
  ),
  Database: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.125 16.556 18 12 18s-8.25-1.875-8.25-4.125v-3.75m16.5 0v3.75" />
    </svg>
  ),
  FileText: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
    </svg>
  ),
  Chart: (props: React.SVGProps<SVGSVGElement>) => (
    <svg fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" {...props}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v5.625c0 .621-.504 1.125-1.125 1.125h-2.25A1.125 1.125 0 0 1 3 18.75v-5.625ZM18 10.125c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v8.625c0 .621-.504 1.125-1.125 1.125h-2.25A1.125 1.125 0 0 1 18 18.75v-8.625ZM10.5 5.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v13.125c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V5.625Z" />
    </svg>
  )
};

export default function DashboardPage() {
  // Authentication & Configuration State
  const [token, setToken] = useState<string>("");
  const [isAuthRequired, setIsAuthRequired] = useState<boolean>(false);
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [configInfo, setConfigInfo] = useState<any>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  
  // App Navigation
  const [activeTab, setActiveTab] = useState<"library" | "search" | "metrics" | "agent-keys" | "settings">("library");

  // System states
  const [documents, setDocuments] = useState<Document[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agentTokens, setAgentTokens] = useState<AgentToken[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  
  const [metrics, setMetrics] = useState<{
    ingestion: { prompt: number; completion: number; total: number };
    sandbox: { prompt: number; completion: number; total: number };
  }>({
    ingestion: { prompt: 0, completion: 0, total: 0 },
    sandbox: { prompt: 0, completion: 0, total: 0 }
  });
  
  // Settings Form State
  const [settingsForm, setSettingsForm] = useState({
    provider: "gemini",
    gemini_api_key: "",
    nvidia_api_key: "",
    llm_model: "gemini-1.5-flash",
    embedding_model: "text-embedding-004",
    chunk_size: 2500,
    chunk_overlap: 250
  });

  // Upload PDF State
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState<boolean>(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<boolean>(false);
  const [isDragging, setIsDragging] = useState<boolean>(false);

  // Search RAG State
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [searchLimit, setSearchLimit] = useState<number>(5);
  const [searchResults, setSearchResults] = useState<any>(null);
  const [searching, setSearching] = useState<boolean>(false);
  const [chatHistory, setChatHistory] = useState<{ query: string; results: any; synthesis?: string; timestamp: string }[]>([]);

  // New Agent Key State
  const [newTokenDesc, setNewTokenDesc] = useState<string>("");
  const [generatedKey, setGeneratedKey] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState<boolean>(false);
  
  // Ingestion Queue History View State
  const [showHistory, setShowHistory] = useState<boolean>(false);

  // Sidebar collapse state
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false);

  // Fetch helper with auth header
  const authFetch = useCallback(async (path: string, options: RequestInit = {}) => {
    const headers = new Headers(options.headers || {});
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    const timeoutMs = (path.startsWith("/api/v1/ingest") || path.startsWith("/api/v1/search")) ? 0 : 10000;
    const res = await fetchWithTimeout(`${API_BASE}${path}`, { ...options, headers }, timeoutMs);
    if (res.status === 401) {
      setIsAuthenticated(false);
      localStorage.removeItem("pdfspecs_agent_token");
      throw new Error("Unauthorized");
    }
    return res;
  }, [token]);

  // Load public setup state
  const loadPublicInfo = async () => {
    try {
      const res = await fetchWithTimeout(`${API_BASE}/api/v1/config/provider`, {}, 5000);
      if (res.ok) {
        setConnectionError(null);
        const data = await res.json();
        setConfigInfo(data);
        setIsAuthRequired(data.auth_required);
        setSettingsForm({
          provider: data.provider,
          gemini_api_key: "",
          nvidia_api_key: "",
          llm_model: data.llm_model,
          embedding_model: data.embedding_model,
          chunk_size: data.chunk_size,
          chunk_overlap: data.chunk_overlap
        });

        if (!data.auth_required) {
          setIsAuthenticated(true);
        } else {
          const localToken = localStorage.getItem("pdfspecs_agent_token");
          if (localToken) {
            setToken(localToken);
            setIsAuthenticated(true);
          }
        }
      } else {
        setConnectionError(`Backend server returned status code ${res.status}`);
      }
    } catch (e: any) {
      console.warn("Failed to connect to backend:", e);
      setConnectionError(`Could not connect to backend server at ${API_BASE}. Please make sure you started the backend server using 'python3 -m src.server'.`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPublicInfo();
  }, []);

  // Fetch Dashboard Data
  const fetchData = useCallback(async () => {
    if (!isAuthenticated) return;
    try {
      const docsRes = await authFetch("/api/v1/documents");
      if (docsRes.ok) setDocuments(await docsRes.json());

      const tasksRes = await authFetch("/api/v1/tasks");
      if (tasksRes.ok) setTasks(await tasksRes.json());

      const keysRes = await authFetch("/api/v1/auth/tokens");
      if (keysRes.ok) setAgentTokens(await keysRes.json());

      const metricsRes = await authFetch("/api/v1/metrics");
      if (metricsRes.ok) setMetrics(await metricsRes.json());
    } catch (e) {
      console.warn("Failed to fetch dashboard data:", e);
    }
  }, [isAuthenticated, authFetch]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 4000);
    return () => clearInterval(interval);
  }, [fetchData]);

  // Login handler
  const handleLogin = (e: React.FormEvent) => {
    e.preventDefault();
    if (token.trim()) {
      localStorage.setItem("pdfspecs_agent_token", token.trim());
      setIsAuthenticated(true);
      fetchData();
    }
  };

  // Save Settings Config
  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const payload: any = { ...settingsForm };
      if (!payload.gemini_api_key) delete payload.gemini_api_key;
      if (!payload.nvidia_api_key) delete payload.nvidia_api_key;

      const res = await authFetch("/api/v1/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        alert("Settings saved successfully!");
        loadPublicInfo();
      } else {
        alert("Failed to save settings.");
      }
    } catch (e) {
      alert("Error saving settings.");
    }
  };

  // Upload Ingestion
  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedFile) return;
    
    setUploading(true);
    setUploadError(null);
    setUploadSuccess(false);

    const formData = new FormData();
    formData.append("file", selectedFile);

    try {
      const res = await authFetch("/api/v1/ingest", {
        method: "POST",
        body: formData
      });

      if (res.ok) {
        setUploadSuccess(true);
        setSelectedFile(null);
        fetchData();
      } else {
        const errData = await res.json();
        setUploadError(errData.detail || "Ingestion failed.");
      }
    } catch (e) {
      setUploadError("Network error uploading file.");
    } finally {
      setUploading(false);
    }
  };

  // Delete Document
  const handleDeleteDoc = async (id: string) => {
    if (!confirm("Are you sure you want to delete this document? All chunks and graph relationships will be deleted.")) return;
    try {
      const res = await authFetch(`/api/v1/documents/${id}`, { method: "DELETE" });
      if (res.ok) fetchData();
    } catch (e) {
      alert("Failed to delete document.");
    }
  };

  // Generate Agent Token
  const handleGenerateToken = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTokenDesc.trim()) return;

    try {
      const res = await authFetch("/api/v1/auth/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: newTokenDesc.trim() })
      });

      if (res.ok) {
        const data = await res.json();
        setGeneratedKey(data.token);
        setCopiedKey(false);
        setNewTokenDesc("");
        if (!isAuthRequired) {
          localStorage.setItem("pdfspecs_agent_token", data.token);
          setToken(data.token);
        }
        fetchData();
        loadPublicInfo();
      }
    } catch (e) {
      alert("Failed to generate token.");
    }
  };

  // Revoke Agent Token
  const handleRevokeToken = async (tok: string) => {
    if (!confirm("Revoke this token? Any agent using this key will be blocked immediately.")) return;
    try {
      const res = await authFetch(`/api/v1/auth/tokens/${tok}`, { method: "DELETE" });
      if (res.ok) {
        if (tok === token) {
          setIsAuthenticated(false);
          localStorage.removeItem("pdfspecs_agent_token");
        }
        fetchData();
        loadPublicInfo();
      }
    } catch (e) {
      alert("Failed to revoke token.");
    }
  };

  // RAG Search
  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) return;

    setSearching(true);
    setSearchResults(null);
    try {
      const res = await authFetch(`/api/v1/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: searchQuery,
          limit: searchLimit
        })
      });

      if (res.ok) {
        const data = await res.json();
        setSearchResults(data.results || "No content returned.");
        // Add to local chat history for the chat-like layout
        setChatHistory(prev => [
          { 
            query: searchQuery, 
            results: data.results, 
            synthesis: data.synthesis || "",
            timestamp: new Date().toLocaleTimeString() 
          },
          ...prev
        ]);
        setSearchQuery("");
        fetchData();
      } else {
        setSearchResults("Error running search query on backend.");
      }
    } catch (e) {
      setSearchResults("Error running search query.");
    } finally {
      setSearching(false);
    }
  };

  // Copy helper
  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedKey(true);
    setTimeout(() => setCopiedKey(false), 2000);
  };

  // Drag-and-drop triggers
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      const file = files[0];
      if (file.type === "application/pdf") {
        setSelectedFile(file);
      } else {
        setUploadError("Only PDF files are supported.");
      }
    }
  };

  // Parse Markdown matches in search output
  const parseSearchResults = (rawOutput: string) => {
    if (!rawOutput) return null;
    if (rawOutput.startsWith("No matches found")) {
      return (
        <div className="flex flex-col items-center justify-center py-12 text-slate-500 bg-slate-950/20 rounded-2xl border border-slate-900 border-dashed p-6">
          <Icon.Error className="w-8 h-8 text-slate-600 mb-2" />
          <p className="text-sm text-center">{rawOutput}</p>
        </div>
      );
    }

    // Parse Markdown output string generated by `search_knowledge`
    const blocks: any[] = [];
    const lines = rawOutput.split("\n");
    let currentBlock: any = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (line.startsWith("### ")) {
        if (currentBlock) blocks.push(currentBlock);
        // Page name & details
        const title = line.replace("### ", "");
        currentBlock = { title, id: "", score: "0.0", summary: "", snippet: "" };
      } else if (currentBlock) {
        if (line.startsWith("- **Chunk ID**:")) {
          currentBlock.id = line.replace("- **Chunk ID**:", "").replace(/`/g, "").trim();
        } else if (line.startsWith("- **Relevance Score**:")) {
          currentBlock.score = line.replace("- **Relevance Score**:", "").replace(/`/g, "").trim();
        } else if (line.startsWith("- **Summary**:")) {
          currentBlock.summary = line.replace("- **Summary**:", "").trim();
        } else if (line.startsWith("- **Text Snippet**:")) {
          currentBlock.snippet = line.replace("- **Text Snippet**:", "").replace(/"/g, "").trim();
        }
      }
    }
    if (currentBlock) blocks.push(currentBlock);

    return (
      <div className="space-y-6">
        {blocks.map((b, index) => {
          const percentage = Math.round(parseFloat(b.score) * 100) || 0;
          return (
            <div key={index} className="bg-slate-900/20 border border-slate-800/80 rounded-2xl p-5 hover:border-slate-700/50 transition-all duration-200">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
                <div className="flex items-center space-x-3">
                  <span className="flex items-center justify-center w-7 h-7 bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 text-xs font-bold rounded-lg font-mono">
                    {index + 1}
                  </span>
                  <h4 className="font-semibold text-slate-200 text-sm truncate max-w-sm sm:max-w-md" title={b.title}>
                    {b.title}
                  </h4>
                </div>

                {/* Relevance Match Meter */}
                <div className="flex items-center space-x-2 shrink-0">
                  <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Match:</span>
                  <div className="h-1.5 w-20 bg-slate-950 rounded-full overflow-hidden border border-slate-800">
                    <div 
                      className="h-full bg-gradient-to-r from-indigo-500 to-violet-500 rounded-full"
                      style={{ width: `${percentage}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono font-bold text-indigo-400">{percentage}%</span>
                </div>
              </div>

              <div className="space-y-3 pl-0 sm:pl-10">
                {b.id && (
                  <div className="flex items-center space-x-2 text-xs">
                    <span className="text-slate-500">Chunk ID:</span>
                    <code className="bg-slate-950 border border-slate-900 px-2 py-0.5 rounded text-slate-400 font-mono text-[10px] select-all">
                      {b.id}
                    </code>
                  </div>
                )}
                {b.summary && (
                  <p className="text-xs text-slate-400 leading-relaxed">
                    <span className="font-semibold text-slate-300">Summary: </span>
                    {b.summary}
                  </p>
                )}
                {b.snippet && (
                  <div className="bg-slate-950/60 border border-slate-900/80 p-3.5 rounded-xl text-slate-300/90 text-xs font-mono leading-relaxed relative group">
                    <p className="italic font-sans text-slate-400 line-clamp-3">"{b.snippet}"</p>
                    <button 
                      onClick={() => copyToClipboard(b.snippet)}
                      className="absolute right-2 top-2 p-1.5 bg-slate-900 hover:bg-slate-850 text-slate-500 hover:text-white border border-slate-800 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
                      title="Copy source text"
                    >
                      <Icon.Copy className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  // Loading Screen
  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 text-white flex items-center justify-center relative overflow-hidden font-sans">
        {/* Glow circles */}
        <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-purple-900/10 rounded-full blur-[100px]" />
        <div className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-indigo-900/10 rounded-full blur-[100px]" />
        
        <div className="flex flex-col items-center z-10">
          <div className="relative flex h-12 w-12 mb-6">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-20"></span>
            <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-indigo-500"></div>
          </div>
          <p className="text-slate-400 text-sm font-medium tracking-wide">Initializing OpenPDFSpecs Local Node...</p>
        </div>
      </div>
    );
  }

  // Connection Error Screen
  if (connectionError) {
    return (
      <div className="min-h-screen bg-slate-950 text-white flex items-center justify-center p-4 relative overflow-hidden font-sans">
        <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-rose-900/5 rounded-full blur-[100px]" />
        <div className="w-full max-w-md bg-slate-900/40 backdrop-blur-2xl border border-rose-500/15 rounded-3xl p-8 shadow-2xl relative z-10">
          <div className="flex justify-center mb-5 text-rose-500">
            <Icon.Error className="w-12 h-12 animate-pulse" />
          </div>
          <h1 className="text-lg font-bold text-center mb-3 text-rose-400 tracking-tight">
            Connection Failed
          </h1>
          <p className="text-slate-400 text-center text-xs mb-8 leading-relaxed">
            {connectionError}
          </p>
          <button
            onClick={() => {
              setLoading(true);
              setConnectionError(null);
              loadPublicInfo();
            }}
            className="w-full bg-slate-900 hover:bg-slate-800 active:bg-slate-950 text-white font-medium py-3 rounded-xl transition-all border border-slate-800 cursor-pointer shadow-lg shadow-black/20"
          >
            Retry Node Connection
          </button>
        </div>
      </div>
    );
  }

  // Authentication Required Screen (Login)
  if (!isAuthenticated && isAuthRequired) {
    return (
      <div className="min-h-screen bg-slate-950 text-white flex items-center justify-center p-4 relative overflow-hidden font-sans">
        <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-indigo-900/10 rounded-full blur-[120px] animate-float-1" />
        <div className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-purple-900/10 rounded-full blur-[120px] animate-float-2" />
        
        <div className="w-full max-w-md bg-slate-900/35 backdrop-blur-2xl border border-slate-850 rounded-3xl p-8 shadow-2xl relative z-10">
          <div className="flex justify-center mb-5">
            <div className="w-12 h-12 bg-gradient-to-tr from-indigo-500 to-purple-500 rounded-2xl flex items-center justify-center shadow-lg shadow-indigo-500/20">
              <Icon.Library className="w-6 h-6 text-white" />
            </div>
          </div>
          <h1 className="text-xl font-bold text-center mb-1 text-slate-100 tracking-tight">
            OpenPDFSpecs Console
          </h1>
          <p className="text-slate-400 text-center text-xs mb-8">
            Knowledgebase is secured. Paste your Agent Token to authorize.
          </p>

          <form onSubmit={handleLogin} className="space-y-5">
            <div>
              <label className="block text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-2">
                Agent access token
              </label>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="sk-pdfspecs-..."
                className="w-full bg-slate-950/80 border border-slate-850 rounded-xl px-4 py-3 text-white placeholder-slate-800 focus:outline-none focus:border-indigo-500/80 transition-colors text-sm font-mono"
                required
              />
            </div>
            <button
              type="submit"
              className="w-full bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 active:scale-[0.98] text-white font-medium py-3 rounded-xl transition-all shadow-lg shadow-indigo-600/20"
            >
              Access Console
            </button>
          </form>
        </div>
      </div>
    );
  }

  // Partition tasks
  const activeTasks = tasks.filter((t) => t.status === "pending" || t.status === "processing");
  const historyTasks = tasks.filter((t) => t.status === "completed" || t.status === "failed");

  // Setup warning banner
  const showBootstrapWarning = !isAuthRequired && (!configInfo?.has_gemini_key && !configInfo?.has_nvidia_key);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300 font-sans flex flex-col md:flex-row relative">
      
      {/* Background radial gradient blobs (drifting mesh) */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none z-0">
        <div className="absolute top-[-20%] left-[-20%] w-[60%] h-[60%] bg-purple-900/10 rounded-full blur-[150px] animate-float-1" />
        <div className="absolute bottom-[-20%] right-[-20%] w-[60%] h-[60%] bg-indigo-900/10 rounded-full blur-[150px] animate-float-2" />
      </div>

      {/* LEFT NAVIGATION SIDEBAR */}
      <aside className={`md:w-72 border-b md:border-b-0 md:border-r border-slate-900/60 bg-slate-950/50 backdrop-blur-2xl p-6 flex flex-col shrink-0 z-20 transition-all duration-300 relative ${sidebarCollapsed ? 'md:w-20' : 'md:w-72'}`}>
        
        {/* Sidebar Header & Toggle */}
        <div className="flex items-center justify-between mb-8">
          <div className={`flex items-center space-x-3 overflow-hidden transition-all ${sidebarCollapsed ? 'w-0 opacity-0' : 'w-full opacity-100'}`}>
            <div className="w-8 h-8 bg-gradient-to-tr from-indigo-500 to-purple-500 rounded-xl flex items-center justify-center shrink-0">
              <Icon.Library className="w-4.5 h-4.5 text-white" />
            </div>
            <span className="font-bold text-base bg-gradient-to-r from-indigo-300 to-purple-300 bg-clip-text text-transparent tracking-tight">
              OpenPDFSpecs
            </span>
          </div>

          <button 
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            className="text-slate-500 hover:text-slate-300 p-1.5 hover:bg-slate-900/50 rounded-lg transition-colors hidden md:block"
          >
            {sidebarCollapsed ? "→" : "←"}
          </button>
        </div>

        {/* Sidebar Nav Links */}
        <nav className="flex-1 space-y-1.5">
          <button
            onClick={() => setActiveTab("library")}
            className={`w-full flex items-center ${sidebarCollapsed ? 'justify-center px-0' : 'px-4'} py-3 rounded-xl text-sm font-medium transition-all ${
              activeTab === "library" 
                ? "bg-indigo-600/10 text-indigo-400 border border-indigo-500/15" 
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-900/30 border border-transparent"
            }`}
            title="Library & Ingestion"
          >
            <Icon.Library className={`w-5 h-5 shrink-0 ${sidebarCollapsed ? 'mr-0' : 'mr-3'}`} />
            {!sidebarCollapsed && <span>Library & Queue</span>}
          </button>
          
          <button
            onClick={() => setActiveTab("search")}
            className={`w-full flex items-center ${sidebarCollapsed ? 'justify-center px-0' : 'px-4'} py-3 rounded-xl text-sm font-medium transition-all ${
              activeTab === "search" 
                ? "bg-indigo-600/10 text-indigo-400 border border-indigo-500/15" 
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-900/30 border border-transparent"
            }`}
            title="RAG Search Sandbox"
          >
            <Icon.Search className={`w-5 h-5 shrink-0 ${sidebarCollapsed ? 'mr-0' : 'mr-3'}`} />
            {!sidebarCollapsed && <span>RAG Sandbox</span>}
          </button>

          <button
            onClick={() => setActiveTab("metrics")}
            className={`w-full flex items-center ${sidebarCollapsed ? 'justify-center px-0' : 'px-4'} py-3 rounded-xl text-sm font-medium transition-all ${
              activeTab === "metrics" 
                ? "bg-indigo-600/10 text-indigo-400 border border-indigo-500/15" 
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-900/30 border border-transparent"
            }`}
            title="Token Metrics Dashboard"
          >
            <Icon.Chart className={`w-5 h-5 shrink-0 ${sidebarCollapsed ? 'mr-0' : 'mr-3'}`} />
            {!sidebarCollapsed && <span>Token Metrics</span>}
          </button>

          <button
            onClick={() => setActiveTab("agent-keys")}
            className={`w-full flex items-center ${sidebarCollapsed ? 'justify-center px-0' : 'px-4'} py-3 rounded-xl text-sm font-medium transition-all ${
              activeTab === "agent-keys" 
                ? "bg-indigo-600/10 text-indigo-400 border border-indigo-500/15" 
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-900/30 border border-transparent"
            }`}
            title="Agent API Keys"
          >
            <Icon.Key className={`w-5 h-5 shrink-0 ${sidebarCollapsed ? 'mr-0' : 'mr-3'}`} />
            {!sidebarCollapsed && <span>Agent Access</span>}
          </button>

          <button
            onClick={() => setActiveTab("settings")}
            className={`w-full flex items-center ${sidebarCollapsed ? 'justify-center px-0' : 'px-4'} py-3 rounded-xl text-sm font-medium transition-all ${
              activeTab === "settings" 
                ? "bg-indigo-600/10 text-indigo-400 border border-indigo-500/15" 
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-900/30 border border-transparent"
            }`}
            title="System Settings"
          >
            <Icon.Settings className={`w-5 h-5 shrink-0 ${sidebarCollapsed ? 'mr-0' : 'mr-3'}`} />
            {!sidebarCollapsed && <span>System Settings</span>}
          </button>
        </nav>

        {/* Sidebar Footer Node status */}
        <div className={`mt-auto pt-6 border-t border-slate-900/60 overflow-hidden transition-all duration-300 ${sidebarCollapsed ? 'w-0 opacity-0 h-0 p-0' : 'w-full opacity-100'}`}>
          <div className="bg-slate-900/25 border border-slate-900/80 rounded-2xl p-4 flex flex-col space-y-2.5">
            <div className="flex items-center space-x-2.5 text-xs text-slate-400">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span className="font-semibold text-slate-300">Local Node Active</span>
            </div>
            
            <div className="text-[10px] text-slate-500 font-mono space-y-1">
              <div className="flex justify-between">
                <span>Provider:</span>
                <span className="text-slate-400">{configInfo?.provider || "N/A"}</span>
              </div>
              <div className="flex justify-between">
                <span>Docs Indexed:</span>
                <span className="text-slate-400">{documents.length}</span>
              </div>
            </div>
          </div>
        </div>
      </aside>

      {/* MAIN CONTENT AREA */}
      <main className="flex-1 overflow-y-auto z-10 px-4 sm:px-8 py-8 animate-fade-in relative max-w-6xl mx-auto w-full">
        
        {/* Setup Warning banner */}
        {showBootstrapWarning && (
          <div className="mb-8 p-5 bg-amber-500/5 border border-amber-500/15 rounded-2xl flex items-start space-x-4">
            <span className="text-xl">⚠️</span>
            <div>
              <h3 className="font-semibold text-amber-300 text-sm mb-1">First-Time Setup Required</h3>
              <p className="text-slate-400 text-xs mb-4">
                To start indexing documents, you must first configure your Google Gemini or Nvidia API keys in settings and generate your first Agent Access token.
              </p>
              <button
                onClick={() => setActiveTab("settings")}
                className="bg-amber-500 hover:bg-amber-600 active:scale-[0.98] text-slate-950 font-semibold px-4 py-2 rounded-xl text-xs transition-all shadow-md shadow-amber-500/10 cursor-pointer"
              >
                Go to Settings
              </button>
            </div>
          </div>
        )}

        {/* TAB 1: LIBRARY & INGESTION QUEUE */}
        {activeTab === "library" && (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
            
            {/* Left Column: upload panel and task queue */}
            <div className="space-y-8 lg:col-span-1">
              
              {/* Upload panel */}
              <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl relative overflow-hidden group">
                <h2 className="text-sm font-bold uppercase tracking-wider mb-4 flex items-center space-x-2.5 text-slate-200">
                  <Icon.Upload className="w-5 h-5 text-indigo-400" /> 
                  <span>Upload Document</span>
                </h2>
                
                <form onSubmit={handleUpload} className="space-y-4">
                  <div 
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    onDrop={handleDrop}
                    className={`border border-dashed rounded-2xl p-8 flex flex-col items-center justify-center cursor-pointer transition-all duration-300 relative ${
                      isDragging 
                        ? "border-indigo-500 bg-indigo-500/5 shadow-lg shadow-indigo-500/5" 
                        : "border-slate-800 bg-slate-950/20 hover:border-slate-700/80 hover:bg-slate-950/40"
                    }`}
                  >
                    <input
                      type="file"
                      accept=".pdf"
                      onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
                      className="absolute inset-0 opacity-0 cursor-pointer"
                    />
                    <Icon.Upload className={`w-8 h-8 mb-3 transition-transform duration-300 ${isDragging ? "scale-110 text-indigo-400" : "text-slate-500"}`} />
                    <span className="text-xs font-semibold text-slate-300 text-center truncate max-w-full px-2">
                      {selectedFile ? selectedFile.name : "Drag & drop PDF catalog here"}
                    </span>
                    <span className="text-[10px] text-slate-500 mt-1">or click to browse local files</span>
                  </div>

                  {uploadError && (
                    <div className="text-rose-400 text-xs p-3 bg-rose-500/5 border border-rose-500/10 rounded-xl flex items-center space-x-2">
                      <Icon.Error className="w-4 h-4 text-rose-500 shrink-0" />
                      <span className="truncate">{uploadError}</span>
                    </div>
                  )}

                  {uploadSuccess && (
                    <div className="text-emerald-400 text-xs p-3 bg-emerald-500/5 border border-emerald-500/10 rounded-xl flex items-center space-x-2">
                      <Icon.Check className="w-4 h-4 text-emerald-500 shrink-0" />
                      <span>File queued! Processing runs in the background.</span>
                    </div>
                  )}

                  <button
                    type="submit"
                    disabled={!selectedFile || uploading}
                    className="w-full bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 disabled:from-slate-800 disabled:to-slate-800 disabled:text-slate-500 text-white font-medium py-3 rounded-xl transition-all shadow-lg shadow-indigo-600/15 disabled:shadow-none active:scale-[0.98]"
                  >
                    {uploading ? "Uploading PDF..." : "Index Document"}
                  </button>
                </form>
              </div>

              {/* Active queue task tracking */}
              <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl">
                <div className="flex justify-between items-center mb-5">
                  <h2 className="text-sm font-bold uppercase tracking-wider flex items-center space-x-2 text-indigo-400">
                    <Icon.ArrowPath className="w-4 h-4 animate-spin shrink-0" style={{ animationDuration: '6s' }} />
                    <span>Active Ingestions</span>
                  </h2>
                  <span className="text-[9px] uppercase font-bold tracking-widest text-slate-500 bg-slate-950/60 px-2 py-0.5 rounded-full border border-slate-900">
                    polling
                  </span>
                </div>

                <div className="space-y-4">
                  {activeTasks.length === 0 ? (
                    <div className="text-center py-6 bg-slate-950/20 border border-slate-900 border-dashed rounded-2xl p-4">
                      <p className="text-slate-600 text-xs">No active pipeline tasks.</p>
                    </div>
                  ) : (
                    activeTasks.map((t) => {
                      const fname = t.file_path.split("/").pop() || "";
                      const filenameMatch = fname.match(/^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})-(.+)$/);
                      const originalName = filenameMatch ? filenameMatch[2] : fname;
                      const fileId = filenameMatch ? filenameMatch[1] : (t.document_id || String(t.id));
                      
                      let progressData = { phase: "Pending", detail: "Waiting in queue...", progress: 0 };
                      if (t.progress_info) {
                          try {
                              progressData = JSON.parse(t.progress_info);
                          } catch(e) {
                              progressData.detail = t.progress_info;
                              if (t.status === "completed") progressData.progress = 100;
                              else if (t.status === "failed") progressData.progress = 0;
                              else progressData.progress = 50;
                          }
                      }

                      return (
                        <div key={t.id} className="p-4 bg-slate-950/40 border border-slate-900 rounded-2xl flex flex-col space-y-3">
                          <div className="flex justify-between items-start gap-3">
                            <div className="flex flex-col min-w-0">
                              <span className="text-xs font-semibold text-slate-200 truncate" title={originalName}>
                                {originalName}
                              </span>
                              <span className="text-[9px] text-slate-500 font-mono mt-0.5 truncate" title={`ID: ${fileId}`}>
                                id: {fileId.slice(0, 18)}...
                              </span>
                            </div>
                            <span className="text-[9px] uppercase font-mono font-bold tracking-wider px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20 animate-pulse shrink-0">
                              {t.status}
                            </span>
                          </div>

                          {/* Minimalist Progress Bar */}
                          <div className="pt-2">
                            <div className="flex justify-between items-end mb-1">
                              <span className="text-[10px] text-slate-400 font-medium">Progress</span>
                              <span className="text-[10px] font-bold text-indigo-400 font-mono">{progressData.progress}%</span>
                            </div>
                            <div className="h-1.5 w-full bg-slate-900 rounded-full overflow-hidden border border-slate-800/60">
                              <div 
                                className="h-full bg-gradient-to-r from-indigo-500 to-violet-500 transition-all duration-500 ease-out" 
                                style={{ width: `${progressData.progress}%` }}
                              />
                            </div>
                          </div>

                          {/* Minimalistic Expanding Details */}
                          <details className="group mt-1 cursor-pointer">
                            <summary className="text-[10px] text-slate-500 font-medium list-none flex items-center select-none hover:text-slate-300 transition-colors">
                              <Icon.DoubleChevronDown className="w-3 h-3 mr-1.5 transition-transform group-open:rotate-180" />
                              <span className="uppercase tracking-wider">{progressData.phase || "Processing"}</span>
                            </summary>
                            <div className="mt-2.5 text-[10px] text-amber-300/95 bg-amber-500/5 p-2 rounded-xl border border-amber-500/10 flex items-center animate-fade-in">
                              <span className="relative flex h-1.5 w-1.5 mr-2 shrink-0">
                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
                                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-500"></span>
                              </span>
                              <span className="truncate">{progressData.detail || "Working..."}</span>
                            </div>
                          </details>
                        </div>
                      );
                    })
                  )}
                </div>

                {/* Collapsible queue history log */}
                <div className="mt-6 border-t border-slate-900/60 pt-4">
                  <button
                    onClick={() => setShowHistory(!showHistory)}
                    className="w-full text-xs text-slate-400 hover:text-slate-200 bg-slate-950/30 border border-slate-900 hover:border-slate-850 rounded-xl py-2.5 transition-all flex items-center justify-between px-3 cursor-pointer"
                  >
                    <span className="font-semibold flex items-center space-x-2">
                      <Icon.FileText className="w-4 h-4 text-slate-500" />
                      <span>Ingestion Log History</span>
                    </span>
                    <div className="flex items-center space-x-2">
                      <span className="text-[9px] bg-slate-900 border border-slate-900 text-slate-400 px-2 py-0.5 rounded-full font-mono">
                        {historyTasks.length}
                      </span>
                      <span>{showHistory ? "▲" : "▼"}</span>
                    </div>
                  </button>

                  {showHistory && (
                    <div className="space-y-3 mt-3 max-h-[240px] overflow-y-auto pr-1">
                      {historyTasks.length === 0 ? (
                        <p className="text-slate-600 text-[10px] text-center py-4">No logged history.</p>
                      ) : (
                        historyTasks.map((t) => {
                          const fname = t.file_path.split("/").pop() || "";
                          const filenameMatch = fname.match(/^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})-(.+)$/);
                          const originalName = filenameMatch ? filenameMatch[2] : fname;
                          const fileId = filenameMatch ? filenameMatch[1] : (t.document_id || String(t.id));
                          const isSuccess = t.status === "completed";
                          return (
                            <div key={t.id} className="p-3 bg-slate-950/25 border border-slate-900 rounded-xl flex flex-col space-y-1.5">
                              <div className="flex justify-between items-start gap-2">
                                <div className="flex flex-col min-w-0 flex-1">
                                  <span className="text-xs font-semibold truncate" title={originalName}>
                                    {originalName}
                                  </span>
                                  <span className="text-[9px] text-slate-500 font-mono mt-0.5 truncate">
                                    id: {fileId.slice(0, 13)}...
                                  </span>
                                </div>
                                <div className={`w-3.5 h-3.5 rounded-full flex items-center justify-center shrink-0 border ${
                                  isSuccess 
                                    ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400" 
                                    : "bg-rose-500/10 border-rose-500/30 text-rose-400"
                                }`}>
                                  {isSuccess ? <Icon.Check className="w-2 h-2" /> : <span className="text-[8px] font-bold">!</span>}
                                </div>
                              </div>
                              {t.error_message && (
                                <div className="text-[10px] text-rose-400 bg-rose-500/5 p-2 rounded-lg border border-rose-500/10 font-mono break-all leading-normal">
                                  {t.error_message}
                                </div>
                              )}
                            </div>
                          );
                        })
                      )}
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Right Column: document catalog library */}
            <div className="lg:col-span-2">
              <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl h-full flex flex-col min-h-[500px]">
                <h2 className="text-sm font-bold uppercase tracking-wider mb-6 flex items-center space-x-2.5 text-slate-200">
                  <Icon.Database className="w-5 h-5 text-indigo-400" />
                  <span>Document Library</span>
                </h2>

                <div className="flex-1 overflow-y-auto pr-1">
                  {documents.length === 0 ? (
                    <div className="flex flex-col items-center justify-center py-24 text-center">
                      <div className="w-16 h-16 bg-slate-900/40 rounded-full flex items-center justify-center mb-4 border border-slate-800">
                        <Icon.Database className="w-8 h-8 text-slate-600" />
                      </div>
                      <p className="text-slate-400 font-semibold mb-1 text-sm">No documents ingested</p>
                      <p className="text-slate-600 text-xs">Upload a PDF catalog to configure RAG and Graph vectors.</p>
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      {documents.map((doc) => {
                        const size_kb = (doc.size_bytes / 1024).toFixed(1);
                        return (
                          <div key={doc.id} className="p-4 bg-slate-950/35 border border-slate-900/80 hover:border-slate-800 hover:bg-slate-950/60 rounded-2xl flex flex-col justify-between gap-4 transition-all duration-200">
                            <div className="flex items-start space-x-3.5 min-w-0">
                              <div className="w-9 h-9 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-xl flex items-center justify-center shrink-0">
                                <Icon.FileText className="w-5 h-5" />
                              </div>
                              <div className="min-w-0 flex-1">
                                <h4 className="font-semibold text-slate-200 text-sm truncate" title={doc.filename}>
                                  {doc.filename}
                                </h4>
                                <p className="text-[10px] text-slate-500 mt-1 font-mono">
                                  UUID: {doc.id.slice(0, 18)}...
                                </p>
                              </div>
                            </div>
                            
                            <div className="flex justify-between items-center border-t border-slate-900 pt-3 mt-1.5">
                              <span className="text-[10px] text-slate-500 font-mono">Size: {size_kb} KB</span>
                              <button
                                onClick={() => handleDeleteDoc(doc.id)}
                                className="text-slate-500 hover:text-rose-400 p-2 hover:bg-rose-500/5 rounded-xl border border-transparent hover:border-rose-500/10 transition-colors cursor-pointer"
                                title="Delete document"
                              >
                                <Icon.Trash className="w-4.5 h-4.5" />
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 2: CHAT PLAYGROUND RAG SANDBOX */}
        {activeTab === "search" && (
          <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl max-w-4xl mx-auto w-full">
            <h2 className="text-sm font-bold uppercase tracking-wider mb-6 flex items-center space-x-2.5 text-slate-200">
              <Icon.Search className="w-5 h-5 text-indigo-400" />
              <span>RAG Chat Sandbox</span>
            </h2>

            {/* Prompt input bar */}
            <form onSubmit={handleSearch} className="flex flex-col sm:flex-row gap-3 mb-6 bg-slate-950 p-2 border border-slate-850 rounded-2xl">
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Ask slokas, details, or questions (e.g. 'slokas on fevers')"
                className="flex-1 bg-transparent focus:outline-none rounded-xl px-3 py-3 text-white text-sm"
                required
              />
              <div className="flex items-center gap-2 shrink-0 px-2 border-t sm:border-t-0 sm:border-l border-slate-900 pt-2 sm:pt-0">
                <select
                  value={searchLimit}
                  onChange={(e) => setSearchLimit(Number(e.target.value))}
                  className="bg-transparent text-slate-400 text-xs focus:outline-none py-2 px-1 rounded cursor-pointer border border-transparent hover:border-slate-850"
                >
                  <option value={3} className="bg-slate-950">3 results</option>
                  <option value={5} className="bg-slate-950">5 results</option>
                  <option value={10} className="bg-slate-950">10 results</option>
                </select>
                <button
                  type="submit"
                  disabled={searching}
                  className="bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 active:scale-[0.98] text-white text-xs font-semibold px-5 py-2.5 rounded-xl transition-all shadow-md shadow-indigo-600/10 cursor-pointer"
                >
                  {searching ? "Searching..." : "Search"}
                </button>
              </div>
            </form>

            {/* Results display */}
            <div className="bg-slate-950/45 border border-slate-900 rounded-2xl p-6 min-h-[360px] max-h-[600px] overflow-y-auto">
              {searching && (
                <div className="flex flex-col items-center justify-center py-28">
                  <div className="relative flex h-8 w-8 mb-4">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-25"></span>
                    <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-indigo-500"></div>
                  </div>
                  <p className="text-xs text-slate-500 tracking-wide">Synthesizing hybrid vector search query...</p>
                </div>
              )}

              {!searching && chatHistory.length === 0 && !searchResults && (
                <div className="flex flex-col items-center justify-center py-28 text-slate-600">
                  <div className="w-12 h-12 bg-slate-900/30 border border-slate-800 rounded-full flex items-center justify-center mb-4 text-slate-500">
                    <Icon.Search className="w-5 h-5" />
                  </div>
                  <p className="text-sm font-semibold mb-0.5">Prompt Sandbox</p>
                  <p className="text-xs text-slate-600">Enter a query to run semantic & keyword lookup over indexed documents.</p>
                </div>
              )}

              {/* Rerender past conversations like a chat */}
              {!searching && chatHistory.length > 0 && (
                <div className="space-y-8">
                  {chatHistory.map((item, idx) => (
                    <div key={idx} className="space-y-4 border-b border-slate-900/80 pb-8 last:border-b-0 last:pb-0 animate-fade-in">
                      {/* Query Bubble */}
                      <div className="flex items-start justify-end">
                        <div className="bg-indigo-600/10 border border-indigo-500/15 p-3.5 rounded-2xl rounded-tr-none text-slate-200 text-xs max-w-lg leading-relaxed shadow shadow-indigo-950/30">
                          <span className="font-semibold text-[10px] text-indigo-400 block mb-1">Query • {item.timestamp}</span>
                          {item.query}
                        </div>
                      </div>

                      {/* Response Bubble */}
                      <div className="flex items-start space-x-3">
                        <div className="w-7 h-7 bg-violet-600/20 text-violet-400 border border-violet-500/20 rounded-lg flex items-center justify-center shrink-0 text-xs font-mono font-bold shadow">
                          N
                        </div>
                        <div className="flex-1 space-y-4">
                          <span className="font-bold text-[10px] text-slate-500 block">Node Response</span>
                          
                          {/* Synthesized Practitioner Assessment */}
                          {item.synthesis && (
                            <div className="bg-slate-900/40 border border-slate-800/60 rounded-2xl p-5 shadow-inner max-w-3xl leading-relaxed text-slate-350">
                              <span className="font-semibold text-[10px] text-indigo-400 uppercase tracking-wider block mb-3 flex items-center gap-1.5">
                                <span>🩺</span> Practitioner Assessment Synthesis
                              </span>
                              <div className="text-slate-200 text-xs leading-relaxed prose prose-invert prose-sm max-w-none prose-p:text-slate-350 prose-a:text-indigo-400 prose-code:text-indigo-300 prose-code:bg-slate-950 prose-code:px-1 prose-code:rounded prose-pre:bg-slate-950 prose-pre:border prose-pre:border-slate-800">
                                <ReactMarkdown
                                  components={{
                                    h1: ({node, ...props}) => <h2 className="text-sm font-extrabold text-white mt-5 mb-2.5 border-b border-slate-800/60 pb-1.5" {...props} />,
                                    h2: ({node, ...props}) => <h3 className="text-xs font-bold text-indigo-400 mt-4 mb-2" {...props} />,
                                    h3: ({node, ...props}) => <h4 className="text-[11px] font-semibold text-slate-200 mt-3.5 mb-1.5" {...props} />,
                                    p: ({node, ...props}) => <p className="text-slate-300 text-xs mb-2 leading-relaxed" {...props} />,
                                    ul: ({node, ...props}) => <ul className="list-disc list-inside space-y-1 pl-4 mb-3 text-slate-350 text-xs" {...props} />,
                                    ol: ({node, ...props}) => <ol className="list-decimal list-inside space-y-1 pl-4 mb-3 text-slate-355 text-xs" {...props} />,
                                    li: ({node, ...props}) => <li className="text-xs" {...props} />,
                                    code: ({node, inline, ...props}: any) => inline ? <code className="bg-slate-950 border border-slate-900 px-1.5 py-0.5 rounded text-[10px] font-mono text-indigo-300" {...props} /> : <pre className="bg-slate-950 border border-slate-900 p-2 rounded text-xs font-mono text-indigo-300 overflow-x-auto"><code {...props} /></pre>,
                                    blockquote: ({node, ...props}) => <blockquote className="border-l-4 border-indigo-500/50 bg-slate-950/40 pl-4 py-1.5 pr-2 my-2.5 rounded-r-xl italic text-slate-400 text-xs" {...props} />,
                                    strong: ({node, ...props}) => <strong className="font-bold text-white" {...props} />,
                                    em: ({node, ...props}) => <em className="italic text-slate-300" {...props} />,
                                  }}
                                >
                                  {item.synthesis}
                                </ReactMarkdown>
                              </div>
                            </div>
                          )}

                          {/* Reference Slokas List */}
                          <div className="space-y-2 max-w-3xl">
                            <span className="font-bold text-[9px] text-slate-500 uppercase tracking-wider block pl-1">
                              📚 Reference Sanskrit Slokas & Context
                            </span>
                            {parseSearchResults(item.results)}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* TAB 5: TOKEN METRICS DASHBOARD */}
        {activeTab === "metrics" && (
          <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl max-w-4xl mx-auto w-full space-y-8">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <h2 className="text-sm font-bold uppercase tracking-wider flex items-center space-x-2.5 text-slate-200">
                <Icon.Chart className="w-5 h-5 text-indigo-400" />
                <span>Token Metrics Dashboard</span>
              </h2>
              <button
                onClick={fetchData}
                className="text-slate-450 hover:text-white border border-slate-800 hover:border-slate-700 bg-slate-950 px-3.5 py-1.5 rounded-xl text-xs flex items-center gap-1.5 transition-all cursor-pointer"
                title="Refresh metrics"
              >
                <Icon.ArrowPath className="w-3.5 h-3.5" />
                Refresh
              </button>
            </div>

            {/* Token Split Meter Card */}
            <div className="bg-slate-950/45 border border-slate-900 rounded-2xl p-6">
              <h3 className="text-xs font-semibold text-slate-450 mb-4 uppercase tracking-wider pl-1">
                Relative Token Consumption Split
              </h3>
              
              {(() => {
                const ingestTotal = metrics.ingestion.total || 0;
                const sandboxTotal = metrics.sandbox.total || 0;
                const grandTotal = ingestTotal + sandboxTotal;
                const ingestPct = grandTotal > 0 ? Math.round((ingestTotal / grandTotal) * 100) : 0;
                const sandboxPct = grandTotal > 0 ? Math.round((sandboxTotal / grandTotal) * 100) : 0;
                
                return (
                  <div className="space-y-4">
                    {/* Visual Progress Bar */}
                    <div className="h-4 w-full bg-slate-900 rounded-full overflow-hidden border border-slate-800 flex">
                      {grandTotal === 0 ? (
                        <div className="w-full h-full bg-slate-800 flex items-center justify-center">
                          <span className="text-[9px] text-slate-500 font-semibold uppercase tracking-wider">No tokens recorded yet</span>
                        </div>
                      ) : (
                        <>
                          <div 
                            className="h-full bg-gradient-to-r from-indigo-600 to-indigo-500 transition-all duration-500"
                            style={{ width: `${ingestPct}%` }}
                            title={`Ingestion: ${ingestPct}%`}
                          />
                          <div 
                            className="h-full bg-gradient-to-r from-violet-600 to-purple-500 transition-all duration-500"
                            style={{ width: `${sandboxPct}%` }}
                            title={`Sandbox: ${sandboxPct}%`}
                          />
                        </>
                      )}
                    </div>
                    
                    {/* Progress Bar Labels */}
                    {grandTotal > 0 && (
                      <div className="flex justify-between items-center text-xs font-semibold px-1">
                        <div className="flex items-center space-x-2">
                          <span className="w-3 h-3 bg-indigo-500 rounded-full" />
                          <span className="text-slate-400">Ingestion: {ingestPct}%</span>
                        </div>
                        <div className="flex items-center space-x-2">
                          <span className="w-3 h-3 bg-violet-500 rounded-full" />
                          <span className="text-slate-400">RAG Sandbox: {sandboxPct}%</span>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>

            {/* Ingestion vs Sandbox detailed metrics cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              
              {/* Ingestion Metrics Card */}
              <div className="bg-slate-950/20 border border-slate-900 hover:border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between transition-all">
                <div>
                  <div className="flex items-center space-x-3 mb-4">
                    <div className="w-9 h-9 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-xl flex items-center justify-center shrink-0">
                      <Icon.Library className="w-5 h-5" />
                    </div>
                    <div>
                      <h3 className="font-semibold text-slate-200 text-sm">Ingestion Tokens</h3>
                      <p className="text-[10px] text-slate-500 mt-0.5">Used during parsing & metadata enrichment</p>
                    </div>
                  </div>

                  <div className="space-y-3 pt-2">
                    <div className="flex justify-between items-center py-2 border-b border-slate-900/50">
                      <span className="text-xs text-slate-400">Prompt Tokens</span>
                      <span className="text-xs font-mono font-bold text-slate-300">{metrics.ingestion.prompt.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between items-center py-2 border-b border-slate-900/50">
                      <span className="text-xs text-slate-400">Completion Tokens</span>
                      <span className="text-xs font-mono font-bold text-slate-300">{metrics.ingestion.completion.toLocaleString()}</span>
                    </div>
                  </div>
                </div>

                <div className="flex justify-between items-center pt-5 mt-4 border-t border-slate-900">
                  <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Total Ingested</span>
                  <span className="text-lg font-mono font-bold text-indigo-400">{metrics.ingestion.total.toLocaleString()}</span>
                </div>
              </div>

              {/* Sandbox Metrics Card */}
              <div className="bg-slate-950/20 border border-slate-900 hover:border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between transition-all">
                <div>
                  <div className="flex items-center space-x-3 mb-4">
                    <div className="w-9 h-9 bg-violet-500/10 border border-violet-500/20 text-violet-400 rounded-xl flex items-center justify-center shrink-0">
                      <Icon.Search className="w-5 h-5" />
                    </div>
                    <div>
                      <h3 className="font-semibold text-slate-200 text-sm">RAG Sandbox Tokens</h3>
                      <p className="text-[10px] text-slate-500 mt-0.5">Used for testing query synthesis</p>
                    </div>
                  </div>

                  <div className="space-y-3 pt-2">
                    <div className="flex justify-between items-center py-2 border-b border-slate-900/50">
                      <span className="text-xs text-slate-400">Prompt Tokens</span>
                      <span className="text-xs font-mono font-bold text-slate-300">{metrics.sandbox.prompt.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between items-center py-2 border-b border-slate-900/50">
                      <span className="text-xs text-slate-400">Completion Tokens</span>
                      <span className="text-xs font-mono font-bold text-slate-300">{metrics.sandbox.completion.toLocaleString()}</span>
                    </div>
                  </div>
                </div>

                <div className="flex justify-between items-center pt-5 mt-4 border-t border-slate-900">
                  <span className="text-xs font-bold uppercase tracking-wider text-slate-500">Total Sandbox</span>
                  <span className="text-lg font-mono font-bold text-violet-400">{metrics.sandbox.total.toLocaleString()}</span>
                </div>
              </div>

            </div>
          </div>
        )}

        {/* TAB 3: AGENT KEY MANAGER */}
        {activeTab === "agent-keys" && (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
            
            {/* Create Token form card */}
            <div className="space-y-8 lg:col-span-1">
              <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl">
                <h2 className="text-sm font-bold uppercase tracking-wider mb-4 flex items-center space-x-2.5 text-slate-200">
                  <Icon.Key className="w-5 h-5 text-indigo-400" />
                  <span>Generate Agent Key</span>
                </h2>

                <form onSubmit={handleGenerateToken} className="space-y-4">
                  <div>
                    <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2">
                      Key Description
                    </label>
                    <input
                      type="text"
                      value={newTokenDesc}
                      onChange={(e) => setNewTokenDesc(e.target.value)}
                      placeholder="e.g. Claude Desktop IDE"
                      className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-3 text-white placeholder-slate-800 focus:outline-none focus:border-indigo-500 transition-colors text-xs"
                      required
                    />
                  </div>
                  <button
                    type="submit"
                    className="w-full bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 active:scale-[0.98] text-white font-semibold py-3 rounded-xl transition-all shadow-lg shadow-indigo-600/10 cursor-pointer text-xs"
                  >
                    Generate Agent Key
                  </button>
                </form>

                {generatedKey && (
                  <div className="mt-6 p-4 bg-emerald-500/5 border border-emerald-500/15 rounded-2xl animate-fade-in">
                    <span className="block text-[9px] uppercase font-bold text-emerald-400 tracking-wider mb-1">
                      Key Generated
                    </span>
                    <p className="text-slate-500 text-[10px] mb-3 leading-relaxed">
                      Copy this key now. It is encrypted in database and cannot be retrieved again.
                    </p>
                    <div className="bg-slate-950 p-2.5 rounded-xl border border-slate-900 flex items-center justify-between gap-2">
                      <code className="text-xs font-mono text-slate-200 select-all break-all pr-2">
                        {generatedKey}
                      </code>
                      <button 
                        onClick={() => copyToClipboard(generatedKey)}
                        className="p-2 hover:bg-slate-900 rounded-lg text-slate-500 hover:text-white shrink-0 border border-transparent hover:border-slate-800 transition-all"
                        title="Copy to clipboard"
                      >
                        {copiedKey ? <Icon.CopySuccess className="w-4 h-4 text-emerald-400" /> : <Icon.Copy className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* Claude config instructions card */}
              <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 text-xs text-slate-400 space-y-4 shadow-xl">
                <h3 className="font-bold text-slate-200 flex items-center space-x-2 text-xs uppercase tracking-wider">
                  <span>🔌</span> <span>MCP Integration</span>
                </h3>
                <p className="leading-relaxed">
                  Add this block to your Claude Desktop config JSON to enable semantic search over your catalog:
                </p>
                <div className="space-y-2">
                  <pre className="bg-slate-950 p-3 border border-slate-900 rounded-xl text-[10px] overflow-x-auto text-indigo-400/80 leading-normal font-mono relative">
{`"mcpServers": {
  "openpdfspecs": {
    "command": "python3",
    "args": ["/absolute/path/to/src/mcp_server.py"],
    "env": {
      "NVIDIA_API_KEY": "..."
    }
  }
}`}
                  </pre>
                </div>
              </div>
            </div>

            {/* List active keys card */}
            <div className="lg:col-span-2">
              <div className="bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-6 shadow-xl h-full flex flex-col min-h-[500px]">
                <h2 className="text-sm font-bold uppercase tracking-wider mb-6 flex items-center space-x-2.5 text-slate-200">
                  <Icon.Key className="w-5 h-5 text-indigo-400" />
                  <span>Authorized Agent Keys</span>
                </h2>

                <div className="flex-1 overflow-y-auto pr-1">
                  {agentTokens.length === 0 ? (
                    <div className="flex flex-col items-center justify-center py-24 text-slate-600 text-center">
                      <div className="w-16 h-16 bg-slate-900/30 border border-slate-800 rounded-full flex items-center justify-center mb-4 text-slate-500">
                        <Icon.Key className="w-7 h-7" />
                      </div>
                      <p className="text-slate-400 font-semibold mb-1 text-sm">No agent keys authorized</p>
                      <p className="text-slate-600 text-xs">Generate a token on the left to authorize external agents.</p>
                    </div>
                  ) : (
                    <div className="space-y-4">
                      {agentTokens.map((tok) => (
                        <div key={tok.token} className="p-4.5 bg-slate-950/35 border border-slate-900 hover:border-slate-800/80 hover:bg-slate-950/50 rounded-2xl flex items-center justify-between gap-4 transition-all duration-200">
                          <div className="min-w-0">
                            <h4 className="font-semibold text-slate-200 text-sm truncate">{tok.description}</h4>
                            <p className="text-[10px] text-slate-500 mt-1.5 font-mono flex flex-wrap gap-x-2">
                              <span>Token:</span>
                              <code className="text-indigo-400/90 font-bold tracking-wider">{tok.token.slice(0, 20)}...</code>
                              <span className="text-slate-600">•</span>
                              <span>Created: {tok.created_at}</span>
                            </p>
                          </div>
                          <button
                            onClick={() => handleRevokeToken(tok.token)}
                            className="text-rose-400/90 hover:text-rose-300 font-semibold p-2 hover:bg-rose-500/5 rounded-xl border border-rose-500/10 hover:border-rose-500/20 transition-all text-xs cursor-pointer shrink-0"
                            title="Revoke Token"
                          >
                            Revoke
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 4: SYSTEM SETTINGS PANEL */}
        {activeTab === "settings" && (
          <div className="max-w-2xl mx-auto bg-slate-900/35 backdrop-blur-xl border border-slate-900/80 rounded-3xl p-8 shadow-xl">
            <h2 className="text-sm font-bold uppercase tracking-wider mb-6 flex items-center space-x-2.5 text-slate-200">
              <Icon.Settings className="w-5 h-5 text-indigo-400" />
              <span>System Configuration</span>
            </h2>

            <form onSubmit={handleSaveSettings} className="space-y-6">
              
              {/* Provider selector */}
              <div>
                <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2">
                  Active Model Provider
                </label>
                <select
                  value={settingsForm.provider}
                  onChange={(e) => {
                    const nextProv = e.target.value;
                    setSettingsForm(prev => {
                      if (nextProv === "nvidia") {
                        return {
                          ...prev,
                          provider: nextProv,
                          llm_model: prev.llm_model === "gemini-1.5-flash" ? "meta/llama-4-maverick-17b-128e-instruct" : prev.llm_model,
                          embedding_model: prev.embedding_model === "text-embedding-004" ? "nvidia/nv-embedqa-e5-v5" : prev.embedding_model,
                          chunk_size: prev.chunk_size === 2500 ? 1200 : prev.chunk_size,
                          chunk_overlap: prev.chunk_overlap === 250 ? 120 : prev.chunk_overlap
                        };
                      } else if (nextProv === "gemini") {
                        return {
                          ...prev,
                          provider: nextProv,
                          llm_model: prev.llm_model === "meta/llama-4-maverick-17b-128e-instruct" ? "gemini-1.5-flash" : prev.llm_model,
                          embedding_model: prev.embedding_model === "nvidia/nv-embedqa-e5-v5" ? "text-embedding-004" : prev.embedding_model,
                          chunk_size: prev.chunk_size === 1200 ? 2500 : prev.chunk_size,
                          chunk_overlap: prev.chunk_overlap === 120 ? 250 : prev.chunk_overlap
                        };
                      }
                      return { ...prev, provider: nextProv };
                    });
                  }}
                  className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-3 text-white text-xs focus:outline-none focus:border-indigo-500 transition-colors cursor-pointer"
                >
                  <option value="gemini">Google Gemini API (Free Tier)</option>
                  <option value="nvidia">Nvidia Developer NIM API (Free Tier)</option>
                </select>
              </div>

              {/* Gemini credentials */}
              {settingsForm.provider === "gemini" && (
                <div className="space-y-4 p-5 bg-slate-950/45 border border-slate-900 rounded-2xl">
                  <h4 className="font-bold text-[10px] uppercase text-indigo-400 tracking-wider">Gemini Model Settings</h4>
                  <div>
                    <label className="block text-[10px] font-semibold text-slate-500 mb-1.5">
                      Gemini API Key (Saved Encrypted in DB)
                    </label>
                    <input
                      type="password"
                      value={settingsForm.gemini_api_key}
                      onChange={(e) => setSettingsForm({ ...settingsForm, gemini_api_key: e.target.value })}
                      placeholder="••••••••••••••••••••••••"
                      className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-2.5 text-white placeholder-slate-700 focus:outline-none focus:border-indigo-500 transition-colors text-xs font-mono"
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-[10px] font-semibold text-slate-500 mb-1.5">LLM Model</label>
                      <input
                        type="text"
                        value={settingsForm.llm_model}
                        onChange={(e) => setSettingsForm({ ...settingsForm, llm_model: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-indigo-500 transition-colors text-xs font-mono"
                      />
                    </div>
                    <div>
                      <label className="block text-[10px] font-semibold text-slate-500 mb-1.5">Embedding Model</label>
                      <input
                        type="text"
                        value={settingsForm.embedding_model}
                        onChange={(e) => setSettingsForm({ ...settingsForm, embedding_model: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-indigo-500 transition-colors text-xs font-mono"
                      />
                    </div>
                  </div>
                </div>
              )}

              {/* Nvidia credentials */}
              {settingsForm.provider === "nvidia" && (
                <div className="space-y-4 p-5 bg-slate-950/45 border border-slate-900 rounded-2xl">
                  <h4 className="font-bold text-[10px] uppercase text-purple-400 tracking-wider">Nvidia NIM Model Settings</h4>
                  <div>
                    <label className="block text-[10px] font-semibold text-slate-500 mb-1.5">
                      Nvidia API Key (Saved Encrypted in DB)
                    </label>
                    <input
                      type="password"
                      value={settingsForm.nvidia_api_key}
                      onChange={(e) => setSettingsForm({ ...settingsForm, nvidia_api_key: e.target.value })}
                      placeholder="••••••••••••••••••••••••"
                      className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-2.5 text-white placeholder-slate-700 focus:outline-none focus:border-indigo-500 transition-colors text-xs font-mono"
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-[10px] font-semibold text-slate-500 mb-1.5">LLM Model</label>
                      <input
                        type="text"
                        value={settingsForm.llm_model}
                        onChange={(e) => setSettingsForm({ ...settingsForm, llm_model: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-indigo-500 transition-colors text-xs font-mono"
                      />
                    </div>
                    <div>
                      <label className="block text-[10px] font-semibold text-slate-500 mb-1.5">Embedding Model</label>
                      <input
                        type="text"
                        value={settingsForm.embedding_model}
                        onChange={(e) => setSettingsForm({ ...settingsForm, embedding_model: e.target.value })}
                        className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-indigo-500 transition-colors text-xs font-mono"
                      />
                    </div>
                  </div>
                </div>
              )}

              {/* Chunking variables */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2">
                    Chunk Size (Characters)
                  </label>
                  <input
                    type="number"
                    value={settingsForm.chunk_size}
                    onChange={(e) => setSettingsForm({ ...settingsForm, chunk_size: Number(e.target.value) })}
                    className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-indigo-500 transition-colors text-xs"
                    required
                  />
                  {settingsForm.provider === "nvidia" && (
                    <span className="text-[10px] text-slate-500 block mt-1.5 leading-normal">
                      ℹ️ NVIDIA embeddings are limited to 512 tokens. Characters will be safely truncated at 1000 characters.
                    </span>
                  )}
                </div>
                <div>
                  <label className="block text-[10px] font-bold text-slate-500 uppercase tracking-wider mb-2">
                    Chunk Overlap (Characters)
                  </label>
                  <input
                    type="number"
                    value={settingsForm.chunk_overlap}
                    onChange={(e) => setSettingsForm({ ...settingsForm, chunk_overlap: Number(e.target.value) })}
                    className="w-full bg-slate-950 border border-slate-850 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-indigo-500 transition-colors text-xs"
                    required
                  />
                </div>
              </div>

              <button
                type="submit"
                className="w-full bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 active:scale-[0.98] text-white font-medium py-3 rounded-xl transition-all shadow-lg shadow-indigo-600/10 cursor-pointer text-xs"
              >
                Save System Settings
              </button>
            </form>
          </div>
        )}
      </main>
    </div>
  );
}
