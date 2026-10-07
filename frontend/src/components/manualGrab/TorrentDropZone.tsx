// Drop zone + "Add .torrent" picker for Manual Grab uploads. Desktop
// gets drag-and-drop and the picker; `compact` (mobile) is the picker
// only, since phones have nothing to drag from.
import { useRef, useState } from "react";
import { useTheme } from "../../theme";

export interface TorrentDropZoneProps {
  onFiles: (files: File[]) => void;
  multiple?: boolean;
  compact?: boolean;
  disabled?: boolean;
}

export function TorrentDropZone({ onFiles, multiple, compact, disabled }: TorrentDropZoneProps) {
  const t = useTheme();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  const take = (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (files.length) onFiles(multiple ? files : files.slice(0, 1));
  };

  const picker = (
    <>
      <input
        ref={input}
        type="file"
        accept=".torrent,application/x-bittorrent"
        multiple={multiple}
        style={{ display: "none" }}
        onChange={(e) => {
          take(e.target.files);
          e.target.value = ""; // the same file can be picked again
        }}
      />
      <button
        type="button"
        disabled={disabled}
        onClick={() => input.current?.click()}
        style={{
          padding: compact ? "12px 14px" : "6px 12px",
          width: compact ? "100%" : undefined,
          fontSize: compact ? 15 : 13, fontWeight: 600, borderRadius: compact ? 10 : 6,
          background: t.bg4, color: t.text2, border: `1px solid ${t.border}`,
          cursor: disabled ? "default" : "pointer",
        }}
      >
        {multiple ? "Add .torrent files" : "Add a .torrent"}
      </button>
    </>
  );

  if (compact) return picker;

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        if (!disabled) take(e.dataTransfer.files);
      }}
      style={{
        display: "flex", alignItems: "center", justifyContent: "center", gap: 12,
        padding: "18px 14px", borderRadius: 10,
        border: `2px dashed ${over ? t.accent : t.border}`,
        background: over ? t.abg : "transparent",
        color: t.td, fontSize: 13,
      }}
    >
      <span>
        {multiple ? "Drop .torrent files here" : "Drop a .torrent here"} or
      </span>
      {picker}
    </div>
  );
}
