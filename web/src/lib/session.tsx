/**
 * Session state.
 *
 * On boot the panel tries to renew the session from the HttpOnly refresh
 * cookie, so a page reload does not force another sign-in. Everything else
 * reads the account from this context.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { endpoints, onUnauthorized, setAccessToken } from "./api";
import type { Account } from "./types";

interface SessionValue {
  account: Account | null;
  status: "loading" | "signed-in" | "signed-out";
  signIn: (username: string, password: string, totp?: string) => Promise<Account>;
  signOut: () => Promise<void>;
  reload: () => Promise<void>;
  setAccount: (account: Account) => void;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [status, setStatus] = useState<SessionValue["status"]>("loading");

  const reload = useCallback(async () => {
    try {
      const me = await endpoints.me();
      setAccount(me);
      setStatus("signed-in");
    } catch {
      setAccount(null);
      setStatus("signed-out");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    // The API client tells us when a renewal failed for good.
    return onUnauthorized(() => {
      setAccessToken(null);
      setAccount(null);
      setStatus("signed-out");
    });
  }, []);

  const signIn = useCallback<SessionValue["signIn"]>(async (username, password, totp) => {
    const response = await endpoints.login(username, password, totp);
    setAccessToken(response.access_token);
    const me = await endpoints.me();
    setAccount(me);
    setStatus("signed-in");
    return me;
  }, []);

  const signOut = useCallback(async () => {
    try {
      await endpoints.logout();
    } catch {
      /* the cookie is cleared server-side when possible */
    }
    setAccessToken(null);
    setAccount(null);
    setStatus("signed-out");
  }, []);

  const value = useMemo<SessionValue>(
    () => ({ account, status, signIn, signOut, reload, setAccount }),
    [account, status, signIn, signOut, reload],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionProvider");
  return value;
}
