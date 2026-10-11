defmodule Hive.Telemetry do
  @moduledoc false

  def emit(event) do
    token = System.get_env("AXIOM_TOKEN")

    if token do
      Task.start(fn ->
        :inets.start()
        :ssl.start()

        url =
          "https://eu-central-1.aws.edge.axiom.co/v1/ingest/#{System.get_env("AXIOM_DATASET", "bmh-company")}"

        body = Jason.encode!([build_event(event)])

        :httpc.request(
          :post,
          {String.to_charlist(url), [{~c"Authorization", String.to_charlist("Bearer " <> token)}],
           ~c"application/json", body},
          [],
          []
        )
      end)
    end

    :ok
  end

  @doc """
  Merges the shared event envelope (_time, service, environment, role) under
  the caller's fields. Same shape as company_ops/axiom_emit.py and the
  opencode/hermes plugins so one APL query covers every component.
  """
  def build_event(event) do
    Map.merge(
      %{
        "_time" => DateTime.utc_now() |> DateTime.to_iso8601(),
        "service" => System.get_env("AXIOM_SERVICE_NAME", "hive"),
        "environment" => System.get_env("DEPLOYMENT_ENVIRONMENT", "local"),
        "role" => "coordinator"
      },
      event
    )
  end
end
