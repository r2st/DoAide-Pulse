import { Navigate, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import { useAuth } from "./hooks/useAuth";
import Analytics from "./pages/Analytics";
import Calendar from "./pages/Calendar";
import ContentEditor from "./pages/ContentEditor";
import ContentList from "./pages/ContentList";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import PreviewPage from "./pages/PreviewPage";
import Projects from "./pages/Projects";
import Publish from "./pages/Publish";
import Settings from "./pages/Settings";
import Templates from "./pages/Templates";
import Triggers from "./pages/Triggers";

function Loading() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <span className="eyebrow animate-pulse">Loading</span>
    </div>
  );
}

function Protected({ children }) {
  const { user, loading } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <Navigate to="/login" replace />;
  return <Shell>{children}</Shell>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      {/* Unauthenticated on purpose — see app.services.preview_links. */}
      <Route path="/preview/:token" element={<PreviewPage />} />

      <Route
        path="/"
        element={
          <Protected>
            <Dashboard />
          </Protected>
        }
      />
      <Route
        path="/projects"
        element={
          <Protected>
            <Projects />
          </Protected>
        }
      />
      <Route
        path="/content"
        element={
          <Protected>
            <ContentList />
          </Protected>
        }
      />
      {/* The editor is entered from a piece, not from the nav. */}
      <Route
        path="/content/:contentId"
        element={
          <Protected>
            <ContentEditor />
          </Protected>
        }
      />
      <Route
        path="/triggers"
        element={
          <Protected>
            <Triggers />
          </Protected>
        }
      />
      <Route
        path="/templates"
        element={
          <Protected>
            <Templates />
          </Protected>
        }
      />
      <Route
        path="/calendar"
        element={
          <Protected>
            <Calendar />
          </Protected>
        }
      />
      <Route
        path="/publish"
        element={
          <Protected>
            <Publish />
          </Protected>
        }
      />
      <Route
        path="/analytics"
        element={
          <Protected>
            <Analytics />
          </Protected>
        }
      />
      <Route
        path="/settings"
        element={
          <Protected>
            <Settings />
          </Protected>
        }
      />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
