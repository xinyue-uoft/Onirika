"""Entry point for onirika-ssh MCP server."""

import argparse

from onirika.server import mcp


def main():
    parser = argparse.ArgumentParser(description="Onirika SSH MCP Server")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config YAML (default: ~/.config/onirika/config.yaml)",
    )
    args = parser.parse_args()

    if args.config:
        import os
        os.environ["ONIRIKA_CONFIG"] = args.config

    mcp.run()


if __name__ == "__main__":
    main()
