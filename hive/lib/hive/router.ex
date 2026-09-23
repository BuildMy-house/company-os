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

  post "/work/:work_id/bids" do
    with %{"agent_id" => agent_id} <- conn.body_params,
         {:ok, bid} <-
           Hive.Work.submit_bid(work_id, agent_id, Map.delete(conn.body_params, "agent_id")) do
      json(conn, bid, 201)
    else
      {:error, :work_not_found} -> json(conn, %{"error" => "work not found"}, 404)
      {:error, :invalid_bid} -> json(conn, %{"error" => "invalid bid"}, 422)
      _ -> json(conn, %{"error" => "agent_id required"}, 400)
    end
  end

  get "/work/:work_id/bids" do
    case Hive.Work.ranked_bids(work_id) do
      {:ok, bids} -> json(conn, %{"work_id" => work_id, "bids" => bids})
      _ -> json(conn, %{"error" => "unable to rank bids"}, 500)
    end
  end

  post "/work/:work_id/allocate" do
    case Hive.Work.allocate(work_id, conn.body_params["lease_seconds"] || 900) do
      {:ok, work} -> json(conn, work)
      {:error, :work_not_found} -> json(conn, %{"error" => "work not found"}, 404)
      {:error, :no_bids} -> json(conn, %{"error" => "no interested bids"}, 409)
      {:error, :unavailable} -> json(conn, %{"error" => "work unavailable"}, 409)
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

  get "/events" do
    task_id = conn.params["task_id"]
    limit = String.to_integer(conn.params["limit"] || "100")

    case task_id && Hive.Work.events(task_id, limit) do
      {:ok, events} -> json(conn, %{"events" => events})
      _ -> json(conn, %{"error" => "task_id required"}, 400)
    end
  end

  get "/events/subscribe" do
    case conn.params["task_id"] do
      task_id when is_binary(task_id) and byte_size(task_id) > 0 ->
        conn =
          conn
          |> put_resp_header("cache-control", "no-cache")
          |> put_resp_header("connection", "keep-alive")
          |> put_resp_header("content-type", "text/event-stream")
          |> send_chunked(200)

        :ok = Hive.Work.subscribe_events(self(), task_id)
        stream_events(conn, task_id)

      _ ->
        json(conn, %{"error" => "task_id required"}, 400)
    end
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

  post "/work/:work_id/heartbeat" do
    with %{"agent_id" => agent_id} <- conn.body_params,
         {:ok, work} <-
           Hive.Work.heartbeat(work_id, agent_id, conn.body_params["lease_seconds"] || 900) do
      json(conn, work)
    else
      {:error, :not_owner} -> json(conn, %{"error" => "agent does not hold active lease"}, 409)
      _ -> json(conn, %{"error" => "agent_id required"}, 400)
    end
  end

  post "/events/:event_id/ack" do
    with %{"consumer_id" => consumer_id} <- conn.body_params do
      :ok = Hive.Work.acknowledge_event(event_id, consumer_id)
      json(conn, %{"event_id" => event_id, "consumer_id" => consumer_id, "acknowledged" => true})
    else
      _ -> json(conn, %{"error" => "consumer_id required"}, 400)
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

          _ ->
            stream_loop(conn)
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

  defp stream_events(conn, task_id) do
    try do
      case Hive.Work.events(task_id) do
        {:ok, events} -> stream_event_list(conn, events)
        _ -> conn
      end
    after
      Hive.Work.unsubscribe_events(self())
    end
  end

  defp stream_event_list(conn, events) do
    Enum.reduce_while(events, conn, fn event, current ->
      case Plug.Conn.chunk(current, "data: " <> Jason.encode!(event) <> "\n\n") do
        {:ok, next} -> {:cont, next}
        {:error, :closed} -> {:halt, current}
      end
    end)
    |> stream_event_loop()
  end

  defp stream_event_loop(conn) do
    receive do
      {:hive_event_available, event} ->
        case Plug.Conn.chunk(conn, "data: " <> Jason.encode!(event) <> "\n\n") do
          {:ok, next} -> stream_event_loop(next)
          {:error, :closed} -> conn
        end
    after
      15_000 ->
        case Plug.Conn.chunk(conn, ": heartbeat\n\n") do
          {:ok, next} -> stream_event_loop(next)
          {:error, :closed} -> conn
        end
    end
  end
end
