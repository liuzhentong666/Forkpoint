-- Two agents change the same product on their own branches.
-- Run with psql against a database made by `python db.py install`:
--   psql -d agentdb -f examples/two_agents.sql
-- Two of the merges below fail on purpose; psql prints the error and goes on.

-- main: product 101 costs 10 and has 100 in stock.
BEGIN;
SELECT vc.begin_change('main', vc.head(), 'setup', 'add product 101');
INSERT INTO app.products VALUES (101, 'sku-101', 'Widget', 10, 100);
SELECT vc.finish_change() AS base \gset
COMMIT;

SELECT vc.fork('price-agent', :'base');
SELECT vc.fork('stock-agent', :'base');

-- Price agent: new price and the pricing code, in one commit.
BEGIN;
SELECT vc.begin_change('price-agent', :'base', 'price-agent', 'raise price to 11');
UPDATE agent.products SET price = 11 WHERE id = 101;
SELECT vc.write_file('src/pricing.py', convert_to('PRICE = 11', 'UTF8'));
SELECT vc.finish_change() AS price_rev \gset
COMMIT;

-- Stock agent: works from the same base and never sees the price change.
BEGIN;
SELECT vc.begin_change('stock-agent', :'base', 'stock-agent', 'cut stock to 95');
UPDATE agent.products SET stock = 95 WHERE id = 101;
SELECT vc.finish_change() AS stock_rev \gset
COMMIT;

-- The price agent merges first.
SELECT vc.merge('main', :'price_rev', :'base', 'price-agent');

-- The stock agent still thinks main is at base: rejected with 40001 (stale head).
SELECT vc.merge('main', :'stock_rev', :'base', 'stock-agent');

-- Re-reading main is not enough: its row 101 still has price 10, so P0001 (merge conflict).
SELECT vc.merge('main', :'stock_rev', vc.head(), 'stock-agent');

-- Re-propose the stock change from the latest main, then merge.
SELECT vc.head() AS latest \gset
SELECT vc.fork('stock-agent-2', :'latest');
BEGIN;
SELECT vc.begin_change('stock-agent-2', :'latest', 'stock-agent', 'cut stock to 95, from latest main');
UPDATE agent.products SET stock = 95 WHERE id = 101;
SELECT vc.finish_change() AS stock_rev2 \gset
COMMIT;
SELECT vc.merge('main', :'stock_rev2', :'latest', 'stock-agent');

-- Both changes survive, and the code went in with the price.
SELECT id, price, stock FROM app.products WHERE id = 101;
SELECT convert_from(vc.read_file(vc.head(), 'src/pricing.py'), 'UTF8') AS pricing_code;
