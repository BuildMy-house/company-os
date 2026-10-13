defmodule Hive.TestDatabaseGuardTest do
  use ExUnit.Case, async: true

  alias Hive.TestDatabaseGuard, as: Guard

  test "allows an unset URL (in-memory store)" do
    assert Guard.check!(%{}) == :ok
    assert Guard.check!(%{"COMPANY_DATABASE_URL" => ""}) == :ok
  end

  test "allows a dedicated test database" do
    assert Guard.validate("postgresql://hive:hive@localhost:5432/hive_test") == :ok
    assert Guard.check!(%{"COMPANY_DATABASE_URL" => "postgresql://u:p@db.ci:5432/company_test"}) == :ok
  end

  test "rejects the production Postgres hosts" do
    for host <- ["postgres", "postgres.company-ops", "postgres.company-ops.svc.cluster.local"] do
      assert {:error, msg} = Guard.validate("postgresql://u:p@#{host}:5432/hive_test")
      assert msg =~ "production Postgres host"
    end
  end

  test "rejects the production database and non-test database names" do
    assert {:error, _} = Guard.validate("postgresql://u:p@localhost/homely_company")
    assert {:error, msg} = Guard.validate("postgresql://u:p@localhost/hive")
    assert msg =~ "_test"
  end

  test "rejects the company schema" do
    url = "postgresql://u:p@localhost/hive_test?options=-csearch_path%3Dcompany"
    assert {:error, msg} = Guard.validate(url)
    assert msg =~ "schema"
    assert {:error, _} = Guard.validate("postgresql://u:p@localhost/hive_test?schema=company")
  end

  test "rejects malformed URLs" do
    assert {:error, _} = Guard.validate("not a url")
  end

  test "check! raises naming the offending variable, for DATABASE_URL too" do
    assert_raise RuntimeError, ~r/COMPANY_DATABASE_URL/, fn ->
      Guard.check!(%{"COMPANY_DATABASE_URL" => "postgresql://u:p@postgres/homely_company"})
    end

    assert_raise RuntimeError, ~r/DATABASE_URL/, fn ->
      Guard.check!(%{"DATABASE_URL" => "postgresql://u:p@localhost/prod"})
    end
  end
end
