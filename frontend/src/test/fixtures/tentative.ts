// Tentative queue fixtures: two held announces (one VIP, one with tags).

const items = [
  {
    id: 61,
    mam_torrent_id: "900011",
    torrent_name: "The Glass Orchard",
    author_blob: "Ada Quill",
    category: "Ebooks - Fantasy",
    language: "English",
    format: "epub",
    vip: true,
    scraped_metadata: { series: "Orchard Cycle", series_index: 2 },
    cover_path: null,
    status: "pending",
    created_at: "2026-10-09 12:10:00",
    categories: ["Fantasy", "Mystery"],
  },
  {
    id: 62,
    mam_torrent_id: "900012",
    torrent_name: "Saltwind",
    author_blob: "Bram Okafor",
    category: "Audiobooks - Science Fiction",
    language: "English",
    format: "m4b",
    vip: false,
    scraped_metadata: {},
    cover_path: "/staging/tentative-covers/62.jpg",
    status: "pending",
    created_at: "2026-10-09 13:30:00",
    categories: null,
  },
];

export const tentativeRoutes = {
  "GET /v1/tentative": { items },
};
