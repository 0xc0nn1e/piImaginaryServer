import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  ApiError,
  getSession,
  getSetupStatus,
  login as requestLogin,
  logout as requestLogout,
  readCsrfCookie,
  setupAccount,
} from "../api";
import { getStoredLocale, translate } from "../i18n";
import type { User } from "../types";

interface AuthState {
  loading: boolean;
  setupRequired: boolean;
  setupEnabled: boolean;
  user: User | null;
  csrfToken: string | null;
  bootstrapError: string | null;
  login: (username: string, password: string) => Promise<void>;
  setup: (setupToken: string, username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  invalidate: () => void;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [loading, setLoading] = useState(true);
  const [setupRequired, setSetupRequired] = useState(false);
  const [setupEnabled, setSetupEnabled] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setBootstrapError(null);
    try {
      const setupStatus = await getSetupStatus();
      setSetupRequired(setupStatus.setup_required);
      setSetupEnabled(setupStatus.setup_enabled);
      if (setupStatus.setup_required) {
        setUser(null);
        setCsrfToken(null);
        return;
      }
      try {
        const session = await getSession();
        setUser(session.user);
        setCsrfToken(readCsrfCookie());
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          setUser(null);
          setCsrfToken(null);
          return;
        }
        throw error;
      }
    } catch {
      setBootstrapError(translate(getStoredLocale(), "common.serverUnavailable"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    await requestLogin(username, password);
    const session = await getSession();
    setUser(session.user);
    setCsrfToken(readCsrfCookie());
    setSetupRequired(false);
  }, []);

  const setup = useCallback(async (setupToken: string, username: string, password: string) => {
    await setupAccount(setupToken, username, password);
    setUser(null);
    setCsrfToken(null);
    setSetupRequired(false);
  }, []);

  const logout = useCallback(async () => {
    const currentCsrfToken = readCsrfCookie() ?? csrfToken;
    if (!currentCsrfToken) throw new Error("CSRF cookie is unavailable");
    await requestLogout(currentCsrfToken);
    setUser(null);
    setCsrfToken(null);
  }, [csrfToken]);

  const invalidate = useCallback(() => {
    setUser(null);
    setCsrfToken(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      loading,
      setupRequired,
      setupEnabled,
      user,
      csrfToken,
      bootstrapError,
      login,
      setup,
      logout,
      invalidate,
      refresh,
    }),
    [
      loading,
      setupRequired,
      setupEnabled,
      user,
      csrfToken,
      bootstrapError,
      login,
      setup,
      logout,
      invalidate,
      refresh,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
