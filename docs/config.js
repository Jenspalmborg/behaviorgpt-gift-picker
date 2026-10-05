// Where the API lives. Run locally (uvicorn app:app), the page and the API
// share one server. On GitHub Pages the page is static, and the API is the
// backend on Render, whose key and Pinterest requests stay server side.
window.GIFT_API = location.hostname.endsWith("github.io")
  ? "https://behaviorgpt-gift-picker.onrender.com"
  : "";
