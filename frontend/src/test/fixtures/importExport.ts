// Import / Export fixtures: a preview of three pasted links — one new
// book, one already owned, one that failed to fetch.
export const IMPORT_URLS = [
  "https://books.example.invalid/show/1-the-glass-orchard",
  "https://books.example.invalid/show/2-ninefold-tide",
  "https://books.example.invalid/show/3-broken",
].join("\n");

export const importRoutes = {
  "POST /discovery/books/import-preview": {
    results: [
      {
        status: "new",
        book: {
          title: "Rootbound", author_name: "Ada Quill", series_name: "Orchard Cycle", series_index: 3,
          pub_date: "2027-02-02", cover_url: null,
          series_options: [{ name: "Orchard Cycle", position: 3 }, { name: "Quill Botanicals", position: 1 }],
        },
      },
      { status: "owned", book: { title: "Ninefold Tide", author_name: "Ada Quill", pub_date: "2024-05-14" } },
      { status: "error", error: "Couldn't read the page" },
    ],
  },
};
