"""Console entrypoint for the daily ``research`` client.

PX02-01 scaffolds the client package and the ``research`` console
script. The lifecycle commands (``init``, ``status``) and the
interactive shell land in PX02-03/PX02-04; until then the parser
accepts no subcommands and prints help.
"""

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research",
        description="Daily client for the researchd control daemon",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "entrypoint", "main"]
