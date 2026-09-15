"""Build the demo page from recorded traces.

    uv run python demo/build_page.py   # demo/traces.json + demo/template.html -> demo/sql_agent_demo.html
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    traces = json.loads((HERE / "traces.json").read_text())
    # Safe inside <script type="application/json">: nothing can close the tag early.
    payload = json.dumps(traces, ensure_ascii=False).replace("</", "<\\/")
    html = (HERE / "template.html").read_text().replace("/*TRACES_JSON*/", payload)
    out = HERE / "sql_agent_demo.html"
    out.write_text(html)
    print(f"wrote {out} ({len(html) // 1024} KB, {len(traces['runs'])} runs)")


if __name__ == "__main__":
    main()
