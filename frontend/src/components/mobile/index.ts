// Mobile primitives barrel — every mobile widget consumes from here.
// The mobile/ folder is the boundary between desktop and mobile
// styling: desktop components NEVER import from this path, and these
// primitives never reach for desktop components either. Keep them
// independently styled so the two surfaces evolve without entangling.
export { MobileBtn } from "./MobileBtn";
export type { MobileBtnProps } from "./MobileBtn";

export { MobileIconBtn } from "./MobileIconBtn";
export type { MobileIconBtnProps } from "./MobileIconBtn";

export { MobileBadge } from "./MobileBadge";
export type { MobileBadgeProps } from "./MobileBadge";

export { MobileChip } from "./MobileChip";
export type { MobileChipProps } from "./MobileChip";

export { MobileRow } from "./MobileRow";
export type { MobileRowProps } from "./MobileRow";

export { MobileInput } from "./MobileInput";
export type { MobileInputProps } from "./MobileInput";

export { MobileSheet } from "./MobileSheet";
export type { MobileSheetProps } from "./MobileSheet";

export { MobilePageHeader } from "./MobilePageHeader";
export type { MobilePageHeaderProps } from "./MobilePageHeader";

export { MobilePagination } from "./MobilePagination";
export type { MobilePaginationProps } from "./MobilePagination";

export { MobileSection } from "./MobileSection";
export type { MobileSectionProps } from "./MobileSection";

export {
  TAP,
  RADIUS,
  scaleFor,
  useMobileCodepath,
} from "./tokens";
export type { Scale } from "./tokens";
