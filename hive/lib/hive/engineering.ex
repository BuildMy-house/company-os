defmodule Hive.Engineering do
  @moduledoc false

  def submit(parts) do
    case Application.get_env(:hive, :engineering_submit) do
      fun when is_function(fun, 1) -> fun.(parts)
      _ -> request(parts)
    end
  end

  def get(task_id) do
    case Application.get_env(:hive, :engineering_get) do
      fun when is_function(fun, 1) ->
        fun.(task_id)

      _ ->
        :inets.start()

        url =
          Application.get_env(:hive, :engineering_a2a_url, "http://engineering-agent:8001") <>
            "/tasks/" <> task_id

        case :httpc.request(:get, {String.to_charlist(url), []}, [], []) do
          {:ok, {{_, status, _}, _, response}} when status in 200..299 ->
            {:ok, Jason.decode!(response)}

          {:ok, {{_, status, _}, _, response}} ->
            {:error, "engineering returned #{status}: #{response}"}

          {:error, reason} ->
            {:error, "engineering request failed: #{inspect(reason)}"}
        end
    end
  end

  defp request(parts) do
    :inets.start()
    url = Application.get_env(:hive, :engineering_a2a_url, "http://engineering-agent:8001")

    body =
      Jason.encode!(%{
        "jsonrpc" => "2.0",
        "id" => "hive_#{System.unique_integer([:positive])}",
        "method" => "message/send",
        "params" => %{"message" => %{"role" => "user", "parts" => parts}}
      })

    case :httpc.request(:post, {String.to_charlist(url), [], ~c"application/json", body}, [], []) do
      {:ok, {{_, status, _}, _, response}} when status in 200..299 ->
        {:ok, Jason.decode!(response)["result"]}

      {:ok, {{_, status, _}, _, response}} ->
        {:error, "engineering returned #{status}: #{response}"}

      {:error, reason} ->
        {:error, "engineering request failed: #{inspect(reason)}"}
    end
  end
end
