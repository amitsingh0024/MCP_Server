"use client";

import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const publishableKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

export const isSupabaseConfigured = Boolean(url && publishableKey);

if (!isSupabaseConfigured) {
  // Surfaced clearly in the console during local dev if .env.local is missing, and in
  // the build output for any deployment whose environment lacks these.
  console.warn(
    "Missing NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY — auth will not work."
  );
}

// Placeholders, not empty strings. This module is "use client", but client components
// are still evaluated on the server to prerender HTML, so createClient() runs during
// `next build` — and it rejects an empty url outright with "supabaseUrl is required.",
// which failed the export of "/" and exited the build. Every Vercel preview deployment
// died that way, because these variables were scoped to Production only, while
// production and local builds passed and hid it.
//
// A missing public config value should degrade the feature, not prevent the app from
// being built at all. `.invalid` is reserved by RFC 2606 and can never resolve, so a
// request that reaches the network fails at an obviously fake host rather than
// somewhere real. The cost is that signing in on such a deployment fails when clicked
// instead of at build time; isSupabaseConfigured is exported so a caller can say so
// before that point.
//
// NEXT_PUBLIC_ values are inlined at build time, so this cannot be repaired at runtime:
// an environment missing them produces a bundle that is permanently unauthenticated.
const PLACEHOLDER_URL = "https://supabase-is-not-configured.invalid";
const PLACEHOLDER_KEY = "supabase-is-not-configured";

// Browser client. The publishable (anon) key is public by design; auth persists in
// localStorage and the OAuth redirect is auto-detected on load.
export const supabase = createClient(
  url || PLACEHOLDER_URL,
  publishableKey || PLACEHOLDER_KEY,
  {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  }
);
