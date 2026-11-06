----------------------------- MODULE AgentMerge -----------------------------
(***************************************************************************)
(* How agents move main. Two agents each sell from a shop's stock and land *)
(* the sale through vc.agent_merge; TLC tries every order of their steps.  *)
(*                                                                         *)
(* Modeled from the SQL:                                                   *)
(*  - vc.agent_merge (sql/011_agent_boundary.sql): refuse a login with no  *)
(*    rows left (AB002) before main's lock; lock main's row; run           *)
(*    vc.merge_validated; charge the rows the merge commit records, and    *)
(*    refuse if fewer are left (AB002). It is one transaction, so a        *)
(*    refusal rolls everything back and releases the lock.                 *)
(*  - vc.merge_validated (sql/008_read_set_merge.sql): under the lock and  *)
(*    from the merge base, refuse (RS001) if a declared read changed on    *)
(*    main, or a row the branch wrote now holds the branch's own value.    *)
(*    Both checks are skipped when main hasn't moved since the base or     *)
(*    the branch has already landed.                                       *)
(*  - vc.merge (sql/001_core.sql): a branch that already landed is a       *)
(*    no-op. Of the rows the branch changed, skip those main holds with    *)
(*    the same value, refuse (P0001) if main changed one to another value, *)
(*    and apply the rest.                                                  *)
(*                                                                         *)
(* Left out: the gate and freezes (AB000, AB006), the bytes budget and     *)
(* max_table_share (AB003, AB007, which refuse and roll back like AB002),  *)
(* files, snapshots, and replaying history: each revision stores its rows. *)
(* docs/FORMAL_MODEL.md says what the results show and what they don't.    *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    ReadOnBranch,  \* TRUE: agents read on their branch, as documented. FALSE: an agent may also read main first, then fork.
    CheckWritten,  \* TRUE: merge_validated checks the rows the branch wrote (008). FALSE: only vc.merge's rule for them.
    SharedLogin,   \* TRUE: the two agents are sessions of one login, so they share its budget.
    Budgets,       \* budgets to issue (vc.agent_budget.main_rows_left); TLC checks each one
    MaxCalls       \* vc.agent_merge calls per branch; 2 adds a retry while the first may still run

ASSUME /\ ReadOnBranch \in BOOLEAN /\ CheckWritten \in BOOLEAN /\ SharedLogin \in BOOLEAN
       /\ Budgets \subseteq Nat /\ Budgets # {} /\ MaxCalls \in Nat \ {0}

Agents == {"a", "b"}
Warehouses == {"w1", "w2"}
Order(a) == "order_" \o a                         \* the agent's order row: the units it sold, 0 before
Keys == Warehouses \cup {Order(a) : a \in Agents}
LoginOf(a) == IF SharedLogin THEN "agent" ELSE a
Logins == {LoginOf(a) : a \in Agents}
Calls == Agents \X (1..MaxCalls)
NoCall == <<"none", 0>>                           \* no call holds the lock (a tuple, so TLC can compare it with calls)

Genesis == [k \in Keys |-> IF k \in Warehouses THEN 2 ELSE 0]  \* two units in each warehouse, no orders

(* An agent sells q units from warehouse x and records its order, under one of two rules:  *)
(* "take" if x has them; "keep" only if a unit is still in stock afterwards, a rule across *)
(* both warehouses, which is how write skew happens.                                       *)
Rules == {"take", "keep"}
Quantities == {1, 2}
Used(rule, x) == IF rule = "take" THEN {x} ELSE Warehouses        \* the rows the rule reads
Sells(d, v) == v[d.x] >= d.q /\ (d.rule = "take" \/ v["w1"] + v["w2"] - d.q >= 1)
\* What decision d of agent a writes, given rows v: row -> value, empty if it doesn't sell.
Writes(a, d, v) == IF Sells(d, v) THEN (d.x :> (v[d.x] - d.q)) @@ (Order(a) :> d.q) ELSE <<>>
Overlay(rows, w) == [k \in Keys |-> IF k \in DOMAIN w THEN w[k] ELSE rows[k]]

VARIABLES
    hist,    \* revisions, oldest first: [parents, rows, by]; hist[1] is genesis
    head,    \* main's head (vc.branch, name 'main')
    lock,    \* the call holding main's row lock, or NoCall
    issued,  \* the budget each login was issued
    left,    \* rows each login may still land on main (vc.agent_budget.main_rows_left), as committed
    serial,  \* main's rows if the sales that landed had run one after another, in the order they landed
    phase,   \* per agent: start, read (read main, no branch yet), forked, decided, committed, done
    dec,     \* per agent: rule, warehouse x, quantity q, declared reads, and the rows it saw
    base,    \* per agent: the revision its branch was forked from (0 before it forks)
    tip,     \* per agent: its branch head
    call     \* per call <<agent, i>>: pc, the head it read under the lock, result

vars == <<hist, head, lock, issued, left, serial, phase, dec, base, tip, call>>

Rows(r) == hist[r].rows

RECURSIVE Ancestors(_)
\* r and every revision behind it through all parent edges (vc._ancestors).
Ancestors(r) == {r} \cup UNION {Ancestors(hist[r].parents[i]) : i \in DOMAIN hist[r].parents}

\* vc.merge_base raises unless exactly one common ancestor is newest; the merge actions assert it.
Newest(x, y) == LET common == Ancestors(x) \cap Ancestors(y)
                IN  {c \in common : \A e \in common : e = c \/ c \notin Ancestors(e)}
MergeBase(x, y) == CHOOSE c \in Newest(x, y) : TRUE

Init ==
    /\ hist = <<[parents |-> <<>>, rows |-> Genesis, by |-> "genesis"]>>
    /\ head = 1
    /\ lock = NoCall
    /\ issued \in Budgets
    /\ left = [l \in Logins |-> issued]
    /\ serial = Genesis
    /\ phase = [a \in Agents |-> "start"]
    /\ dec = [a \in Agents |-> [rule |-> "none", x |-> "none", q |-> 0, reads |-> {}, saw |-> Genesis]]
    /\ base = [a \in Agents |-> 0]
    /\ tip = [a \in Agents |-> 0]
    /\ call = [c \in Calls |-> [pc |-> "idle", held |-> 0, result |-> "none"]]

(* The agent decides from the rows of revision r: a rule, a warehouse, a quantity, and the  *)
(* rows it declares. Declared reads plus the warehouse it writes cover every row the rule   *)
(* reads, as merge_validated asks (rows a branch wrote count as read without being named).  *)
Decide(a, r) ==
    \E rule \in Rules, x \in Warehouses, q \in Quantities, reads \in SUBSET Warehouses :
        /\ Used(rule, x) \subseteq reads \cup {x}
        /\ dec' = [dec EXCEPT ![a] = [rule |-> rule, x |-> x, q |-> q, reads |-> reads, saw |-> Rows(r)]]

\* SELECT on the app tables: main as it is now, before the agent has a branch.
ReadMain(a) ==
    /\ ~ReadOnBranch
    /\ phase[a] = "start"
    /\ Decide(a, head)
    /\ phase' = [phase EXCEPT ![a] = "read"]
    /\ UNCHANGED <<hist, head, lock, issued, left, serial, base, tip, call>>

\* vc.agent_fork(name, vc.head()): a branch from main's current head.
Fork(a) ==
    /\ phase[a] \in {"start", "read"}
    /\ base' = [base EXCEPT ![a] = head]
    /\ tip' = [tip EXCEPT ![a] = head]
    /\ phase' = [phase EXCEPT ![a] = IF @ = "read" THEN "decided" ELSE "forked"]
    /\ UNCHANGED <<hist, head, lock, issued, left, serial, dec, call>>

\* Reading on the branch shows the revision it was forked from.
ReadBranch(a) ==
    /\ phase[a] = "forked"
    /\ Decide(a, base[a])
    /\ phase' = [phase EXCEPT ![a] = "decided"]
    /\ UNCHANGED <<hist, head, lock, issued, left, serial, base, tip, call>>

(* One change on the branch (vc.agent_begin ... vc.finish_change), writing the values the   *)
(* agent worked out from what it saw. A value equal to the branch's own records nothing     *)
(* (vc._stage), so the change can be empty. An agent that doesn't sell writes nothing.      *)
Commit(a) ==
    /\ phase[a] = "decided"
    /\ LET w == Writes(a, dec[a], dec[a].saw) IN
         IF DOMAIN w = {}
         THEN /\ phase' = [phase EXCEPT ![a] = "done"]
              /\ UNCHANGED <<hist, tip>>
         ELSE /\ hist' = Append(hist, [parents |-> <<tip[a]>>, rows |-> Overlay(Rows(tip[a]), w), by |-> a])
              /\ tip' = [tip EXCEPT ![a] = Len(hist) + 1]
              /\ phase' = [phase EXCEPT ![a] = "committed"]
    /\ UNCHANGED <<head, lock, issued, left, serial, dec, base, call>>

(* vc.agent_merge before main's lock: a login with no rows left is refused (AB002).         *)
(* A later call is a retry, which may start while the earlier one still runs.               *)
Start(c) ==
    LET a == c[1] IN
    /\ phase[a] = "committed"
    /\ call[c].pc = "idle"
    /\ IF c[2] = 1 THEN TRUE ELSE call[<<a, c[2] - 1>>].pc # "idle"
    /\ call' = [call EXCEPT ![c] = IF left[LoginOf(a)] > 0 THEN [@ EXCEPT !.pc = "waiting"]
                                                           ELSE [@ EXCEPT !.pc = "done", !.result = "AB002"]]
    /\ UNCHANGED <<hist, head, lock, issued, left, serial, phase, dec, base, tip>>

\* SELECT head FROM vc.branch WHERE name='main' FOR UPDATE, which waits while another call holds it.
Lock(c) ==
    /\ call[c].pc = "waiting"
    /\ lock = NoCall
    /\ lock' = c
    /\ call' = [call EXCEPT ![c].pc = "locked", ![c].held = head]
    /\ UNCHANGED <<hist, head, issued, left, serial, phase, dec, base, tip>>

(* The rest of the call under the lock, ending in COMMIT or ROLLBACK, which releases it.    *)
Finish(c) ==
    LET a == c[1]
        h == call[c].held                                  \* main's head, read under the lock
        s == tip[a]                                        \* the branch being merged
        d == dec[a]
        mb == MergeBase(h, s)
        check == mb # h /\ mb # s                          \* merge_validated skips both checks otherwise
        changed == {k \in Keys : Rows(mb)[k] # Rows(s)[k]} \* the rows the branch wrote
        readConflict == {k \in d.reads : Rows(mb)[k] # Rows(h)[k]}
        writtenConflict == {k \in changed \ d.reads : Rows(h)[k] # Rows(mb)[k] /\ Rows(h)[k] = Rows(s)[k]}
        apply == {k \in changed : Rows(h)[k] # Rows(s)[k]} \* vc.merge skips rows main already holds
        clash == {k \in apply : Rows(h)[k] # Rows(mb)[k]}
        n == Cardinality(apply)                            \* rows the merge commit records
        result == CASE check /\ readConflict # {} -> "RS001"
                    [] check /\ CheckWritten /\ writtenConflict # {} -> "RS001"
                    [] s \in Ancestors(h) -> "merged"      \* landed already: vc.merge returns the head
                    [] clash # {} -> "P0001"
                    [] n > left[LoginOf(a)] -> "AB002"
                    [] OTHER -> "landed"
    IN
    /\ call[c].pc = "locked"
    /\ Assert(Cardinality(Newest(h, s)) = 1, "vc.merge_base would find no unique merge base")
    /\ lock' = NoCall
    /\ call' = [call EXCEPT ![c].pc = "done", ![c].result = result]
    /\ IF result = "landed"
       THEN /\ hist' = Append(hist, [parents |-> <<h, s>>,
                                     rows |-> Overlay(Rows(h), [k \in apply |-> Rows(s)[k]]), by |-> a])
            /\ head' = Len(hist) + 1
            /\ left' = [left EXCEPT ![LoginOf(a)] = @ - n]
            /\ serial' = Overlay(serial, Writes(a, d, serial))
       ELSE UNCHANGED <<hist, head, left, serial>>
    /\ UNCHANGED <<issued, phase, dec, base, tip>>

Done == \A a \in Agents : \/ phase[a] = "done"
                          \/ phase[a] = "committed" /\ \A i \in 1..MaxCalls : call[<<a, i>>].pc = "done"

Next ==
    \/ \E a \in Agents : ReadMain(a) \/ Fork(a) \/ ReadBranch(a) \/ Commit(a)
    \/ \E c \in Calls : Start(c) \/ Lock(c) \/ Finish(c)
    \/ Done /\ UNCHANGED vars      \* finished: stutter, so TLC reports only real deadlocks

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* What must hold in every state TLC reaches.                                *)

TypeOK ==
    /\ \A r \in DOMAIN hist : Rows(r) \in [Keys -> 0..2]
    /\ head \in DOMAIN hist
    /\ lock \in Calls \cup {NoCall}
    /\ left \in [Logins -> 0..issued]
    /\ serial \in [Keys -> 0..2]
    /\ phase \in [Agents -> {"start", "read", "forked", "decided", "committed", "done"}]
    /\ \A c \in Calls : call[c].pc \in {"idle", "waiting", "locked", "done"}

\* No sale is lost: main holds what the sales that landed would give, run one at a time in landing order.
MainIsSerial == Rows(head) = serial

\* No login lands more rows than it was issued, and the budget left says exactly what it landed.
RECURSIVE Total(_)
Total(S) == IF S = {} THEN 0 ELSE LET r == CHOOSE r \in S : TRUE IN
            Cardinality({k \in Keys : Rows(r)[k] # Rows(hist[r].parents[1])[k]}) + Total(S \ {r})
LandedBy(l) == Total({r \in DOMAIN hist : Len(hist[r].parents) = 2 /\ LoginOf(hist[r].by) = l})
WithinBudget == \A l \in Logins : LandedBy(l) <= issued /\ left[l] = issued - LandedBy(l)

\* A merge's checks look at the main it lands on: while a call holds the lock, main stays where it read it.
HeadStaysUnderLock == \A c \in Calls : call[c].pc = "locked" => call[c].held = head
=============================================================================
