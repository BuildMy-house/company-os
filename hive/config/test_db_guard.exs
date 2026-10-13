defmodule Hive.TestDbGuard do
  @moduledoc """
  Refuses to run the Hive test suite against anything but a dedicated test database.

  `Hive.Work` connects to whatever `COMPANY_DATABASE_URL` names and every query is
  qualified with `company.`, so a stray production URL in the environment makes the
  suite write junk work items, events and agents into the live `company` schema
  (2026-10-12: 112 work items, 348 events). This runs from `config/test.exs`, before
  the application starts, so nothing connects first.

  Allowed: no URL at all (in-memory mode), or a URL whose database name is
  `test`/`*_test`/`test_*` on a non-production host, with no `schema`/`search_path`
  pointing at `company`.
  """

  @prod_hosts ~w(postgres postgres.company-ops postgres.company-ops.svc postgres.company-ops.svc.cluster.local)

  @doc "Returns `:ok` or `{:error, reason}`."
  def check(nil), do: :ok
  def check(""), do: :ok

  def check(url) when is_binary(url) do
    uri = URI.parse(url)
    host = String.downcase(uri.host || "")
    database = String.trim_leading(uri.path || "", "/")
    params = URI.decode_query(uri.query || "")

    cond do
      host == "" or database == "" ->
        {:error, "URL has no host or database name"}

      host in @prod_hosts ->
        {:error, "host #{inspect(host)} is the production Postgres"}

      schema_company?(params) ->
        {:error, "URL selects the production schema 'company'"}

      not test_database?(database) ->
        {:error, "database #{inspect(database)} is not a dedicated test database (name must be test, *_test or test_*)"}

      true ->
        :ok
    end
  end

  @doc "Raises with a clear message unless `check/1` passes."
  def check!(url) do
    case check(url) do
      :ok ->
        :ok

      {:error, reason} ->
        raise """
        Refusing to run Hive tests: #{reason}.

        COMPANY_DATABASE_URL must point at a dedicated test database (for example
        postgresql://user:pass@localhost:5432/hive_test), never the production
        `company` schema or the production Postgres host. Unset it to run against the
        in-memory store. See hive/README.md, "Running tests".
        """
    end
  end

  defp test_database?(database),
    do: database == "test" or String.ends_with?(database, "_test") or String.starts_with?(database, "test_")

  defp schema_company?(params) do
    Enum.any?(~w(schema search_path currentSchema), fn key ->
      params |> Map.get(key, "") |> String.downcase() |> String.split(",") |> Enum.any?(&(String.trim(&1) == "company"))
    end) or
      (params |> Map.get("options", "") |> String.downcase() |> String.contains?("search_path=company"))
  end
end
