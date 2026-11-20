# Fleet design system

One visual system for the whole live-servers fleet. Canonical CSS:
`fleet.css` in this directory. Every rebuilt service vendors a
byte-identical copy at `services/<svc>/assets/fleet.css` and serves it
at `GET {BASE}/assets/fleet.css`; pages link it and add only small
token-based `<style>` blocks of their own.

Page shell (header/footer) pattern:

```html
<link rel="stylesheet" href="{BASE}/assets/fleet.css">
<header class="fl-header"><div class="fl-header-inner">
  <a class="fl-wordmark" href="/members/">911TransEdu</a>
  <span class="fl-service">Members</span>
  <nav class="fl-nav">…service-appropriate links…</nav>
</div></header>
<main class="fl-main">…</main>
<footer class="fl-footer"><div class="fl-footer-inner">
  <span>Taylor's Transgender Patient Awareness, LLC</span>
  <a href="https://911transedu.net">911transedu.net</a>
</div></footer>
```

Rules (from DESIGN.md §3): mobile-first; no webfonts; times in
24-hour format; no family/personal information and no infrastructure
detail on any public page; Psychic EMT material keeps its auto-links
(Directors/Producers roles only; Apple TV, Netflix, 44 Blue, USO,
NCIS). Contrast of token pairs ≥ 4.5:1 for body text.
