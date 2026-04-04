"""Scrape /usage from Claude Code via Windows PTY."""
import sys, re, time, json, threading

sys.stdout.reconfigure(encoding='utf-8')


def scrape_usage():
    from winpty import PtyProcess

    print("Starting claude...", file=sys.stderr, flush=True)
    proc = PtyProcess.spawn('claude', dimensions=(45, 180))

    # Continuous reader in background thread
    output = []

    def reader():
        while proc.isalive():
            try:
                chunk = proc.read(4096)
                if chunk:
                    output.append(chunk)
            except EOFError:
                break
            except Exception:
                time.sleep(0.2)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Wait for claude to start
    time.sleep(6)
    print(f"Buffered {sum(len(c) for c in output)} chars", file=sys.stderr, flush=True)

    if not proc.isalive():
        return {"error": "claude exited", "raw": "".join(output)[:1000]}

    # Send /usage
    print("Sending /usage...", file=sys.stderr, flush=True)
    proc.write('/usage\r')
    time.sleep(8)

    print(f"Total buffered: {sum(len(c) for c in output)} chars", file=sys.stderr, flush=True)

    # Exit
    proc.write('\x1b')
    time.sleep(1)
    proc.write('/exit\r')
    time.sleep(2)
    try:
        proc.close(force=True)
    except Exception:
        pass
    t.join(timeout=2)

    raw = "".join(output)

    # Strip ANSI
    clean = re.sub(
        r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]'
        r'|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][AB012]|\x1b[>=<]',
        '', raw
    )

    # Parse
    result = {}
    for key, pattern in {
        "session": r"(?i)current\s+session",
        "week_all": r"(?i)(?:current\s+)?week\s*[\(:]?\s*all\s+models",
        "week_sonnet": r"(?i)(?:current\s+)?week\s*[\(:]?\s*sonnet",
    }.items():
        m = re.search(pattern, clean)
        if not m:
            continue
        block = clean[m.start():m.start() + 400]
        pct = re.search(r'(\d+)\s*%', block)
        reset = re.search(r'[Rr]esets?\s+(.+?)(?:\r|\n|$)', block)
        result[key] = {
            "pct": int(pct.group(1)) if pct else None,
            "resets": reset.group(1).strip() if reset else None,
        }

    result["raw"] = clean[:3000]
    return result


if __name__ == "__main__":
    try:
        data = scrape_usage()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
