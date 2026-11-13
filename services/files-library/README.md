# Files Library

Taylor's complete file library, served at `https://members.911transedu.net/files/`
behind the nginx gateway (host port `127.0.0.1:18113`, `BASE_PATH=/files`).

- `app.py` — Python 3 stdlib only. Renders the listing once at startup from
  `library.json` (grouped by project, client-side search), serves
  `/files/health`, and streams downloads at `/files/dl/<id>` from the
  `fileslib-data` volume (`FILESLIB_DATA_DIR=/data`) in 256 KB chunks.
- `library.json` — generated from Taylor's All Files Index with the final
  hold decisions applied (2026-11-13). Held entries carry ONLY
  file/type/date/group: no description, no size, no download id, so held
  content cannot leak through the listing. There are 1,088 entries:
  767 downloadable entries (747 unique files), 255 "On file — available on
  request", and 66 held entries shown name-only under "Held — ask Taylor".
- File bytes are NEVER in git. They are staged into the volume by
  `ops/deploy-files-library.sh` (run by the `deploy-files-library`
  workflow) from a checksummed tar archive fetched over a short-lived
  capability URL held in a GitHub secret; the archive and secret are
  removed after staging. Held files' bytes are never in the archive.

History: the library was first built for the training website, whose
platform content review refused to publish it (2026-11-13, "personal
details about other people"). Taylor directed: "Put the Files library on
my member server instead" — same day. The paid CCCE course material
stays paid (member site only); it is in the held set.
