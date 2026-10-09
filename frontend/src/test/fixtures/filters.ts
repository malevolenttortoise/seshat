// Filters page fixtures: a few MAM categories in two main groups, the
// settings blob with some allow / exclude lists set.
import settings from "./settings.json";

const cat = (id: string, name: string, main_id: string, main_name: string) => ({
  id, name, main_id, main_name, normalized: `${main_name.replace("-", "")} ${name}`.toLowerCase(),
});

export const filtersRoutes = {
  "GET /v1/enums": {
    categories: [
      cat("63", "Fantasy", "14", "E-Books"),
      cat("64", "Science Fiction", "14", "E-Books"),
      cat("67", "Mystery", "14", "E-Books"),
      cat("41", "Fantasy", "13", "AudioBooks"),
      cat("47", "Science Fiction", "13", "AudioBooks"),
    ],
    languages: ["English", "German", "French"],
    formats: ["epub", "mobi", "azw3", "pdf", "m4b", "mp3"],
  },
  "GET /v1/settings": {
    ...settings,
    allowed_categories: ["ebooks fantasy", "ebooks science fiction"],
    excluded_categories: ["ebooks mystery"],
    allowed_formats: ["epub", "m4b"],
    excluded_formats: ["pdf"],
    allowed_languages: ["english"],
  },
};
