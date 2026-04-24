import { useTheme } from "../theme";
import { Ic } from "../icons";

export interface SearchBarProps {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}

export function SearchBar({ value, onChange, placeholder = "Search..." }: SearchBarProps) {
  const t = useTheme();
  return (
    <div
      className="seshat-search"
      style={{ position: "relative", flex: 1, maxWidth: 340 }}
    >
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        style={{ width: "100%", padding: "8px 32px 8px 34px", background: t.inp, border: `1px solid ${t.border}`, borderRadius: 8, color: t.text2, fontSize: 13 }}
      />
      <span style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: t.tg, pointerEvents: "none" }}>{Ic.search}</span>
      {value && (
        <button
          onClick={() => onChange("")}
          style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: t.tg, padding: 2, display: "flex" }}
        >{Ic.x}</button>
      )}
    </div>
  );
}
