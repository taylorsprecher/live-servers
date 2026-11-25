#!/usr/bin/env python3
"""Stream Mic receiver — standalone edition (no dependencies).

Built 2026-11-24 for Taylor Sprecher ("Create a python to allow
streaming"). This is the same receiver that runs inside the member
site at members.911transedu.net/members/stream, packaged to run on
any machine:

    python3 stream_receiver.py [--port 8765] [--out ./stream_data]

It serves the streaming page at / and accepts microphone or screen+microphone chunks at
POST /upload?session=<id>&seq=<n>&ext=<mp4|webm|ogg|wav|m4a>, saving
them under <out>/<session>/ with a session.log of timestamps.

Phone note: phone browsers only allow microphone capture on HTTPS
pages (or localhost). For phone use, the member-site copy above is
the one that works anywhere; this standalone is for a computer
browser on the same machine, or behind your own HTTPS.
"""
import argparse
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_EXTS = {"mp4", "webm", "ogg", "wav", "m4a", "bin"}
MAX_CHUNK_BYTES = 5 * 1024 * 1024

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stream Mic \u2014 standalone receiver</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 0 auto; padding: 16px;">
<h1>Stream Mic</h1>
<p>Streams this device's microphone \u2014 or its screen plus microphone \u2014 to this receiver in 10-second chunks.</p>
<label style="display:block; margin:6px 0;"><input type="radio" name="mode" value="mic" checked> Microphone only</label>
<label style="display:block; margin:6px 0;"><input type="radio" name="mode" value="screen" id="modeScreen"> Screen + microphone</label>
<div id="screenNote" style="font-size: 13px; opacity: 0.8;"></div>
<button id="toggle" style="font-size: 20px; padding: 14px 28px; width: 100%;">Start streaming</button>
<div id="status" style="margin: 14px 0;">Stopped.</div>
<div id="count"></div>
<script>
(function () {
  var btn = document.getElementById('toggle');
  var status = document.getElementById('status');
  var count = document.getElementById('count');
  var screenNote = document.getElementById('screenNote');
  var screenRadio = document.getElementById('modeScreen');
  var recorder = null, streams = [], seq = 0, sent = 0, failed = 0;
  var session = '';
  var screenSupported = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  if (!screenSupported) {
    screenRadio.disabled = true;
    screenNote.textContent = 'Screen capture is not offered by this browser on this device; the microphone stream still works.';
  }
  function mode() { var el = document.querySelector('input[name="mode"]:checked'); return el ? el.value : 'mic'; }
  function pickType(m) {
    var c = m === 'screen' ? ['video/mp4', 'video/webm;codecs=vp9,opus', 'video/webm', '']
                           : ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg', ''];
    for (var i = 0; i < c.length; i++) {
      if (!c[i] || (window.MediaRecorder && MediaRecorder.isTypeSupported(c[i]))) return c[i];
    }
    return '';
  }
  function extFor(t) {
    if (t.indexOf('mp4') >= 0) return 'mp4';
    if (t.indexOf('webm') >= 0) return 'webm';
    if (t.indexOf('ogg') >= 0) return 'ogg';
    return 'bin';
  }
  function show() { count.textContent = sent + ' chunk(s) saved' + (failed ? ', ' + failed + ' failed' : '') + ' \u2014 session ' + session; }
  function upload(blob) {
    var mySeq = seq++;
    fetch('/upload?session=' + encodeURIComponent(session) + '&seq=' + mySeq + '&ext=' + extFor(blob.type || ''),
      { method: 'POST', body: blob }).then(function (r) { if (r.ok) { sent++; } else { failed++; } show(); })
      .catch(function () { failed++; show(); });
  }
  function stopTracks() { streams.forEach(function (s) { s.getTracks().forEach(function (t) { t.stop(); }); }); streams = []; }
  function stop() {
    if (recorder && recorder.state !== 'inactive') { recorder.stop(); }
    recorder = null;
    btn.textContent = 'Start streaming'; status.textContent = 'Stopped.';
  }
  btn.addEventListener('click', function () {
    if (recorder) { stop(); return; }
    var m = mode();
    var micP = navigator.mediaDevices.getUserMedia({ audio: true });
    var capP = m === 'screen'
      ? Promise.all([navigator.mediaDevices.getDisplayMedia({ video: true, audio: false }), micP])
      : micP.then(function (s) { return [s]; });
    capP.then(function (parts) {
      streams = parts;
      var tracks = [];
      parts.forEach(function (s) { s.getTracks().forEach(function (t) { tracks.push(t); }); });
      var combined = new MediaStream(tracks);
      var type = pickType(m);
      var opts = {};
      if (type) opts.mimeType = type;
      if (m === 'screen') { opts.videoBitsPerSecond = 1000000; opts.audioBitsPerSecond = 64000; }
      recorder = new MediaRecorder(combined, opts);
      recorder.ondataavailable = function (e) { if (e.data && e.data.size > 0) upload(e.data); };
      recorder.onstop = stopTracks;
      var vt = combined.getVideoTracks()[0];
      if (vt) { vt.addEventListener('ended', function () { stop(); }); }
      session = (m === 'screen' ? 'screen-' : 'mic-') + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
      seq = 0; sent = 0; failed = 0; show();
      recorder.start(10000);
      btn.textContent = 'Stop streaming';
      status.textContent = 'Streaming \u2014 a chunk is saved every 10 seconds.';
    }).catch(function () { status.textContent = 'Permission was not given.'; stopTracks(); });
  });
})();
</script>
</body></html>
"""


def save_chunk(out_dir, session, seq, ext, data):
    if not _SESSION_RE.match(session or ""):
        return False, "bad session"
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        return False, "bad seq"
    if not (0 <= seq <= 100000):
        return False, "bad seq"
    ext = (ext or "").lower()
    if ext not in _EXTS:
        return False, "bad ext"
    if not data or len(data) > MAX_CHUNK_BYTES:
        return False, "bad size"
    folder = os.path.join(out_dir, session)
    os.makedirs(folder, exist_ok=True)
    name = "chunk-%04d.%s" % (seq, ext)
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(data)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(folder, "session.log"), "a", encoding="utf-8") as fh:
        fh.write("%s seq=%d file=%s bytes=%d\n" % (stamp, seq, name, len(data)))
    return True, name


def make_handler(out_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if urlparse(self.path).path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/upload":
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_CHUNK_BYTES:
                self.send_response(400)
                self.end_headers()
                return
            data = self.rfile.read(length)
            ok, msg = save_chunk(
                out_dir,
                qs.get("session", [""])[0],
                qs.get("seq", [""])[0],
                qs.get("ext", [""])[0],
                data,
            )
            body = (("ok " + msg) if ok else ("error " + msg)).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(time.strftime("%H:%M:%S"), msg, flush=True)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Stream Mic receiver (standalone)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default="./stream_data")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(args.out))
    print("Stream Mic receiver on http://0.0.0.0:%d — saving to %s" % (args.port, args.out))
    print("Open the page, tap Start, and chunks land here every 10 seconds. Ctrl-C stops.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
