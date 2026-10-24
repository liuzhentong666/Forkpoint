"""Stand-in for Claude Code headless, for dry runs of run_state_trials.py; no model is called.

It is fake_claude.py's stand-in (MCP server, stream-json, token estimate) with a script for
the restock task: it reads products 101 and 202 at the baseline, proposes 101's stock as
202's minus 5, and checkpoints that fact with the rows it was read from. When it resumes it
re-reads product 202 only if the packet marks that fact stale, so setup U lands the stale 95
and setup F the correct 55. That choice is scripted: the dry run shows the path works, not
how a model behaves. After a reset it continues from the stage instructions and the resume
packet the handoff carries.
"""
import json
from pathlib import Path
import sys
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "context_archive"))
import archive  # noqa: E402
from fake_claude import MARKERS, Mcp, StandIn  # noqa: E402

STATE_MARK = "\nYour saved task state, as resume returns it now:\n"
INSTRUCTIONS_MARK = "Your instructions for the current stage were:\n"


class StateStandIn(StandIn):
    def __init__(self, *args):
        super().__init__(*args)
        self.state.update(stock_202=None, marks={}, tag=uuid.uuid4().hex[:6])

    def learn_packet(self, packet):
        facts, s = packet.get("facts") or {}, self.state
        s["stock_202"] = facts.get("stock_202", s["stock_202"])
        s["proposal"] = facts.get("proposal", s["proposal"])
        s["base"] = facts.get("expected_main", s["base"])
        s["marks"] = packet.get("freshness") or {}

    def propose(self, base, branch):
        value = self.state["stock_202"] - 5
        self.call("fork", branch=branch, base=base)
        return self.call("propose", branch=branch, expected=base, field="stock", value=str(value),
                         code=f"STOCK = {value}\n")["result"]["revision"]

    def prepare(self):
        s, base = self.state, self.state["base"]
        self.once("info")
        s["stock_202"] = self.call("read-product", product="202", revision=base)["result"]["product"]["stock"]
        self.once("read-code", revision=base, path="experiments/inventory.py")
        s["proposal"] = self.propose(base, f"gpt-b-restock-{s['tag']}")
        self.once("diff", base=base, revision=s["proposal"])
        self.call("checkpoint", change={
            "goal": "Land product 101's stock as product 202's stock minus 5",
            "next_action": "merge after the pricing agent's stage",
            "facts": {"stock_202": {"value": s["stock_202"],
                                    "based_on": {"revision": base, "rows": {"app.products": [{"id": 202}]}}},
                      "proposal": {"value": s["proposal"]}, "expected_main": {"value": base}}})
        return f"Proposal revision {s['proposal']}; original expected main {base}."

    def price(self):
        """fake_claude's pricing script, restartable after a reset: its own branch per process, from current main."""
        info = self.call("info")["result"]
        if float(info["product"]["price"]) == 11:
            return "Price already merged."
        main, branch = info["current_main"], f"gpt-a-price-{self.state['tag']}"
        self.call("fork", branch=branch, base=main)
        proposal = self.call("propose", branch=branch, expected=main, field="price", value="11",
                             code="PRICE = 11\n")["result"]["revision"]
        self.once("diff", base=main, revision=proposal)
        self.call("merge", revision=proposal, expected=main)
        return f"Price proposal {proposal} merged."

    def recover_inventory(self):
        s = self.state
        self.learn_packet(self.call("resume").get("result") or {})
        self.call("merge", revision=s["proposal"], expected=s["base"])  # refused: main has moved
        main = self.call("info")["result"]["current_main"]
        if s["marks"].get("stock_202", {}).get("status") == "stale":
            s["stock_202"] = self.call("read-product", product="202", revision=main)["result"]["product"]["stock"]
        proposal = self.propose(main, f"gpt-b-restock-{s['tag']}-2")
        self.call("merge", revision=proposal, expected=main)
        return f"Stock proposal {proposal} merged."

    def recover(self, text):
        prompt, rest = text.split(INSTRUCTIONS_MARK, 1)[1].split(STATE_MARK, 1)
        saved = rest[:len(rest) - len(archive.CONTINUE) - 1]
        self.state["phase"] = next(phase for start, phase in MARKERS.items() if prompt.startswith(start))
        if self.state["phase"] in ("prepare", "price"):
            self.from_prompt(prompt)
        if saved != "(nothing saved yet)":
            self.learn_packet(json.loads(saved))


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("0.0.0 (task state stand-in, not Claude Code)")
        return
    option = {name: argv[argv.index(name) + 1] for name in ("--model", "--mcp-config", "--allowedTools")}
    fits = json.loads((archive.ROOT / "results" / "context_archive" / "long-task-check.json").read_text(encoding="utf-8"))
    token_model = fits["token_model"]["fits"][fits["token_model"]["chosen"]]
    mcp = Mcp(option["--mcp-config"])
    try:
        StateStandIn(option["--model"], option["--allowedTools"], mcp, token_model).serve()
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
