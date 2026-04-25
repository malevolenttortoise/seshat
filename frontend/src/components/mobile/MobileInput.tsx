// Touch-sized text input. 16px font is mandatory on mobile — iOS
// Safari zooms in on focus when the field is below 16px (the global
// CSS in index.css already enforces this for any input on phone, but
// we set it locally too so iPad behaves the same way).
import type { InputHTMLAttributes, ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { TAP, RADIUS, scaleFor } from "./tokens";

export interface MobileInputProps extends InputHTMLAttributes<HTMLInputElement> {
  leadingIcon?: ReactNode;
  trailing?: ReactNode;
  fullWidth?: boolean;
}

export function MobileInput({
  leadingIcon,
  trailing,
  fullWidth = true,
  style,
  ...rest
}: MobileInputProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const padLeft = leadingIcon ? 38 : s.pad.tight;
  const padRight = trailing ? 40 : s.pad.tight;

  return (
    <div
      style={{
        position: "relative",
        width: fullWidth ? "100%" : undefined,
      }}
    >
      <input
        {...rest}
        style={{
          width: "100%",
          minHeight: TAP.min,
          padding: `0 ${padRight}px 0 ${padLeft}px`,
          background: t.inp,
          border: `1px solid ${t.border}`,
          borderRadius: RADIUS.md,
          color: t.text,
          fontSize: s.type.body,
          outline: "none",
          ...style,
        }}
      />
      {leadingIcon && (
        <span
          style={{
            position: "absolute",
            left: 12,
            top: "50%",
            transform: "translateY(-50%)",
            color: t.tg,
            display: "flex",
            pointerEvents: "none",
          }}
        >
          {leadingIcon}
        </span>
      )}
      {trailing && (
        <span
          style={{
            position: "absolute",
            right: 8,
            top: "50%",
            transform: "translateY(-50%)",
            display: "flex",
          }}
        >
          {trailing}
        </span>
      )}
    </div>
  );
}
