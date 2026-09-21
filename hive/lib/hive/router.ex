defmodule Hive.Router do
  use Plug.Router

  plug(:match)
  plug(Plug.Parsers, parsers: [:json], json_decoder: Jason)
  plug(:dispatch)

  get "/.well-known/agent-card.json" do
    json(conn, %{
      "name" => "buildmy.house Hive",
      "description" => "Coordinates work between buildmy.house agents",
      "url" => "http://hive-coordinator:4100",
      "version" => "0.1.0",
      "capabilities" => %{"streaming" => false, "pushNotifications" => false},
      "skills" => [%{"id" => "coordinate-work", "name" => "Coordinate work"}]
    })
  end

  post "/" do
    case conn.body_params do
      %{"jsonrpc" => "2.0", "id" => request_id, "method" => "message/send", "params" => params} ->
        task_id = "task_" <> Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)
        task = Hive.Tasks.create(task_id, get_in(params, ["message", "parts"]) || [])
        json(conn, %{"jsonrpc" => "2.0", "id" => request_id, "result" => task_response(task)})

      %{"jsonrpc" => "2.0", "id" => request_id, "method" => method} ->
        json(
          conn,
          %{
            "jsonrpc" => "2.0",
            "id" => request_id,
            "error" => %{"code" => -32601, "message" => "unsupported method: #{method}"}
          },
          404
        )

      _ ->
        json(
          conn,
          %{
            "jsonrpc" => "2.0",
            "id" => nil,
            "error" => %{"code" => -32600, "message" => "invalid JSON-RPC request"}
          },
          400
        )
    end
  end

  get "/tasks/:task_id" do
    case Hive.Tasks.get(task_id) do
      nil -> json(conn, %{"error" => "task not found"}, 404)
      task -> json(conn, task_response(task))
    end
  end

  match _ do
    json(conn, %{"error" => "not found"}, 404)
  end

  defp task_response(task), do: %{"id" => task.id, "status" => %{"state" => task.state}}

  defp json(conn, body, status \\ 200) do
    conn |> put_resp_content_type("application/json") |> send_resp(status, Jason.encode!(body))
  end
end
