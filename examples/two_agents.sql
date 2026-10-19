-- Two agents change the same product on their own branches.
-- Run with psql 12+ against a database made by `python db.py install`:
--   psql -d agentdb_demo -f examples/two_agents.sql
-- or with Docker, from the project root: docker compose up
--
-- Two merges below fail on purpose; psql prints the error and goes on.
-- `\gset` stores a result in a psql variable instead of printing it.
-- The last step checks the outcome, so a wrong result exits non-zero.

\set QUIET on
\set SHOW_CONTEXT never
\pset pager off

\echo '1. main: product 101 costs 10 and has 100 in stock'
BEGIN;
SELECT vc.begin_change('main', vc.head(), 'setup', 'add product 101') \gset
INSERT INTO app.products VALUES (101, 'sku-101', 'Widget', 10, 100);
SELECT vc.finish_change() AS base \gset
COMMIT;

\echo '2. price-agent and stock-agent each fork a branch from that version'
SELECT vc.fork('price-agent', :'base') \gset
SELECT vc.fork('stock-agent', :'base') \gset

\echo '3. price-agent: price 11 and src/pricing.py, in one commit'
BEGIN;
SELECT vc.begin_change('price-agent', :'base', 'price-agent', 'raise price to 11') \gset
UPDATE agent.products SET price = 11 WHERE id = 101;
SELECT vc.write_file('src/pricing.py', convert_to('PRICE = 11', 'UTF8')) \gset
SELECT vc.finish_change() AS price_rev \gset
COMMIT;

\echo '4. stock-agent: stock 95, on a branch that never sees the price change'
BEGIN;
SELECT vc.begin_change('stock-agent', :'base', 'stock-agent', 'cut stock to 95') \gset
UPDATE agent.products SET stock = 95 WHERE id = 101;
SELECT vc.finish_change() AS stock_rev \gset
COMMIT;

\echo '5. price-agent merges into main'
SELECT vc.merge('main', :'price_rev', :'base', 'price-agent') \gset

\echo '6. stock-agent merges, still expecting main at the old version (should fail: stale head)'
SELECT vc.merge('main', :'stock_rev', :'base', 'stock-agent') \gset
\set stale_error :LAST_ERROR_SQLSTATE
\echo '   SQLSTATE' :stale_error '-> re-read main and retry'

\echo '7. stock-agent retries against the latest main (should fail: its row 101 still has price 10)'
SELECT vc.merge('main', :'stock_rev', vc.head(), 'stock-agent') \gset
\set conflict_error :LAST_ERROR_SQLSTATE
\echo '   SQLSTATE' :conflict_error '-> re-propose from the latest main'

\echo '8. stock-agent re-proposes from the latest main and merges'
SELECT vc.head() AS latest \gset
SELECT vc.fork('stock-agent-2', :'latest') \gset
BEGIN;
SELECT vc.begin_change('stock-agent-2', :'latest', 'stock-agent', 'cut stock to 95, from latest main') \gset
UPDATE agent.products SET stock = 95 WHERE id = 101;
SELECT vc.finish_change() AS stock_rev2 \gset
COMMIT;
SELECT vc.merge('main', :'stock_rev2', :'latest', 'stock-agent') \gset

\echo '9. main now has both changes, and the price change brought its code'
SELECT id, price, stock FROM app.products WHERE id = 101;
SELECT convert_from(vc.read_file(vc.head(), 'src/pricing.py'), 'UTF8') AS pricing_code;

-- Check the outcome. From here on, any error stops psql with a non-zero exit code.
\set ON_ERROR_STOP on
SELECT set_config('demo.errors', :'stale_error' || ' ' || :'conflict_error', false) \gset
DO $$
BEGIN
  IF current_setting('demo.errors') <> '40001 P0001'
     OR NOT EXISTS (SELECT 1 FROM app.products WHERE id = 101 AND price = 11 AND stock = 95)
     OR convert_from(vc.read_file(vc.head(), 'src/pricing.py'), 'UTF8') <> 'PRICE = 11' THEN
    RAISE EXCEPTION 'two_agents.sql: unexpected result (merge errors: %)', current_setting('demo.errors');
  END IF;
END $$;
\echo 'OK: both conflicts were caught, and no agent overwrote the other.'
