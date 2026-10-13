defmodule Hive.TestDatabaseGuard do
  @moduledoc """
  Refuses to let the test suite run against anything but a dedicated test database.

  On 2026-10-12 a test run with the production `COMPANY_DATABASE_URL` in its
  environment wrote junk work items, events and agents into the prod `company`
  schema. `mix test` calls `check!/1` (see the `test` alias in `mix.exs`) before the
  application boots, because `Hive.Work` connects to Postgres on startup.

  With no database URL set the suite runs on the in-memory store, which is allowed.
  """

  @url_vars ["COMPANY_DATABASE_URL", "DATABASE_URL"]
  @prod_schema "company"
  @prod_database "homely_company"
  @prod_host_pattern ~r/^postgres(\.company-ops)?(\.svc(\.cluster\.local)?)?$|company-ops/i

  @doc "Raises `Mix.Error`-style `RuntimeError` unless every configured URL is a test database."
  def check!(env \\ System.get_env()) do
    for var <- @url_vars, url = env[var], url not in [nil, ""] do
      case validate(url) do
        :ok -> :ok
        {:error, reason} -> raise "Refusing to run Hive tests: #{var} #{reason}. " <> hint()
      end
    end

    :ok
  end

  @doc "Returns `:ok` or `{:error, reason}` for one database URL."
  def validate(url) when is_binary(url) do
    uri = URI.parse(url)
    host = uri.host || ""
    database = String.trim_leading(uri.path || "", "/")
    query = URI.decode_query(uri.query || "")

    cond do
      host == "" or database == "" ->
        {:error, "is not a postgresql://user:pass@host/database URL"}

      Regex.match?(@prod_host_pattern, host) ->
        {:error, "points at the production Postgres host #{inspect(host)}"}

      database == @prod_database ->
        {:error, "points at the production database #{inspect(database)}"}

      not String.ends_with?(database, "_test") ->
        {:error, "database #{inspect(database)} is not a dedicated test database (name must end in _test)"}

      schema_override(query) == @prod_schema ->
        {:error, "selects the production schema #{inspect(@prod_schema)}"}

      true ->
        :ok
    end
  end

  defp schema_override(query) do
    case Map.get(query, "options", "") |> String.split("search_path=") do
      [_, rest] -> rest |> String.split([",", " "]) |> hd() |> String.trim()
      _ -> Map.get(query, "schema")
    end
  end

  defp hint do
    "Unset it to use the in-memory store, or point it at a dedicated database, " <>
      "e.g. postgresql://hive:hive@localhost:5432/hive_test (see hive/README.md)."
  end
end
