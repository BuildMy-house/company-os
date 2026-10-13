defmodule Mix.Tasks.Hive.TestGuard do
  @shortdoc "Fails unless the test database is dedicated (not prod)"
  @moduledoc "Runs `Hive.TestDatabaseGuard.check!/0`; wired in front of `mix test`."
  use Mix.Task

  @impl true
  def run(_args) do
    Mix.Task.run("loadpaths")
    Mix.Task.run("compile", ["--no-deps-check"])

    try do
      Hive.TestDatabaseGuard.check!()
    rescue
      e in RuntimeError -> Mix.raise(Exception.message(e))
    end
  end
end
