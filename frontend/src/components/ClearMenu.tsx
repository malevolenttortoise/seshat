// Single-button dropdown for the "Clear data" action bar across
// Authors / Author-detail / Books multi-select. Consolidates what
// used to be 3-5 inline buttons into one "Clear ▾" trigger — the
// action bars were getting crowded once we added the ebook/audiobook
// source split and the "Clear Both" variant.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTheme } from "../theme";
import { Btn } from "./Btn";

export interface ClearOption {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  // Visual hint for cross-library variants. "ebook"/"audio" tints the
  // row so the scope is obvious at a glance.
  variant?: "default" | "ebook" | "audio" | "danger";
  // Optional separator ABOVE this item.
  divider?: boolean;
  // Trailing annotation (e.g. "active library only").
  hint?: string;
}

interface ClearMenuProps {
  options: ClearOption[];
  disabled?: boolean;
  // Default button label. "Clear" for multi-select bars, "Clear Data"
  // for more prominent placements. The caller picks.
  label?: ReactNode;
  align?: "left" | "right";
}

export function ClearMenu({
  options, disabled, label = "Clear", align = "left",
}: ClearMenuProps) {
  const t = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const colorFor = (v?: ClearOption["variant"]) => {
    switch (v) {
      case "ebook": return t.ylwt;
      case "audio": return t.purt;
      case "danger": return t.redt;
      default: return t.text;
    }
  };

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <Btn
        size="sm"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        style={{
          background: t.ylw + "22",
          color: t.ylwt,
          border: `1px solid ${t.ylw}44`,
        }}
      >
        {label} ▾
      </Btn>
      {open ? (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 4px)",
            [align === "right" ? "right" : "left"]: 0,
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 8,
            boxShadow: "0 6px 16px rgba(0,0,0,0.35)",
            minWidth: 240,
            zIndex: 50,
            padding: 4,
          }}
        >
          {options.map((opt, i) => (
            <div key={i}>
              {opt.divider ? (
                <div
                  style={{
                    height: 1,
                    background: t.border,
                    margin: "4px 2px",
                  }}
                />
              ) : null}
              <button
                onClick={() => {
                  if (opt.disabled) return;
                  setOpen(false);
                  opt.onClick();
                }}
                disabled={opt.disabled}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 10,
                  width: "100%",
                  padding: "8px 12px",
                  textAlign: "left",
                  background: "transparent",
                  border: "none",
                  color: opt.disabled ? t.tg : colorFor(opt.variant),
                  fontSize: 13,
                  fontWeight: 500,
                  cursor: opt.disabled ? "not-allowed" : "pointer",
                  borderRadius: 4,
                  opacity: opt.disabled ? 0.5 : 1,
                }}
                onMouseEnter={(e) => {
                  if (!opt.disabled) e.currentTarget.style.background = t.bg3;
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                }}
              >
                <span>{opt.label}</span>
                {opt.hint ? (
                  <span style={{ fontSize: 11, color: t.tg, fontWeight: 400 }}>
                    {opt.hint}
                  </span>
                ) : null}
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
