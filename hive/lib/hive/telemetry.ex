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

        body =
          Jason.encode!([
            Map.merge(
              %{
                "_time" => DateTime.utc_now() |> DateTime.to_iso8601(),
                "service" => System.get_env("AXIOM_SERVICE_NAME", "hive"),
                "environment" => System.get_env("DEPLOYMENT_ENVIRONMENT", "local"),
                "role" => "coordinator"
              },
              event
            )
          ])

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
end
