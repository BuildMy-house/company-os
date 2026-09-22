defmodule Hive.MixProject do
  use Mix.Project

  def project do
    [
      app: :hive,
      version: "0.1.0",
      elixir: "~> 1.20",
      start_permanent: Mix.env() == :prod,
      deps: deps()
    ]
  end

  def application do
    [extra_applications: [:logger, :inets, :ssl], mod: {Hive.Application, []}]
  end

  defp deps do
    [
      {:jason, "~> 1.4"},
      {:plug_cowboy, "~> 2.7"},
      {:postgrex, "~> 0.20"}
    ]
  end
end
