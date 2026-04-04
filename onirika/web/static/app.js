/**
 * Onirika Web — terminal panel setup with xterm.js + WebSocket.
 */

(function () {
    "use strict";

    const THEME = {
        background: "#0d1117",
        foreground: "#e6edf3",
        cursor: "#e6edf3",
        cursorAccent: "#0d1117",
        selectionBackground: "#264f78",
        black: "#484f58",
        red: "#ff7b72",
        green: "#3fb950",
        yellow: "#d29922",
        blue: "#58a6ff",
        magenta: "#bc8cff",
        cyan: "#39d353",
        white: "#b1bac4",
        brightBlack: "#6e7681",
        brightRed: "#ffa198",
        brightGreen: "#56d364",
        brightYellow: "#e3b341",
        brightBlue: "#79c0ff",
        brightMagenta: "#d2a8ff",
        brightCyan: "#56d364",
        brightWhite: "#f0f6fc",
    };

    /**
     * Create a terminal instance connected to a backend PTY via WebSocket.
     */
    function createTerminal(containerId, sessionId, statusId) {
        const container = document.getElementById(containerId);
        const statusEl = document.getElementById(statusId);

        // Create terminal
        const term = new Terminal({
            cursorBlink: true,
            fontSize: 13,
            fontFamily: '"SF Mono", "Menlo", "Monaco", "Courier New", monospace',
            theme: THEME,
            allowProposedApi: true,
        });

        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);

        const webLinksAddon = new WebLinksAddon.WebLinksAddon();
        term.loadAddon(webLinksAddon);

        term.open(container);

        // Initial fit after a brief delay for layout
        setTimeout(() => fitAddon.fit(), 50);

        // WebSocket connection
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${proto}//${location.host}/ws/terminal/${sessionId}`;
        let ws = null;
        let reconnectTimer = null;

        function connect() {
            ws = new WebSocket(wsUrl);
            ws.binaryType = "arraybuffer";

            ws.onopen = () => {
                statusEl.textContent = `${sessionId}: connected`;
                statusEl.classList.add("connected");
                statusEl.classList.remove("disconnected");
                // Send initial size
                sendResize();
            };

            ws.onmessage = (event) => {
                if (typeof event.data === "string") {
                    // JSON control message
                    try {
                        const msg = JSON.parse(event.data);
                        if (msg.type === "exit") {
                            term.writeln(`\r\n\x1b[33m[${msg.message}]\x1b[0m`);
                            statusEl.textContent = `${sessionId}: exited`;
                            statusEl.classList.remove("connected");
                            statusEl.classList.add("disconnected");
                        }
                    } catch (e) {
                        // Not JSON, write as text
                        term.write(event.data);
                    }
                } else {
                    term.write(new Uint8Array(event.data));
                }
            };

            ws.onclose = () => {
                statusEl.textContent = `${sessionId}: disconnected`;
                statusEl.classList.remove("connected");
                statusEl.classList.add("disconnected");
            };

            ws.onerror = () => {
                statusEl.textContent = `${sessionId}: error`;
                statusEl.classList.remove("connected");
                statusEl.classList.add("disconnected");
            };
        }

        // Terminal input -> WebSocket -> PTY
        term.onData((data) => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(new TextEncoder().encode(data));
            }
        });

        // Resize handling
        function sendResize() {
            fitAddon.fit();
            if (term.cols && term.rows) {
                fetch(`/api/terminal/${sessionId}/resize`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ cols: term.cols, rows: term.rows }),
                }).catch(() => {});
            }
        }

        // Watch container size changes
        const resizeObserver = new ResizeObserver(() => {
            sendResize();
        });
        resizeObserver.observe(container);

        connect();

        return { term, ws, fitAddon, sendResize };
    }

    // ── Resizer drag logic ──────────────────────────────────────────────────

    function setupResizer() {
        const resizer = document.getElementById("resizer");
        const panels = document.getElementById("panels");
        const panelLeft = document.getElementById("panel-ssh");
        let dragging = false;

        resizer.addEventListener("mousedown", (e) => {
            dragging = true;
            resizer.classList.add("dragging");
            e.preventDefault();
        });

        document.addEventListener("mousemove", (e) => {
            if (!dragging) return;
            const rect = panels.getBoundingClientRect();
            const pct = ((e.clientX - rect.left) / rect.width) * 100;
            const clamped = Math.max(15, Math.min(85, pct));
            panelLeft.style.flex = `0 0 ${clamped}%`;
        });

        document.addEventListener("mouseup", () => {
            if (dragging) {
                dragging = false;
                resizer.classList.remove("dragging");
                // Trigger resize on both terminals
                if (window._sshTerm) window._sshTerm.sendResize();
                if (window._claudeTerm) window._claudeTerm.sendResize();
            }
        });
    }

    // ── Init ────────────────────────────────────────────────────────────────

    window.addEventListener("DOMContentLoaded", () => {
        window._sshTerm = createTerminal("term-ssh", "ssh", "ssh-status");
        window._claudeTerm = createTerminal("term-claude", "claude", "claude-status");
        setupResizer();
    });
})();
