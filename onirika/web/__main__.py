"""Entry point for onirika-web launcher."""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Onirika Web Launcher")
    parser.add_argument("host", nargs="?", default=None, help="Host alias from config")
    parser.add_argument("--port", type=int, default=8765, help="Web server port (default: 8765)")
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Configure the app before uvicorn starts it
    import onirika.web.app as web_app
    web_app._host_alias = args.host
    web_app._auto_open = not args.no_open
    web_app.app.state.port = args.port

    print(f"Onirika Web — http://{args.bind}:{args.port}")

    uvicorn.run(
        web_app.app,
        host=args.bind,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
