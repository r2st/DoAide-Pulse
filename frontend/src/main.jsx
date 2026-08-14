import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import { ToastProvider } from "./components/ui/Toast";
import { AuthProvider } from "./hooks/useAuth";
import { ROUTER_FUTURE } from "./lib/routerFuture";
import "./index.css";

// The outermost boundary, above the router and the two providers, catching what
// the per-route boundaries in App.jsx sit below: a throw from the router itself,
// from AuthProvider while it resolves the session, or from the toast layer. It
// deliberately has no reset key — there is no navigation left to recover with at
// this level, so its fallback's "Try again" is the whole offer.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ErrorBoundary title="Herald failed to start" section="root">
      <BrowserRouter future={ROUTER_FUTURE}>
        <ToastProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
);
