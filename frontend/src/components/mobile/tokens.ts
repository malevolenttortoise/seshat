// Mobile design tokens — the size and spacing scale every mobile
// primitive composes from.
//
// Two viewport tiers consume these tokens:
//   - phone   (useViewport.isMobile): tightest, every pixel matters
//   - tablet  (useViewport.isTablet): same touch targets, more breathing
//             room (padding, gaps, type one notch up)
//
// Touch targets follow Apple HIG: 44pt minimum, 48pt for primary
// actions. We never go below 44 in the mobile codepath — the whole
// reason these primitives exist is so finger-sized hit areas are the
// default, not a thing each widget has to remember.
import type { Viewport } from "../../hooks/useViewport";

export const TAP = {
  min: 44,
  primary: 48,
  icon: 44,
} as const;

export const RADIUS = {
  sm: 8,
  md: 10,
  lg: 12,
  xl: 16,
  full: 9999,
} as const;

export interface Scale {
  type: {
    title: number;
    heading: number;
    body: number;
    label: number;
    caption: number;
    micro: number;
  };
  space: {
    xs: number;
    sm: number;
    md: number;
    lg: number;
    xl: number;
    xxl: number;
  };
  pad: {
    tight: number;
    normal: number;
    loose: number;
  };
}

// Phone scale (≤ 700px). Tablet scale lives below — same shape,
// one notch larger across the board.
const PHONE: Scale = {
  type: { title: 22, heading: 18, body: 16, label: 15, caption: 13, micro: 12 },
  space: { xs: 4, sm: 8, md: 12, lg: 16, xl: 20, xxl: 24 },
  pad: { tight: 12, normal: 16, loose: 20 },
};

const TABLET: Scale = {
  type: { title: 26, heading: 20, body: 17, label: 16, caption: 14, micro: 13 },
  space: { xs: 4, sm: 10, md: 14, lg: 18, xl: 24, xxl: 32 },
  pad: { tight: 14, normal: 20, loose: 28 },
};

// Single helper every mobile primitive calls. Pass the viewport from
// useViewport(), get back the right scale. Keeps individual components
// from having to branch on isMobile/isTablet themselves.
export function scaleFor(vp: Viewport): Scale {
  return vp.isTablet ? TABLET : PHONE;
}

// True for any "mobile codepath" viewport. Phone OR iPad. Components
// that only have a desktop variant + a mobile variant key off this.
export function useMobileCodepath(vp: Viewport): boolean {
  return vp.isMobile || vp.isTablet;
}
