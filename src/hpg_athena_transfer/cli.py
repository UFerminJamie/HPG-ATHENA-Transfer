"""Command-line entry point."""

import argparse
import os
from pathlib import Path
import sys

from .api import APIError, AthenaClient
from .transfer import transfer


DEFAULT_SOURCE = Path("/orange/pinaki.sarder/j.fermin/cort_med_wsi/trident_processed_final/KPMP/inference_jsons/svc_2_rbf_1.0/")


def _api_key() -> str:
    if os.environ.get("ATHENA_API_KEY"):
        return os.environ["ATHENA_API_KEY"]
    env_file = Path.cwd() / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "ATHENA_API_KEY":
                return value.strip().strip('"\'')
    return ""


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="hpg-athena-transfer",
        description="Attach native DSA JSON annotations to exactly matching Athena WSI items. Dry-run is the default.",
    )
    command.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                         help="JSON source directory on HiPerGator (default: configured KPMP inference directory)")
    command.add_argument("--destination", help="Athena collection/folder URL or 24-character ID")
    command.add_argument("--api-url", default="https://athena.rc.ufl.edu/api/v1",
                         help="Girder API base URL (default: %(default)s)")
    command.add_argument("--recursive", action="store_true", help="Search source subdirectories; default is top level only")
    command.add_argument("--normalize-rgba-alpha", action="store_true",
                         help="Explicitly rewrite exponent-form rgba fillColor alpha to equal plain decimals; record count in manifest")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (this is the default)")
    mode.add_argument("--apply", action="store_true", help="Create annotations after matching and validation")
    command.add_argument("--check-connection", action="store_true",
                         help="Check Athena authentication and optional destination, then exit")
    command.add_argument("--manifest", type=Path, default=Path("transfer-manifest.json"),
                         help="Machine-readable report path (default: %(default)s)")
    return command


def main() -> int:
    args = parser().parse_args()
    if not args.destination and not args.check_connection:
        parser().error("--destination is required unless --check-connection is used")
    key = _api_key()
    if not key:
        print("ATHENA_API_KEY is missing from the environment or ignored .env file", file=sys.stderr)
        return 2
    try:
        client = AthenaClient(args.api_url, key)
        user = client.check_connection()
        if args.check_connection:
            if args.destination:
                resource_type, object_id = client.resolve_destination(args.destination)
                print("Connected to Athena; destination: %s %s" % (resource_type, object_id))
            else:
                print("Connected to Athena")
            return 0
        result = transfer(client, args.source, args.destination, args.manifest,
                          recursive=args.recursive, apply=args.apply,
                          normalize_rgba_alpha=args.normalize_rgba_alpha)
        print("Athena user: %s; slides: %d; source files: %d; annotation records: %d" %
              (user.get("login") or user["id"], result["slides"], result["files"], result["records"]))
        print("Duplicate slide basenames: %d" % result["duplicate_slide_basenames"])
        for status in ("uploaded", "skipped-identical", "planned", "unmatched", "ambiguous", "invalid", "failed"):
            print("%s: %d files; %d annotation records" %
                  (status, result["file_summary"].get(status, 0), result["summary"].get(status, 0)))
        print("Manifest: " + result["manifest"])
        return 1 if result["summary"].get("failed") or result["summary"].get("invalid") or result["summary"].get("ambiguous") else 0
    except (APIError, ValueError, OSError) as exc:
        # APIError messages never contain request URLs, headers, response bodies,
        # tokens or keys. Filesystem errors are reduced to their class name.
        message = str(exc) if isinstance(exc, (APIError, ValueError)) else type(exc).__name__
        print("Error: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
