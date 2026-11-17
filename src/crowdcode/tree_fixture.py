"""Store a deliberately worse capability-tree version, to test W8's gate on
production (contract C7, "Publishing").

    python -m crowdcode.tree_fixture buy_first            # degrade the current version
    python -m crowdcode.tree_fixture buy_first --base 6

The copy is the newest version, so `?version=candidate` serves it until the
next build; its trigger is `fixture:<variant>:v<base>`, the gate's planner
skips it unless an operator names it, and `publish_gated` never serves it as
`current`. It is pruned with the other old versions.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from crowdcode import capability_tree
from crowdcode.db import connect


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m crowdcode.tree_fixture")
    parser.add_argument("variant", choices=capability_tree.FIXTURE_VARIANTS)
    parser.add_argument("--base", type=int, help="version to degrade (default: the current published one)")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    with connect() as conn:
        base = args.base
        if base is None:
            current = capability_tree.latest(conn, published_only=True)
            if current is None:
                raise SystemExit("no published capability tree to degrade")
            base = current["version"]
        version = capability_tree.store_fixture(conn, base, args.variant, now=now)
        conn.commit()
    print(json.dumps({"stored": version, "variant": args.variant, "base": base}))


if __name__ == "__main__":
    main()
