// Which book's sidebar is open, and its 200ms closing animation.
//
// The author-detail, Discovery MAM and Books pages (desktop + phone)
// each carried this verbatim (wave 5b S7), as did Series detail and
// Hidden (S18). The phone pages open a book with a bare `setSb(b)`, the
// desktop ones with `toggleSb` (a second click on the same book closes
// it) or `openSb` (Series detail); each stays as it was.
import { useState } from "react";
import type { Book } from "../types";

export const SIDEBAR_CLOSE_MS = 200;

export function useBookSidebar() {
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, SIDEBAR_CLOSE_MS);
  };
  const openSb = (b: Book) => {
    setSbClosing(false);
    setSb(b);
  };
  const toggleSb = (b: Book) => {
    if (sb && sb.id === b.id) closeSb();
    else openSb(b);
  };

  return { sb, setSb, sbClosing, closeSb, openSb, toggleSb };
}
