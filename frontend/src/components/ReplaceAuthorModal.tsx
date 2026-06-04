// Modal shown when the user tries to remove a book's ONLY author.
//
// A book must keep at least one author (the backend 409s with
// `code: "last_author"` otherwise), so instead of just blocking we let
// the user pick a replacement author from the same library via a
// debounced typeahead (`GET /discovery/authors/search?q=&slug=`). The
// parent performs the actual DELETE (passing `replacement_author_id`)
// through `onConfirm` so all the refresh/toast wiring stays in one place.
import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError } from "../api";
import { Btn } from "./Btn";

interface AuthorPick {
  id: number;
  name: string;
}

export interface ReplaceAuthorModalProps {
  bookTitle: string;
  slug?: string | null;
  removingName: string;
  removingAuthorId: number;
  onCancel: () => void;
  /** Parent runs the DELETE with the chosen replacement; may throw. */
  onConfirm: (replacementId: number, replacementName: string) => Promise<void>;
}

export function ReplaceAuthorModal({
  bookTitle,
  slug,
  removingName,
  removingAuthorId,
  onCancel,
  onConfirm,
}: ReplaceAuthorModalProps) {
  const t = useTheme();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<AuthorPick[]>([]);
  const [picked, setPicked] = useState<AuthorPick | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (picked) return; // already chose; stop searching
    const q = query.trim();
    if (!q) {
      setMatches([]);
      return;
    }
    const timer = setTimeout(() => {
      const sq = slug ? `&slug=${encodeURIComponent(slug)}` : "";
      api
        .get<{ authors: AuthorPick[] }>(
          `/discovery/authors/search?q=${encodeURIComponent(q)}${sq}`,
        )
        .then((r) =>
          setMatches(
            r.authors.filter((a) => a.id !== removingAuthorId).slice(0, 10),
          ),
        )
        .catch(() => setMatches([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [query, picked, slug, removingAuthorId]);

  const confirm = async () => {
    if (!picked) return;
    setBusy(true);
    setErr("");
    try {
      await onConfirm(picked.id, picked.name);
      // Parent closes the modal on success.
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <div
      onClick={onCancel}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        zIndex: 300,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        animation: "fadeOverlay 0.2s ease-out",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 10,
          width: 480,
          maxWidth: "92vw",
          padding: 20,
          color: t.text,
          boxShadow: "0 12px 40px rgba(0,0,0,0.4)",
        }}
      >
        <h3 style={{ margin: "0 0 6px", fontSize: 16 }}>Replace sole author</h3>
        <p style={{ margin: "0 0 14px", fontSize: 13, color: t.td }}>
          <strong style={{ color: t.text2 }}>{removingName}</strong> is the only
          author of <em>{bookTitle}</em>. Pick a replacement so the book stays
          attributed.
        </p>

        {picked ? (
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "8px 10px",
              background: t.bg4,
              border: `1px solid ${t.border}`,
              borderRadius: 6,
              marginBottom: 12,
            }}
          >
            <span style={{ fontSize: 14 }}>{picked.name}</span>
            <button
              onClick={() => {
                setPicked(null);
                setQuery("");
              }}
              style={{
                background: "none",
                border: "none",
                color: t.accent,
                cursor: "pointer",
                fontSize: 12,
              }}
            >
              change
            </button>
          </div>
        ) : (
          <div style={{ position: "relative", marginBottom: 12 }}>
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search authors in this library…"
              style={{
                width: "100%",
                boxSizing: "border-box",
                padding: "8px 10px",
                background: t.bg,
                border: `1px solid ${t.border}`,
                borderRadius: 6,
                color: t.text,
                fontSize: 14,
              }}
            />
            {matches.length > 0 && (
              <div
                style={{
                  position: "absolute",
                  top: "100%",
                  left: 0,
                  right: 0,
                  zIndex: 1,
                  marginTop: 4,
                  background: t.bg2,
                  border: `1px solid ${t.border}`,
                  borderRadius: 6,
                  maxHeight: 220,
                  overflowY: "auto",
                }}
              >
                {matches.map((a) => (
                  <button
                    key={a.id}
                    onClick={() => setPicked(a)}
                    style={{
                      display: "block",
                      width: "100%",
                      textAlign: "left",
                      padding: "8px 10px",
                      background: "none",
                      border: "none",
                      borderBottom: `1px solid ${t.border}`,
                      color: t.text2,
                      cursor: "pointer",
                      fontSize: 14,
                    }}
                  >
                    {a.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {err ? (
          <div style={{ color: t.red, fontSize: 12, marginBottom: 10 }}>{err}</div>
        ) : null}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Btn variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Btn>
          <Btn
            variant="primary"
            size="sm"
            disabled={!picked || busy}
            onClick={confirm}
          >
            {busy ? "Replacing…" : "Replace author"}
          </Btn>
        </div>
      </div>
    </div>
  );
}
