import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AuthProvider } from "./auth/AuthContext";
import { AppLayout } from "./components/AppLayout";
import { HomeRedirect, LoginGuard, RequireAuth, SetupGuard } from "./components/RouteGuards";
import { LanguageProvider } from "./i18n";
import { LoginPage } from "./pages/LoginPage";
import { RecordingDetailPage } from "./pages/RecordingDetailPage";
import { RecordingsPage } from "./pages/RecordingsPage";
import { SetupPage } from "./pages/SetupPage";

export function App() {
  return (
    <BrowserRouter>
      <LanguageProvider>
        <AuthProvider>
          <Routes>
            <Route path="/" element={<HomeRedirect />} />
            <Route
              path="/setup"
              element={
                <SetupGuard>
                  <SetupPage />
                </SetupGuard>
              }
            />
            <Route
              path="/login"
              element={
                <LoginGuard>
                  <LoginPage />
                </LoginGuard>
              }
            />
            <Route
              element={
                <RequireAuth>
                  <AppLayout />
                </RequireAuth>
              }
            >
              <Route path="/recordings" element={<RecordingsPage />} />
              <Route path="/recordings/:id" element={<RecordingDetailPage />} />
            </Route>
            <Route path="*" element={<HomeRedirect />} />
          </Routes>
        </AuthProvider>
      </LanguageProvider>
    </BrowserRouter>
  );
}
