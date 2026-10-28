"""Stand-in for Claude Code headless, for dry runs of run_readset_agents.py; no model is called.

It is fake_claude.py's stand-in (MCP server, stream-json) with a script for the bundle task. It
reads products 101 and 202 and sells its product if the bundle keeps 180. Guarding (the default),
it locks both rows and checks the rule again in its P execute, or lists both rows in its F merge's
reads and, refused, reads main again before it decides; with READSET_STAND_IN_GUARDS=0 in its
environment it writes its product without that check, or lists only that product. Two guarding
stand-ins land one sale and two unguarded ones both: the dry run shows the path works, not how a
model behaves.
"""
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "context_archive"))
import archive  # noqa: E402
from fake_claude import Interrupted, Mcp, StandIn  # noqa: E402
import long_task  # noqa: E402

GUARDS = os.environ.get("READSET_STAND_IN_GUARDS", "1") != "0"
BUNDLE = "SELECT id, stock FROM products WHERE id IN (101, 202) ORDER BY id;"
ROW = re.compile(r"^\s*(101|202)\s*\|\s*(\d+)\s*$", re.M)  # a row of psql's aligned output
SALE, FLOOR = 15, 180


class ReadsetStandIn(StandIn):
    def stocks(self, **where):
        return {int(product): int(stock) for product, stock in ROW.findall(self.call("query", sql=BUNDLE, **where)["output"])}

    def sell_p(self, product):
        stocks = self.stocks()
        if sum(stocks.values()) - SALE < FLOOR:
            return f"Did not sell product {product}: the bundle has {stocks}."
        if GUARDS:
            sql = (BUNDLE[:-1] + " FOR UPDATE;\n"
                   f"UPDATE products SET stock = stock - {SALE} WHERE id = {product} "
                   f"AND (SELECT sum(stock) FROM products WHERE id IN (101, 202)) - {SALE} >= {FLOOR};")
        else:
            sql = f"UPDATE products SET stock = stock - {SALE} WHERE id = {product};"
        sold = "UPDATE 1" in self.call("execute", sql=sql).get("output", "")
        return f"{'Sold' if sold else 'Did not sell'} {SALE} units of product {product}; my decision used {stocks}."

    def sell_f(self, product):
        for _ in range(3):
            stocks = self.stocks(main=True)
            if sum(stocks.values()) - SALE < FLOOR:
                return f"Did not sell product {product}: the bundle has {stocks} on main."
            self.call("fork")
            self.call("execute", sql=f"UPDATE products SET stock = stock - {SALE} WHERE id = {product};")
            rows = [{"id": 101}, {"id": 202}] if GUARDS else [{"id": product}]
            error = self.call("merge", reads={"products": rows}).get("error")
            if error is None:
                return f"Sold {SALE} units of product {product}; my decision used {stocks}."
            if error != "read_conflict":
                return f"Did not sell product {product}: the merge failed with {error}."
        return f"Did not sell product {product}: every merge was refused."

    def turn(self, text):
        self.context += long_task.context_bytes({"type": "text", "text": text}, False)
        self.interrupted = False
        try:
            product = int(re.search(r"A customer wants 15 units of product (\d+)\.", text).group(1))
            reply = (self.sell_p if "in the same execute call" in text else self.sell_f)(product)
            self.respond({"type": "text", "text": reply})
            self.finish("success", reply)
        except Interrupted:
            self.finish("error_during_execution", "Interrupted", error=True)


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("0.0.0 (read-set agents stand-in, not Claude Code)")
        return
    option = {name: argv[argv.index(name) + 1] for name in ("--model", "--mcp-config", "--allowedTools")}
    fits = json.loads((archive.ROOT / "results" / "context_archive" / "long-task-check.json").read_text(encoding="utf-8"))
    token_model = fits["token_model"]["fits"][fits["token_model"]["chosen"]]
    mcp = Mcp(option["--mcp-config"])
    try:
        ReadsetStandIn(option["--model"], option["--allowedTools"], mcp, token_model).serve()
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
