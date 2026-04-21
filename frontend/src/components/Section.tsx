import { useState, type ReactNode } from "react";
import { useTheme } from "../theme";

export function Section({
  title,
  subtitle,
  children,
  right,
  count,
  defaultOpen = true,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  right?: ReactNode;
  count?: number;
  defaultOpen?: boolean;
}) {
  const theme = useTheme();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section
      style={{
        background: theme.bg2,
        border: `1px solid ${theme.borderL}`,
        borderRadius: 12,
        padding: 20,
        marginBottom: 16,
      }}
    >
      <header
        onClick={() => setOpen(!open)}
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: open ? 16 : 0,
          cursor: "pointer",
          userSelect: "none",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: theme.text }}>
            {title}
          </h2>
          {count !== undefined && (
            <span style={{ fontSize: 12, color: theme.textDim, fontWeight: 500 }}>
              ({count})
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {right && (
            // Stop click events from bubbling up to the header's
            // collapse-toggle handler. Without this, interacting
            // with anything in the `right` slot (the AuthorsPage
            // search box is the caller that surfaced this) triggers
            // a collapse on the first click and forces a second
            // click to re-expand and re-focus.
            <div
              onClick={(e) => e.stopPropagation()}
              style={{ cursor: "auto" }}
            >
              {right}
            </div>
          )}
          {subtitle && (
            <span style={{ fontSize: 13, color: theme.textDim }}>
              {subtitle}
            </span>
          )}
          <span style={{ fontSize: 12, color: theme.textDim }}>{open ? "▾" : "▸"}</span>
        </div>
      </header>
      {open && children}
    </section>
  );
}
