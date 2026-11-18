# Pathways Editor (Flutter Web) — serves the PREBUILT Web bundle.
#
# The bundle (build/web) is compiled by the "Deploy Flutter app" GitHub
# Actions workflow on the runner — never on the host: a Flutter build
# needs far more RAM than this host can spare next to the live fleet.
# ops/deploy-flutter.sh copies the fresh bundle into build/web/ before
# `docker compose build flutter` runs, so this image stays a tiny nginx.
FROM nginx:alpine
COPY build/web /usr/share/nginx/html/flutter
EXPOSE 80
