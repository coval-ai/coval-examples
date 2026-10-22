"""Run the same Coval simulations against an existing agent and its Cloudflare replacement.

  python3 scripts/parity.py launch --manifest coval-agents.json [--only <org>] [--cases 6] [--seed 42]
  python3 scripts/parity.py report scripts/out/parity-<stamp>.json

Both runs in a pair use the same test set, persona, metrics and seeded test-case sample. Pairs are
manifest entries with both a source_agent_id and a cloudflare_agent_id; each may name a
template_id (run template to copy the test set, persona and metrics from) or a test_set_id.
COVAL_API_KEYS is a JSON map of org -> Coval API key.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.coval.dev/v1"
OUT = ROOT / "scripts" / "out"
TRUE_WORDS = {"YES", "TRUE", "PASS", "MET", "SUCCESS"}
FALSE_WORDS = {"NO", "FALSE", "FAIL", "NOT_MET", "FAILURE"}


def coval_key(org: str) -> str:
    return json.loads(os.environ["COVAL_API_KEYS"])[org]


def call(
    key: str, method: str, path: str, body: dict | None = None, params: dict | None = None
) -> dict:
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            return json.load(urllib.request.urlopen(request, timeout=90))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 2:
                raise RuntimeError(
                    f"{method} {path} -> {exc.code} {exc.read()[:400].decode(errors='replace')}"
                ) from None
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def run_config(key: str, twin: dict) -> dict:
    source = call(key, "GET", f"/agents/{twin['source_agent_id']}")
    source = source.get("agent", source)
    if twin.get("template_id"):
        template = call(key, "GET", f"/run-templates/{twin['template_id']}")["run_template"]
        return {
            "test_set_id": template["test_set_ids"][0],
            "persona_id": template["persona_ids"][0],
            "metric_ids": template["metric_ids"] or source.get("metric_ids") or [],
        }
    recent = call(
        key,
        "GET",
        "/runs",
        params={"page_size": 5, "filter": f'agent_id="{twin["source_agent_id"]}"'},
    )
    runs = [r for r in recent.get("runs") or [] if r.get("persona_id")]
    if not runs:
        raise RuntimeError(f"no recent run to borrow a persona from for {twin['source_agent_id']}")
    return {
        "test_set_id": twin.get("test_set_id") or runs[0]["test_set_id"],
        "persona_id": runs[0]["persona_id"],
        "metric_ids": source.get("metric_ids") or [],
    }


def launch(args: argparse.Namespace) -> None:
    manifest = json.loads(args.manifest.read_text())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pairs = []
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"parity-{stamp}.json"

    # Saved after every pair, so a later failure never loses runs that already launched.
    def save() -> None:
        summary = {"stamp": stamp, "cases": args.cases, "seed": args.seed, "pairs": pairs}
        path.write_text(json.dumps(summary, indent=2))

    for twin in manifest["twins"]:
        if not (twin.get("cloudflare_agent_id") and twin.get("source_agent_id")):
            continue
        if args.only and twin["org"] != args.only:
            continue
        key = coval_key(twin["org"])
        try:
            config = run_config(key, twin)
        except RuntimeError as exc:
            print(f"{twin['org']:18} {twin['slug']:15} skipped: {exc}")
            continue
        pair = {**twin, **config, "runs": {}}
        for label, agent_id in (
            ("source", twin["source_agent_id"]),
            ("cloudflare", twin["cloudflare_agent_id"]),
        ):
            body = {
                "agent_id": agent_id,
                "persona_id": config["persona_id"],
                "test_set_id": config["test_set_id"],
                "options": {
                    "sub_sample_size": args.cases,
                    "sub_sample_seed": args.seed,
                    "concurrency": 3,
                },
                "metadata": {
                    "display_name": f"Cloudflare parity {stamp} - {twin['slug']} - {label}",
                    "tags": ["cloudflare-parity", f"parity:{stamp}", f"parity-side:{label}"],
                },
            }
            # An empty list is rejected; omitting it applies the agent's default metrics.
            if config["metric_ids"]:
                body["metric_ids"] = config["metric_ids"]
            try:
                run = call(key, "POST", "/runs", body)["run"]
            except RuntimeError as exc:
                print(f"{twin['org']:18} {twin['slug']:15} {label:10} failed: {exc}")
                continue
            pair["runs"][label] = run["run_id"]
            print(f"{twin['org']:18} {twin['slug']:15} {label:10} run {run['run_id']}")
        if pair["runs"]:
            pairs.append(pair)
            save()
    print(f"saved {path}")


def fmt(value: float | None) -> str:
    return f"{value:9.2f}" if value is not None else "        -"


def numeric(value) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        word = value.strip().upper()
        if word in TRUE_WORDS:
            return 1.0
        if word in FALSE_WORDS:
            return 0.0
        try:
            return float(word)
        except ValueError:
            return None
    return None


def collect(key: str, run_id: str) -> dict:
    run = call(key, "GET", f"/runs/{run_id}")
    run = run.get("run", run)
    sims, token = [], None
    while True:
        params = {"page_size": 50, "filter": f'run_id="{run_id}"'}
        if token:
            params["page_token"] = token
        page = call(key, "GET", "/simulations", params=params)
        sims += page.get("simulations") or []
        token = page.get("next_page_token")
        if not token:
            break
    values: dict[str, list[float]] = defaultdict(list)
    for sim in sims:
        if sim.get("status") != "COMPLETED":
            continue
        for metric in (
            call(key, "GET", f"/simulations/{sim['simulation_id']}/metrics").get("metrics") or []
        ):
            if metric.get("status") != "COMPLETED":
                continue
            number = numeric(metric.get("value"))
            if number is not None:
                values[metric["metric_id"]].append(number)
    statuses = defaultdict(int)
    for sim in sims:
        statuses[sim.get("status")] += 1
    return {"status": run.get("status"), "simulations": dict(statuses), "values": values}


def report(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.file).read_text())
    summary = []
    for pair in data["pairs"]:
        key = coval_key(pair["org"])
        names = {}
        token = None
        while True:
            params = {"page_size": 100, **({"page_token": token} if token else {})}
            page = call(key, "GET", "/metrics", params=params)
            names.update({m["id"]: m["metric_name"] for m in page.get("metrics") or []})
            token = page.get("next_page_token")
            if not token:
                break
        sides = {label: collect(key, run_id) for label, run_id in pair["runs"].items()}
        for metric_id in set(sides["source"]["values"]) | set(sides["cloudflare"]["values"]):
            if metric_id not in names:
                # Built-in metrics are not in the org's metric list.
                try:
                    metric = call(key, "GET", f"/metrics/{metric_id}")
                    names[metric_id] = metric.get("metric", metric).get("metric_name", metric_id)
                except RuntimeError:
                    names[metric_id] = metric_id
        print(f"\n## {pair['org']} / {pair['slug']}")
        for label, side in sides.items():
            print(
                f"  {label:10} run {pair['runs'][label]} {side['status']} sims={side['simulations']}"
            )
        print(f"  {'metric':52} {'source':>9} {'cloudflare':>11}  n")
        rows = []
        for metric_id in sorted(
            set(sides["source"]["values"]) | set(sides["cloudflare"]["values"]),
            key=lambda m: names.get(m, m),
        ):
            a, b = (
                sides["source"]["values"].get(metric_id, []),
                sides["cloudflare"]["values"].get(metric_id, []),
            )
            mean_a = sum(a) / len(a) if a else None
            mean_b = sum(b) / len(b) if b else None
            rows.append(
                {
                    "metric": names.get(metric_id, metric_id),
                    "source": mean_a,
                    "cloudflare": mean_b,
                    "n": [len(a), len(b)],
                }
            )
            print(
                f"  {names.get(metric_id, metric_id)[:52]:52} {fmt(mean_a)} {fmt(mean_b):>11}  {len(a)}/{len(b)}"
            )
        summary.append(
            {
                "org": pair["org"],
                "slug": pair["slug"],
                "runs": pair["runs"],
                "sides": {
                    k: {"status": v["status"], "simulations": v["simulations"]}
                    for k, v in sides.items()
                },
                "metrics": rows,
            }
        )
    out = Path(args.file).with_suffix(".report.json")
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nsaved {out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--manifest", type=Path, default=Path("coval-agents.json"))
    launch_parser.add_argument("--only")
    launch_parser.add_argument("--cases", type=int, default=6)
    launch_parser.add_argument("--seed", type=int, default=42)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("file")
    args = parser.parse_args()
    launch(args) if args.command == "launch" else report(args)


if __name__ == "__main__":
    main()
