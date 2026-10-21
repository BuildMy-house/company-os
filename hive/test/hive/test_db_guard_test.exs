Code.require_file("../../config/test_db_guard.exs", __DIR__)

defmodule Hive.TestDbGuardTest do
  use ExUnit.Case, async: true

  alias Hive.TestDbGuard

  test "allows in-memory mode and dedicated test databases" do
    assert :ok = TestDbGuard.check(nil)
    assert :ok = TestDbGuard.check("")
    assert :ok = TestDbGuard.check("postgresql://u:p@localhost:5432/hive_test")
    assert :ok = TestDbGuard.check("postgresql://u:p@db.ci.local/test")
    assert :ok = TestDbGuard.check("postgresql://u:p@localhost/test_hive?sslmode=disable")
  end

  test "rejects the production host" do
    for host <- ["postgres", "POSTGRES", "postgres.company-ops.svc.cluster.local"] do
      assert {:error, reason} = TestDbGuard.check("postgresql://u:p@#{host}:5432/hive_test")
      assert reason =~ "production Postgres"
    end
  end

  test "rejects the production schema" do
    for query <- [
          "schema=company",
          "search_path=company,public",
          "options=-csearch_path%3Dcompany"
        ] do
      assert {:error, reason} = TestDbGuard.check("postgresql://u:p@localhost/hive_test?#{query}")
      assert reason =~ "'company'"
    end
  end

  test "rejects databases that are not dedicated test databases" do
    assert {:error, reason} = TestDbGuard.check("postgresql://u:p@localhost:5544/homely_company")
    assert reason =~ "not a dedicated test database"
    assert {:error, _} = TestDbGuard.check("postgresql://u:p@localhost")
    assert {:error, _} = TestDbGuard.check("not a url")
  end

  test "check!/1 raises a clear error" do
    assert_raise RuntimeError, ~r/Refusing to run Hive tests/, fn ->
      TestDbGuard.check!("postgresql://u:p@postgres:5432/homely_company")
    end

    assert :ok = TestDbGuard.check!(nil)
  end
end
