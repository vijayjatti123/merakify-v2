import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import "./index.css";
import { createTheme, ThemeProvider } from "@mui/material/styles";

const theme = createTheme({ palette: { mode: "dark", primary: { main: "#E8A33D" }, background: { paper: "#1B1D2B" } } });

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ThemeProvider theme={theme}><App /></ThemeProvider>
  </React.StrictMode>
);
// force rebuild 09/08/2026 08:11:13
// retry 09/08/2026 08:17:43
