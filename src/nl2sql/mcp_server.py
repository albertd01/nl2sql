"""MCP server exposing the agent's database tools, for chat UIs such as Open WebUI.

In this mode the chat UI runs the tool-calling loop, not nl2sql's Agent: the tools and the
system prompt (`nl2sql prompt --chat`) are the same as the evaluated agent, but the
self-check, forced final answer and structured final_answer tool are not available.

    uv run nl2sql mcp --db sqlite:///../m3_repro/db/mimic_iv.sqlite --port 8765
    # Streamable HTTP endpoint: http://127.0.0.1:8765/mcp
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .db import Database, build_schema_card
from .tools.toolbox import TOOL_SPECS, Toolbox


def build_server(db_url: str, name: str = "nl2sql") -> MCPServer:
    db = Database(db_url)
    schema = build_schema_card(db)
    toolbox = Toolbox(db, schema)
    descriptions = {spec["name"]: spec["description"] for spec in TOOL_SPECS}

    server = MCPServer(
        name=name,
        instructions=f"Read-only SQL tools for the {schema.dialect} database '{schema.database}'.",
    )

    def call(tool: str, **args) -> str:
        return toolbox.call(tool, args).text

    @server.tool(name="list_tables", description=descriptions["list_tables"])
    def list_tables() -> str:
        return call("list_tables")

    @server.tool(name="describe_table", description=descriptions["describe_table"])
    def describe_table(table: str) -> str:
        return call("describe_table", table=table)

    @server.tool(name="search_values", description=descriptions["search_values"])
    def search_values(table: str, column: str, term: str, limit: int = 20) -> str:
        return call("search_values", table=table, column=column, term=term, limit=limit)

    @server.tool(name="check_sql", description=descriptions["check_sql"])
    def check_sql(sql: str) -> str:
        return call("check_sql", sql=sql)

    @server.tool(name="run_query", description=descriptions["run_query"])
    def run_query(sql: str) -> str:
        return call("run_query", sql=sql)

    return server


def serve(db_url: str, host: str = "127.0.0.1", port: int = 8765, name: str = "nl2sql") -> None:
    server = build_server(db_url, name)
    print(f"nl2sql MCP server for {db_url} on http://{host}:{port}/mcp", flush=True)
    server.run(transport="streamable-http", host=host, port=port, stateless_http=True)
