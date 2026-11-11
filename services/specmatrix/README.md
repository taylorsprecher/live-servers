# Spec Matrix — cross-brand device & camera spec analyzer

Deployed on the audited AWS host as part of the live-servers set, behind
the nginx gateway at **http://52.41.84.116:443/specmatrix/** (host port
18110, loopback only; the gateway is the public entry point).

- Python 3 stdlib only (`http.server`). No pip installs, no build step.
- One self-contained page: `index.html` embeds the dataset, CSS and JS —
  the page makes no external requests.
- Faithful port of the React component Taylor pasted on 2026-11-11
  (verbatim source: `~/workspace/user/files/SpecMatrix_App_Pasted.jsx`).
  - 15 devices: 5 cameras (Sony A1 II, Canon R5 Mark II, Nikon Z9,
    RED V-Raptor 8K VV, Fujifilm X-H2S), 4 smartphones (iPhone 16 Pro
    Max, Galaxy S25 Ultra, Pixel 9 Pro XL, Xperia 1 VI), 3 laptops
    (MacBook Pro 16" M4 Max, ROG Zephyrus G16, XPS 16), 3 displays
    (ROG Swift OLED PG32UCDM, Pro Display XDR, Odyssey OLED G9) — every
    spec and score field carried over verbatim.
  - Four views: Side-by-Side comparison matrix (diff highlighting,
    average spec score), Catalog grid, Score Radar analytics bars,
    Deep Scanner table. Search + category + brand + max-price filters.
  - Pin up to 4 devices to compare (defaults: Sony A1 II + Canon R5 II);
    device detail modal; "Export Report" copies a plain-text comparison
    report to the clipboard with a toast confirmation.
  - The paste used an `AlertCircle` icon it never imported; the port
    renders the catalog empty-state icon as an inline SVG.
- Stateless: no database, no volume, nothing is saved server-side.
- Health: `GET /specmatrix/health` → `{"status": "ok"}`.
