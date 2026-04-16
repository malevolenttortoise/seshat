import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useTheme } from "../theme";

type Variant = "primary" | "secondary" | "danger" | "ghost" | "accent" | "default";

export interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children: ReactNode;
  fullWidth?: boolean;
  size?: string;
}

export function Btn({
  variant = "secondary",
  children,
  fullWidth,
  size,
  style,
  ...rest
}: BtnProps) {
  const theme = useTheme();
  const palette: Record<Variant, { bg: string; fg: string; border: string }> = {
    primary: { bg: theme.accent, fg: theme.bg, border: theme.accent },
    accent: { bg: theme.accent, fg: theme.bg, border: theme.accent },
    secondary: { bg: theme.bg3, fg: theme.text2, border: theme.border },
    default: { bg: theme.bg3, fg: theme.text2, border: theme.border },
    danger: { bg: theme.err, fg: theme.bg, border: theme.err },
    ghost: { bg: "transparent", fg: theme.text2, border: theme.border },
  };
  const c = palette[variant];
  const sizeStyle = size === "sm"
    ? { padding: "4px 10px", fontSize: 12 }
    : size === "xs"
    ? { padding: "2px 8px", fontSize: 11 }
    : { padding: "8px 14px", fontSize: 14 };
  return (
    <button
      {...rest}
      style={{
        background: c.bg,
        color: c.fg,
        border: `1px solid ${c.border}`,
        borderRadius: 8,
        fontWeight: 600,
        cursor: "pointer",
        width: fullWidth ? "100%" : undefined,
        ...sizeStyle,
        ...style,
      }}
    >
      {children}
    </button>
  );
}
