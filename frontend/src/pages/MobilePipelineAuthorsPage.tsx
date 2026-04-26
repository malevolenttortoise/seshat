// Mobile-native pipeline author-lists page (allow / ignore /
// tentative_review). Tab chips + search + add-author textarea
// (collapsed in a section) + paginated row list with per-row
// move/remove buttons.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { Ic } from "../icons";
import {
  MobileChip,
  MobileBtn,
  MobileSection,
  MobileInput,
  MobilePagination,
  MobileBackButton,
} from "../components/mobile";

type ListName = "allowed" | "ignored" | "tentative_review";

interface AuthorRow {
  name: string;
  normalized: string;
  source: string;
  added_at: string;
}

interface ListResponse {
  list_name: ListName;
  count: number;
  items: AuthorRow[];
}

interface OverviewResponse {
  counts: Record<ListName, number>;
}

const TAB_LABELS: Record<ListName, string> = {
  allowed: "Allowed",
  ignored: "Ignored",
  tentative_review: "Tentative",
};

const PAGE_SIZE = 50;

export default function MobilePipelineAuthorsPage() {
  const t = useTheme();
  const [tab, setTab] = useState<ListName>("allowed");
  const [counts, setCounts] = useState<Record<ListName, number> | null>(null);
  const [items, setItems] = useState<AuthorRow[] | null>(null);
  const [totalCount, setTotalCount] = useState(0);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [addText, setAddText] = useState("");

  const refreshCounts = async () => {
    try {
      const r = await api.get<OverviewResponse>("/v1/authors");
      setCounts(r.counts);
    } catch (e) {
      setError(String(e));
    }
  };

  const refreshList = async () => {
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (search) params.set("search", search);
      const r = await api.get<ListResponse>(`/v1/authors/${tab}?${params}`);
      setItems(r.items);
      setTotalCount(r.count);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    refreshCounts();
  }, []);

  useEffect(() => {
    setItems(null);
    refreshList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, search, offset]);

  const changeTab = (next: ListName) => {
    if (next === tab) return;
    setTab(next);
    setOffset(0);
    setSearch("");
  };

  const addAuthors = async () => {
    if (!addText.trim()) return;
    if (tab === "tentative_review") {
      setError("Tentative review is auto-populated; cannot add manually.");
      return;
    }
    const names = addText
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .slice(0, 500);
    if (names.length === 0) return;
    setBusy(true);
    try {
      const r = await api.post<{ added: number; skipped: number }>(
        `/v1/authors/${tab}`,
        { names },
      );
      setAddText("");
      setError(
        r.skipped > 0
          ? `Added ${r.added}, skipped ${r.skipped}.`
          : `Added ${r.added}.`,
      );
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: AuthorRow) => {
    if (!confirm(`Remove "${row.name}" from ${TAB_LABELS[tab]}?`)) return;
    setBusy(true);
    try {
      await api.del(`/v1/authors/${tab}/${encodeURIComponent(row.normalized)}`);
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const move = async (row: AuthorRow, to: "allowed" | "ignored") => {
    setBusy(true);
    try {
      await api.post(
        `/v1/authors/${tab}/${encodeURIComponent(row.normalized)}/move`,
        { to },
      );
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const bulkMoveTentative = async (to: "allowed" | "ignored") => {
    const count = counts?.tentative_review ?? 0;
    if (count === 0) return;
    if (
      !confirm(
        `${to === "ignored" ? "Ignore" : "Allow"} all ${count} tentative author(s)?`,
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.post<{ moved: number }>(
        `/v1/authors/tentative_review/bulk-move`,
        { to },
      );
      setError(`Moved ${r.moved} author(s) to ${to}.`);
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const page = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />

      <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
        Author Lists
      </h1>

      {error && (
        <div
          style={{
            padding: "10px 14px",
            background: t.ylwb,
            border: `1px solid ${t.ylwt}`,
            color: t.ylw,
            borderRadius: 10,
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}

      {/* Tab chips */}
      <div
        style={{
          display: "flex",
          gap: 6,
          overflowX: "auto",
          scrollbarWidth: "none",
        }}
      >
        {(Object.keys(TAB_LABELS) as ListName[]).map((name) => (
          <MobileChip
            key={name}
            active={tab === name}
            onClick={() => changeTab(name)}
          >
            {TAB_LABELS[name]} ({counts?.[name] ?? "…"})
          </MobileChip>
        ))}
      </div>

      {/* Search */}
      <MobileInput
        value={search}
        onChange={(e) => {
          setSearch(e.target.value);
          setOffset(0);
        }}
        placeholder="Search authors"
        leadingIcon={Ic.search}
        trailing={
          search ? (
            <button
              onClick={() => setSearch("")}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: t.tg,
                padding: 4,
                display: "flex",
                width: 32,
                height: 32,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {Ic.x}
            </button>
          ) : undefined
        }
      />

      {/* Add authors (allowed/ignored only) */}
      {tab !== "tentative_review" && (
        <MobileSection title={`Add to ${TAB_LABELS[tab]}`} defaultOpen={false}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <textarea
              value={addText}
              onChange={(e) => setAddText(e.target.value)}
              placeholder="Paste author names — one per line or comma-separated"
              rows={4}
              style={{
                width: "100%",
                padding: 10,
                background: t.inp,
                color: t.text,
                border: `1px solid ${t.border}`,
                borderRadius: 10,
                fontSize: 16,
                resize: "vertical",
                fontFamily: "inherit",
              }}
            />
            <MobileBtn
              variant="primary"
              primary
              fullWidth
              onClick={addAuthors}
              disabled={busy || !addText.trim()}
            >
              {busy ? "Adding…" : "Add"}
            </MobileBtn>
          </div>
        </MobileSection>
      )}

      {/* Bulk-move actions for tentative_review */}
      {tab === "tentative_review" && (counts?.tentative_review ?? 0) > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <MobileBtn
            variant="primary"
            primary
            fullWidth
            onClick={() => bulkMoveTentative("allowed")}
            disabled={busy}
          >
            Allow all
          </MobileBtn>
          <MobileBtn
            variant="danger"
            fullWidth
            onClick={() => bulkMoveTentative("ignored")}
            disabled={busy}
          >
            Ignore all
          </MobileBtn>
        </div>
      )}

      {/* List */}
      {items === null ? (
        <div style={{ padding: 24, textAlign: "center", color: t.tg }}>
          Loading…
        </div>
      ) : items.length === 0 ? (
        <div
          style={{
            padding: 24,
            textAlign: "center",
            color: t.tg,
            fontSize: 13,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          {search ? "No matches." : "List is empty."}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {items.map((row) => (
            <div
              key={row.normalized}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
                padding: 10,
                background: t.bg2,
                border: `1px solid ${t.border}`,
                borderRadius: 10,
              }}
            >
              <div
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: t.text,
                }}
              >
                {row.name}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {tab === "allowed" && (
                  <MobileBtn
                    variant="ghost"
                    onClick={() => move(row, "ignored")}
                    disabled={busy}
                    style={{ minHeight: 36, fontSize: 13 }}
                  >
                    → Ignored
                  </MobileBtn>
                )}
                {tab === "ignored" && (
                  <MobileBtn
                    variant="ghost"
                    onClick={() => move(row, "allowed")}
                    disabled={busy}
                    style={{ minHeight: 36, fontSize: 13 }}
                  >
                    → Allowed
                  </MobileBtn>
                )}
                {tab === "tentative_review" && (
                  <>
                    <MobileBtn
                      variant="ghost"
                      onClick={() => move(row, "allowed")}
                      disabled={busy}
                      style={{ minHeight: 36, fontSize: 13 }}
                    >
                      Allow
                    </MobileBtn>
                    <MobileBtn
                      variant="ghost"
                      onClick={() => move(row, "ignored")}
                      disabled={busy}
                      style={{ minHeight: 36, fontSize: 13 }}
                    >
                      Ignore
                    </MobileBtn>
                  </>
                )}
                <MobileBtn
                  variant="ghost"
                  onClick={() => remove(row)}
                  disabled={busy}
                  style={{ minHeight: 36, fontSize: 13, color: t.red }}
                >
                  Remove
                </MobileBtn>
              </div>
            </div>
          ))}
        </div>
      )}

      <MobilePagination
        page={page}
        totalPages={totalPages}
        onPrev={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        onNext={() => setOffset(offset + PAGE_SIZE)}
      />
    </div>
  );
}
