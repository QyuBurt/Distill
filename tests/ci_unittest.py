"""Standalone stdlib-only unittest completion checker (not a discovery module)."""
import argparse
import os
import re
import signal
import subprocess
import sys


COMPLETION = re.compile(
    r"(?m)^-{10,}\nRan (?P<count>\d+) tests? in \d+\.\d+s\n\n"
    r"OK(?: \((?P<details>(?:skipped=\d+|expected failures=\d+)"
    r"(?:, (?:skipped=\d+|expected failures=\d+))*)\))?\s*\Z"
)
DRAIN_TIMEOUT = 10


def successful(returncode, output):
    """Only the final unittest result block can certify completion."""
    match = COMPLETION.search(output.replace("\r\n", "\n"))
    if returncode != 0 or match is None:
        return False
    count = int(match["count"])
    skipped = re.search(r"(?:^|, )skipped=(\d+)(?:, |$)", match["details"] or "")
    return count >= 1 and count - (int(skipped[1]) if skipped else 0) >= 1


def run(command, timeout):
    """Capture and echo the child; a timeout kills its process tree too."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
    timed_out = False
    drain_timed_out = False
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.kill()
        try:
            output, _ = child.communicate(timeout=DRAIN_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            drain_timed_out = True
            output = exc.output or b""
            child.stdout.close()
            child.wait(timeout=DRAIN_TIMEOUT)
    text = output.decode("utf-8", errors="replace")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    if drain_timed_out:
        print(f"unittest checker: output drain timed out after {DRAIN_TIMEOUT}s; closed pipe", file=sys.stderr)
    if timed_out:
        print(f"unittest checker: timeout after {timeout}s; killed child tree", file=sys.stderr)
    elif not successful(child.returncode, text):
        print(f"unittest checker: invalid completion (child exit {child.returncode})", file=sys.stderr)
    return 0 if not timed_out and successful(child.returncode, text) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("a child command and positive timeout are required")
    return run(command, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
