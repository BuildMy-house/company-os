defmodule Hive.Router do
  use Plug.Router

  plug(:match)
  plug(:fetch_query_params)
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
        parts = get_in(params, ["message", "parts"]) || []
        task = Hive.Tasks.create(task_id, parts)
        started_at = System.monotonic_time(:millisecond)

        Hive.Telemetry.emit(%{
          "event" => "a2a_task",
          "task_id" => task_id,
          "state" => "submitted"
        })

        {:ok, _work} = Hive.Work.enqueue(task_id, parts)

        Hive.Telemetry.emit(%{
          "event" => "a2a_task",
          "task_id" => task_id,
          "state" => "available",
          "duration_ms" => System.monotonic_time(:millisecond) - started_at
        })

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
      task -> json(conn, task_response(refresh_work(task)))
    end
  end

  post "/agents/register" do
    case conn.body_params do
      %{"id" => id} = agent ->
        :ok = Hive.Work.register_agent(Map.put_new(agent, "capabilities", %{}))
        json(conn, %{"agent_id" => id, "status" => "registered"})

      _ ->
        json(conn, %{"error" => "agent id required"}, 400)
    end
  end

  get "/work/subscribe" do
    case conn.params["agent_id"] do
      agent_id when is_binary(agent_id) and byte_size(agent_id) > 0 ->
        conn =
          conn
          |> put_resp_header("cache-control", "no-cache")
          |> put_resp_header("connection", "keep-alive")
          |> put_resp_header("content-type", "text/event-stream")
          |> send_chunked(200)

        :ok = Hive.Work.subscribe(self(), agent_id)
        stream_work(conn)

      _ ->
        json(conn, %{"error" => "agent_id required"}, 400)
    end
  end

  get "/work" do
    {:ok, work} = Hive.Work.available(String.to_integer(conn.params["limit"] || "10"))
    json(conn, %{"work" => work})
  end

  post "/work/:work_id/claim" do
    with %{"agent_id" => agent_id} <- conn.body_params,
         {:ok, work} <-
           Hive.Work.claim(work_id, agent_id, conn.body_params["lease_seconds"] || 300) do
      json(conn, work)
    else
      {:error, :unavailable} -> json(conn, %{"error" => "work unavailable"}, 409)
      _ -> json(conn, %{"error" => "agent_id required"}, 400)
    end
  end

  post "/work/:work_id/complete" do
    with %{"agent_id" => agent_id, "state" => state} <- conn.body_params,
         {:ok, work} <-
           Hive.Work.complete(work_id, agent_id, state, conn.body_params["result"] || %{}) do
      Hive.Tasks.attach_remote(work_id, %{
        "id" => work_id,
        "status" => %{"state" => state},
        "result" => work["result"]
      })

      json(conn, work)
    else
      {:error, :not_owner} -> json(conn, %{"error" => "agent does not hold lease"}, 409)
      _ -> json(conn, %{"error" => "agent_id and state required"}, 400)
    end
  end

  match _ do
    json(conn, %{"error" => "not found"}, 404)
  end

  defp task_response(task),
    do: %{
      "id" => task.id,
      "status" => %{"state" => task.state},
      "metadata" => %{"remote" => task.remote}
    }

  defp refresh_work(task) do
    case Hive.Work.get(task.id) do
      %{} = work -> %{task | state: work["state"] || work.state}
      _ -> task
    end
  end

  defp json(conn, body, status \\ 200) do
    conn |> put_resp_content_type("application/json") |> send_resp(status, Jason.encode!(body))
  end

  defp stream_work(conn) do
    try do
      case Hive.Work.available(100) do
        {:ok, work} -> stream_available(conn, work)
        _ -> :closed
      end
      |> case do
        {:ok, next} -> stream_loop(next)
        :closed -> :ok
      end
    after
      Hive.Work.unsubscribe(self())
    end
  end

  defp stream_loop(conn) do
    receive do
      {:hive_work_available, _id} ->
        case Hive.Work.available(100) do
          {:ok, work} ->
            case stream_available(conn, work) do
              {:ok, next} -> stream_loop(next)
              :closed -> :ok
            end

          _ -> stream_loop(conn)
        end

      {:hive_work_heartbeat} ->
        case Plug.Conn.chunk(conn, ": heartbeat\n\n") do
          {:ok, next} -> stream_loop(next)
          {:error, :closed} -> conn
        end

    after
      15_000 ->
        case Plug.Conn.chunk(conn, ": heartbeat\n\n") do
          {:ok, next} -> stream_loop(next)
          {:error, :closed} -> conn
        end
    end
  end

  defp stream_available(conn, work) do
    Enum.reduce_while(work, {:ok, conn}, fn item, {:ok, current} ->
      case Plug.Conn.chunk(current, "data: " <> Jason.encode!(item) <> "\n\n") do
        {:ok, next} -> {:cont, {:ok, next}}
        {:error, :closed} -> {:halt, :closed}
      end
    end)
  end
end
