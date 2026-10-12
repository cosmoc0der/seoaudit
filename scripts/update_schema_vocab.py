"""Rebuilds the bundled schema.org vocabulary: python scripts/update_schema_vocab.py"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

SOURCE = "https://schema.org/version/latest/schemaorg-current-https.jsonld"
TARGET = Path(__file__).parents[1] / "src/seoaudit/checks/structured_data/vocab.json"


def _ids(value) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    return sorted(v["@id"].removeprefix("schema:") for v in values if v["@id"].startswith("schema:"))


def build(graph: list[dict]) -> dict:
    types, properties, members = {}, {}, {}
    for node in graph:
        name = node["@id"].removeprefix("schema:")
        kinds = node["@type"] if isinstance(node["@type"], list) else [node["@type"]]
        if "rdf:Property" in kinds:
            properties[name] = [_ids(node.get("schema:domainIncludes")), _ids(node.get("schema:rangeIncludes"))]
            continue
        if "rdfs:Class" in kinds:
            # Data types have no superclass in the source; DataType as a parent lets ranges tell literals from things.
            types[name] = _ids(node.get("rdfs:subClassOf")) or (["DataType"] if "schema:DataType" in kinds else [])
        enums = [k.removeprefix("schema:") for k in kinds if k.startswith("schema:") and k != "schema:DataType"]
        if enums:
            members[name] = enums
    return {"source": SOURCE, "types": types, "properties": properties, "members": members}


def main() -> None:
    graph = httpx.get(SOURCE, follow_redirects=True, timeout=60).raise_for_status().json()["@graph"]
    vocab = build(graph)
    TARGET.write_text(json.dumps(vocab, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(f"{len(vocab['types'])} types, {len(vocab['properties'])} properties, {len(vocab['members'])} members -> {TARGET}")


if __name__ == "__main__":
    main()
