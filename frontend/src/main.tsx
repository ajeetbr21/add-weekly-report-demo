import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Shell from "./layouts/Shell";
import Dashboard from "./pages/Dashboard";
import Tasks from "./pages/Tasks";
import TaskDetailPage from "./pages/TaskDetail";
import Agents from "./pages/Agents";
import MemoryPage from "./pages/Memory";
import Learning from "./pages/Learning";
import Approvals from "./pages/Approvals";
import Reports from "./pages/Reports";
import Schedules from "./pages/Schedules";
import Integrations from "./pages/Integrations";
import Settings from "./pages/Settings";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:id" element={<TaskDetailPage />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/learning" element={<Learning />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/reports/:id" element={<Reports />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
);
