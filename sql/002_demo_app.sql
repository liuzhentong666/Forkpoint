-- Example application. Run once, in the same NEW dedicated database.
BEGIN;
CREATE SCHEMA app;
CREATE TABLE app.products (
  id bigint PRIMARY KEY,
  sku text NOT NULL,
  name text NOT NULL,
  price numeric(12,2) NOT NULL CHECK(price>=0),
  stock integer NOT NULL CHECK(stock>=0),
  CONSTRAINT products_sku_unique UNIQUE(sku) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE app.orders (
  id bigint PRIMARY KEY,
  product_id bigint NOT NULL,
  quantity integer NOT NULL CHECK(quantity>0),
  CONSTRAINT orders_product_fk FOREIGN KEY(product_id) REFERENCES app.products(id)
    DEFERRABLE INITIALLY DEFERRED
);
SELECT vc.register_table('app.products');
SELECT vc.register_table('app.orders');

-- Roles are cluster-wide. Reuse an existing role without changing its attributes;
-- grant this project's database-local privileges only. Provision login roles separately.
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agentdb_client') THEN
    CREATE ROLE agentdb_client NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA vc,agent,app TO agentdb_client;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA app,agent TO agentdb_client;
GRANT EXECUTE ON FUNCTION vc.head(text), vc.table_id(regclass), vc.fork(text,uuid),
  vc.checkout(text), vc.pin(uuid), vc.begin_change(text,uuid,text,text), vc.finish_change(),
  vc.rows_at(uuid,uuid), vc.visible_rows(uuid), vc.write_file(text,bytea,boolean),
  vc.read_file(uuid,text), vc.materialize(uuid,uuid), vc.diff_rows(uuid,uuid,uuid),
  vc.diff_files(uuid,uuid), vc.history(uuid,integer),
  vc.merge_base(uuid,uuid), vc.merge(text,uuid,uuid,text) TO agentdb_client;
COMMIT;
