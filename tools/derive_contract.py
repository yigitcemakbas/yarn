"""Derive yarn's Finnhub contract from the recorded fixtures and REA's API surface.

REA's api-surface view (tests/contract/rea/api_surface.json) fixes the path
templates and observed query names. It deliberately does not infer value types
or required fields, so this script measures those from the response bodies:
key order, JSON types, nullability, item counts, sort order, status codes and
error bodies. The result, tests/contract/contract.json, is what yarn's
contract tests assert against.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "tests" / "contract"


def jtype(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def item_schema(items):
    orders = Counter(tuple(i.keys()) for i in items)
    fields = defaultdict(Counter)
    examples = {}
    for it in items:
        for k, v in it.items():
            fields[k][jtype(v)] += 1
            if v not in (None, "") and k not in examples:
                examples[k] = v
    return {
        "key_orders": [{"keys": list(k), "count": c} for k, c in orders.most_common()],
        "fields": {k: {"types": dict(c), "example": examples.get(k)} for k, c in fields.items()},
        "items_observed": len(items),
    }


def load(endpoint_dir):
    return [json.loads(p.read_text()) for p in sorted((ROOT / "fixtures" / endpoint_dir).glob("*.json"))]


def list_items(body, wrapper=None):
    if wrapper:
        return body.get(wrapper, []) if isinstance(body, dict) else []
    return body if isinstance(body, list) else []


def sort_check(items, key, reverse=True):
    vals = [i.get(key) for i in items if i.get(key) is not None]
    return vals == sorted(vals, reverse=reverse)


def main():
    surface = json.loads((ROOT / "rea" / "api_surface.json").read_text())
    contract = {"source": {"rea_api_surface_evidence_id": surface.get("evidence_id")}, "auth": {}, "endpoints": {}}

    spec = {
        "/api/v1/company-news": ("company-news", None, "datetime"),
        "/api/v1/news": ("news", None, "datetime"),
        "/api/v1/calendar/earnings": ("calendar-earnings", "earningsCalendar", None),
        "/api/v1/stock/recommendation": ("stock-recommendation", None, "period"),
    }

    def groups(o):
        if isinstance(o, dict):
            if "groups" in o:
                return o["groups"]
            for v in o.values():
                r = groups(v)
                if r:
                    return r
        return None

    rea_groups = {g["path_template"]: g for g in groups(surface)}
    for path, (d, wrapper, sort_key) in spec.items():
        fx = load(d)
        ok = [f for f in fx if f["status"] == 200 and not f["case"].startswith("burst_")]
        items = [i for f in ok for i in list_items(f["body"], wrapper)]
        per_response = [len(list_items(f["body"], wrapper)) for f in ok]
        errors = {f["case"]: {"status": f["status"], "body": f["body"]} for f in fx if f["status"] != 200}
        edge = {f["case"]: {"status": f["status"], "items": len(list_items(f["body"], wrapper)) if f["status"] == 200 else None}
                for f in fx if not f["case"].startswith(("loom_", "burst_"))}
        sorted_desc = all(sort_check(list_items(f["body"], wrapper), sort_key) for f in ok) if sort_key else None
        contract["endpoints"][path] = {
            "rea_query_parameter_names": [q["name"] for q in rea_groups[path].get("query_parameter_names", [])],
            "rea_record_ordinals": len(rea_groups[path].get("record_ordinals", [])),
            "body_wrapper": wrapper,
            "item_schema": item_schema(items),
            "items_per_response": {"min": min(per_response, default=0), "max": max(per_response, default=0)},
            "sorted_desc_by": sort_key if sorted_desc else None,
            "error_responses": errors,
            "edge_cases": edge,
            "content_type": Counter(f["headers"].get("content-type") for f in fx).most_common(1)[0][0],
        }

    any_fx = load("company-news")
    contract["auth"] = {
        "query_param": "token",
        "header": "X-Finnhub-Token",
        "header_works": next(f["status"] for f in any_fx if f["case"] == "header_token") == 200,
        "bad_token": next({"status": f["status"], "body": f["body"]} for f in any_fx if f["case"] == "bad_token"),
        "no_token": next({"status": f["status"], "body": f["body"]} for f in any_fx if f["case"] == "no_token"),
    }
    burst = [f for f in load("stock-recommendation") if f["case"].startswith("burst_")]
    limited = next((f for f in burst if f["status"] == 429), None)
    contract["rate_limit"] = {
        "headers": ["x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"],
        "limit_per_minute": int(burst[0]["headers"]["x-ratelimit-limit"]) if burst else None,
        "reset_is_epoch_seconds": True,
        "status_429": {"status": 429, "body": limited["body"]} if limited else None,
        "retry_after_header": bool(limited and "retry-after" in {k.lower() for k in limited["headers"]}),
    }
    (ROOT / "contract.json").write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(contract, indent=1, ensure_ascii=False)[:200])


if __name__ == "__main__":
    main()
