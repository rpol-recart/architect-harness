"""Human-readable graph projection; labels are always treated as data."""

import html


def mermaid(snapshot: dict) -> str:
    nodes = sorted(snapshot["nodes"], key=lambda n: n["id"])
    identifiers = {node["id"]: f"n{i}" for i, node in enumerate(nodes)}
    def label(value: str) -> str:
        return html.escape(str(value), quote=True).replace("\n", " ").replace("\r", " ")
    lines = ["flowchart TB"]
    for node in nodes:
        text = label(f'{node["title"]} ({node["kind"]})')
        lines.append(f'    {identifiers[node["id"]]}["{text}"]')
    for edge in sorted(snapshot["edges"], key=lambda e: e["id"]):
        source, target = identifiers[edge["source"]], identifiers[edge["target"]]
        lines.append(f'    {source} -->|"{label(edge["kind"])}"| {target}')
    return "\n".join(lines) + "\n"
