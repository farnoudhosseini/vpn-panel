/** Data fetching and polling helpers. */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";
import { errorMessage } from "./format";

interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** True while a background refresh runs over existing data. */
  refreshing: boolean;
}

interface AsyncResult<T> extends AsyncState<T> {
  reload: (options?: { silent?: boolean }) => Promise<void>;
  setData: (value: T | null) => void;
}

/**
 * Run an async loader, with a `silent` reload that keeps the current data on
 * screen so polling never makes the interface flash.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: readonly unknown[],
  options: { pollMs?: number } = {},
): AsyncResult<T> {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: true,
    refreshing: false,
  });
  const mounted = useRef(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (silent = false) => {
    if (!silent) setState((prev) => ({ ...prev, loading: true, error: null }));
    else setState((prev) => ({ ...prev, refreshing: true }));
    try {
      const data = await loaderRef.current();
      if (!mounted.current) return;
      setState({ data, error: null, loading: false, refreshing: false });
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status === 401) {
        setState((prev) => ({ ...prev, loading: false, refreshing: false }));
        return;
      }
      setState((prev) => ({
        ...prev,
        error: errorMessage(error instanceof ApiError ? error.payload : String(error)),
        loading: false,
        refreshing: false,
      }));
    }
  }, []);

  useEffect(() => {
    void run(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    if (!options.pollMs) return;
    const timer = window.setInterval(() => void run(true), options.pollMs);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.pollMs, ...deps]);

  return {
    ...state,
    reload: (opts) => run(Boolean(opts?.silent)),
    setData: (value) => setState((prev) => ({ ...prev, data: value })),
  };
}

/** Re-render on an interval; used for relative timestamps. */
export function useTicker(intervalMs = 30_000): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return tick;
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );
  useEffect(() => {
    const list = window.matchMedia(query);
    const listener = (event: MediaQueryListEvent) => setMatches(event.matches);
    list.addEventListener("change", listener);
    setMatches(list.matches);
    return () => list.removeEventListener("change", listener);
  }, [query]);
  return matches;
}

/** Persist small UI state (selected tab, filters) across reloads. */
export function usePersistentState<T>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored ? (JSON.parse(stored) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* ignore */
    }
  }, [key, value]);
  return [value, setValue];
}

export function useCopy(): (text: string) => Promise<boolean> {
  return useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }, []);
}

export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} — Panah` : "Panah";
  }, [title]);
}
