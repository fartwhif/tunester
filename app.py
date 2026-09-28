#!/usr/bin/env python3
"""tunester - dirty song name in, verified uncensored MP3 out.

Output spec: MP3 160 kbps CBR / 44.1 kHz / stereo, named "ARTIST - TITLE (YEAR).mp3".

Usage:
  python3 app.py "that screaming song about war" [-o OUTDIR] [options]
  python3 app.py --serve [--host 0.0.0.0] [--port 8102]
"""
import argparse
import json
import os
import sys
from pathlib import Path

from pipeline import Options, TuneError, tune
from sources import SOURCES


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="tunester",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="dirty song name")
    ap.add_argument("-o", "--outdir", type=Path, default=None,
                    help="output directory (default: $TUNESTER_OUTDIR or ./downloads)")
    ap.add_argument("--cookies", type=Path, default=None,
                    help="Netscape cookies file (YouTube age/explicit access)")
    ap.add_argument("--source", choices=[s.id for s in SOURCES], default=None,
                    help="use one source instead of the best-to-worst fallback chain")
    ap.add_argument("--keep-source", action="store_true",
                    help="keep the downloaded source file")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip LLM discovery + QC gate (heuristic parse, filter-only verify)")
    ap.add_argument("--json", action="store_true",
                    help="print a JSON result object on success")
    ap.add_argument("--serve", action="store_true",
                    help="run the HTTP API + web UI")
    ap.add_argument("--host", default=os.environ.get("TUNESTER_WEB_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TUNESTER_WEB_PORT", "8102")))
    args = ap.parse_args(argv)

    if args.serve:
        import web
        outdir = args.outdir if args.outdir else Options().outdir
        web.serve(args.host, args.port, outdir)
        return 0

    if not args.name:
        ap.error("a dirty song name is required (or use --serve)")

    opts = Options()
    if args.outdir:
        opts.outdir = args.outdir
    opts.cookies = args.cookies
    opts.source = args.source
    opts.keep_source = args.keep_source
    opts.use_llm = not args.no_llm

    try:
        res = tune(args.name, opts)
    except TuneError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "artist": res.artist,
            "title": res.title,
            "year": res.year,
            "source": res.source,
            "url": res.url,
            "file": str(res.file),
            "gate": res.gate,
            "duration": res.duration,
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
