import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  Navigate,
  Route,
  BrowserRouter as Router,
  Routes,
} from 'react-router-dom';
import { DevDataPanel } from '@/components/dev/DevDataPanel';
import { AppShell } from '@/components/layout/AppShell';
import { AuthProvider } from '@/hooks/useAuth';
import { ThemeProvider } from '@/hooks/useTheme';
import { ChangePasswordPage } from '@/pages/ChangePasswordPage';
import { JobPage } from '@/pages/JobPage';
import { LoginPage } from '@/pages/LoginPage';
import { NewMeetingPage } from '@/pages/NewMeetingPage';
import {
  DiarizationSettingsPage,
  IntegrationsSettingsPage,
  LlmSettingsPage,
  MatchingSettingsPage,
  PromptSettingsPage,
  SettingsPage,
  UsersSettingsPage,
} from '@/pages/SettingsPage';
import { ThreadDetailPage } from '@/pages/ThreadDetailPage';
import { ThreadsPage } from '@/pages/ThreadsPage';
import { TranscriptPage } from '@/pages/TranscriptPage';
import { RequireAdmin, RequireAuth, RequirePasswordChanged } from '@/routes/guards';
import { ApiError } from '@/types/api';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      // Retrying a 401/403/404 just delays the inevitable.
      retry: (count, error) =>
        !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

function NotFound() {
  return (
    <div className="py-20 text-center">
      <h1 className="font-display text-3xl font-semibold">Not found</h1>
      <p className="mt-2 text-fg-subtle">That page does not exist.</p>
    </div>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <Router>
          <AuthProvider>
            <Routes>
              <Route path="/login" element={<LoginPage />} />

              <Route element={<RequireAuth />}>
                {/* Outside RequirePasswordChanged, or it redirect-loops. */}
                <Route path="/change-password" element={<ChangePasswordPage />} />

                <Route element={<RequirePasswordChanged />}>
                  <Route element={<AppShell />}>
                    <Route index element={<ThreadsPage />} />
                    <Route path="/threads/:threadId" element={<ThreadDetailPage />} />
                    <Route path="/meetings/new" element={<NewMeetingPage />} />
                    <Route path="/meetings/:meetingId" element={<TranscriptPage />} />
                    <Route path="/jobs/:jobId" element={<JobPage />} />

                    <Route path="/settings" element={<SettingsPage />}>
                      <Route index element={<Navigate to="/settings/llm" replace />} />
                      <Route path="llm" element={<LlmSettingsPage />} />
                      <Route path="diarization" element={<DiarizationSettingsPage />} />
                      <Route path="integrations" element={<IntegrationsSettingsPage />} />
                      <Route path="matching" element={<MatchingSettingsPage />} />
                      {/* Old path kept as a redirect: bookmarks, and error
                          deep-links from a cached bundle, both still land. */}
                      <Route
                        path="mcp"
                        element={<Navigate to="/settings/integrations" replace />}
                      />
                      <Route path="prompt" element={<PromptSettingsPage />} />
                      {/* Renders its own "no account yet" state when the server
                          has dev data off; the tab is hidden there anyway. */}
                      <Route path="development" element={<DevDataPanel />} />
                      <Route element={<RequireAdmin />}>
                        <Route path="users" element={<UsersSettingsPage />} />
                      </Route>
                    </Route>

                    <Route path="*" element={<NotFound />} />
                  </Route>
                </Route>
              </Route>
            </Routes>
          </AuthProvider>
        </Router>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
