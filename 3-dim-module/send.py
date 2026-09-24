"""Send whatever you type (or pass on the command line) to COM9 and print replies."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cnc_grbl import GrblController, GrblException


def print_reply(extras: list[str]) -> None:
    for line in extras:
        print(line)
    print("ok")


def send_one(cnc: GrblController, line: str) -> None:
    text = line.strip()
    if not text:
        return
    if text == "?":
        print(cnc.status().raw)
        return
    if text == "!":
        cnc.hold()
        print("hold")
        return
    if text == "~":
        cnc.resume()
        print("resume")
        return
    timeout = 60.0 if text.upper().startswith("$H") else None
    print_reply(cnc.send(text, timeout=timeout))


def run_interactive(cnc: GrblController) -> None:
    print("type G-code and Enter; quit / exit / Ctrl+C to stop")
    print("realtime:  ? status   ! hold   ~ resume")
    while True:
        try:
            line = input("> ")
        except EOFError:
            print()
            return
        if line.strip().lower() in {"quit", "exit"}:
            return
        try:
            send_one(cnc, line)
        except GrblException as exc:
            print(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send lines to Grbl on COM9 and print replies.")
    parser.add_argument("lines", nargs="*", help="G-code lines to send, then exit")
    parser.add_argument("--port", default="COM9")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--file", dest="gcode_file", help="send a .gcode / .nc file")
    args = parser.parse_args()

    try:
        with GrblController(port=args.port, baudrate=args.baud) as cnc:
            print(f"connected {args.port} @ {args.baud}")
            print(cnc.welcome)
            if args.gcode_file:
                for extra in cnc.send_file(args.gcode_file):
                    print(extra)
                print("ok")
            for line in args.lines:
                send_one(cnc, line)
            if not args.lines and not args.gcode_file:
                run_interactive(cnc)
    except GrblException as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"serial error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
