# Pathways Editor — Flutter app (fleet service)

A Flutter starter for the **connectable Pathways Platform**: a node-link
editor for pathway graphs. Drag nodes on the canvas, connect output
ports to input ports, and validate the flow (missing required inputs,
cycle detection, unreachable nodes) before it runs.

Built 2026-11-18 from Taylor's Cross-Platform design document
(`Cross-Platform.gdoc`, the GitHub Copilot session compilation), Option
B: "Flutter app starter for a node-link editor." The graph model in
`lib/pathway.dart` — `PathwayNode`, `PathwayEdge`, `PathwayController`
with `addNode` / `connect` / `validate` — is the document's own starter
model, extended with move/delete and the document's validation rules.

## Platforms — all established in parallel

The full project was created with `flutter create` for **iOS, Android,
Web, macOS, Windows, and Linux** in one pass (Taylor's direction:
"Establish for all parallel platforms"). The complete project — every
platform folder included — is preserved by Jasmine as
`Pathways_Editor_Flutter_Project.zip` and mirrored in Replit if needed.
This repo carries the source the fleet builds from (`lib/`, `test/`,
`web/`, `pubspec.yaml`); the platform folders regenerate anywhere with:

```sh
flutter create --platforms=ios,android,web,macos,windows,linux .
```

## How it runs in the fleet

- The Web build is what goes live: **https://members.911transedu.net/flutter/**
- The "Deploy Flutter app" workflow compiles `flutter build web
  --release --base-href /flutter/` **on the GitHub Actions runner**,
  rsyncs the bundle to the host, and `ops/deploy-flutter.sh` installs
  it and rebuilds this service's tiny nginx image. The host never runs
  a Flutter build (RAM).
- Backend: nginx serving the bundle at container path `/flutter/`,
  published on loopback only: `127.0.0.1:18125`.
- Known wrinkle: a full-fleet `deploy.sh` rsync does not carry
  `build/web` (it is not in git). If the Flutter page ever 404s after a
  full deploy, re-run the "Deploy Flutter app" workflow — the bundle
  also stays on the host at `/home/deploy/flutter-bundle/`.

## Local development

```sh
flutter pub get
flutter analyze     # clean
flutter test        # seeded example validates; broken graph does not
flutter run -d chrome
```
