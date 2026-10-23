// MAM content tags as small chips (2026-10 audit wave 5a, G124 / G125).
// An announce carries several ("Crime, Mystery, Thriller/Suspense"); the
// single `category` string keeps only the first. Renders nothing when the
// list is empty or unknown (rows from before the change, search-API grabs).
import { useTheme } from "../theme";

export function CategoryChips({ categories }: { categories?: string[] | null }) {
  const t = useTheme();
  if (!categories || categories.length === 0) return null;
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
      {categories.map((c) => (
        <span
          key={c}
          style={{
            fontSize: 10,
            padding: "1px 7px",
            borderRadius: 99,
            background: t.bg3,
            color: t.text2,
            border: `1px solid ${t.borderL}`,
            whiteSpace: "nowrap",
          }}
        >
          {c}
        </span>
      ))}
    </span>
  );
}
