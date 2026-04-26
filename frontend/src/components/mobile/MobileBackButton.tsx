// Hierarchical back button. Each mobile page declares its parent
// page via the `to` prop, so the back path is always predictable
// (Author Detail → Authors, Library → Dashboard, etc.) regardless
// of how the user got there. Pages that are themselves a "root"
// (the unified dashboard) omit the button entirely or pass no `to`.
import { useTheme } from "../../theme";
import { useNavigation } from "../../providers/NavigationProvider";
import { TAP, RADIUS } from "./tokens";

export interface MobileBackButtonProps {
  // Target page to navigate to when tapped. Omit to hide the button
  // entirely — useful on root pages like the dashboard.
  to?: string;
  // Optional arg (page-specific id, slug, etc.). Most parent pages
  // don't need one — the user lands back on a list view.
  arg?: string | number | null;
  // Override the visible label. Defaults to "Back".
  label?: string;
}

export function MobileBackButton({ to, arg, label = "Back" }: MobileBackButtonProps) {
  const t = useTheme();
  const { nav } = useNavigation();

  if (!to) return null;

  return (
    <button
      onClick={() => nav(to, arg)}
      aria-label={label}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        height: TAP.min,
        padding: "0 14px 0 10px",
        background: t.bg3,
        color: t.text2,
        border: `1px solid ${t.border}`,
        borderRadius: RADIUS.full,
        fontSize: 14,
        fontWeight: 600,
        cursor: "pointer",
        alignSelf: "flex-start",
      }}
    >
      <span style={{ fontSize: 18, lineHeight: 1 }}>‹</span>
      <span>{label}</span>
    </button>
  );
}
