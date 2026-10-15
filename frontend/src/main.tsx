import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import ChatPage from "./pages/ChatPage";
import AdminPage from "./pages/AdminPage";
import "./styles.css";

function Shell() {
  return (
    <div className="app">
      <nav className="topnav" aria-label="Main">
        <span className="brand">
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="currentColor" /><path d="M9 22V13l7-5 7 5v9h-5v-5h-4v5z" fill="#fff" /></svg>
          Aria
        </span>
        <NavLink to="/" end>Chat</NavLink>
        <NavLink to="/admin">Business console</NavLink>
      </nav>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/admin" element={<AdminPage />} />
      </Routes>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  </React.StrictMode>,
);
