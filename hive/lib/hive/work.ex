defmodule Hive.Work do
  @moduledoc false

  use GenServer

  @channel "hive_work"

  def start_link(_), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)

  def enqueue(id, parts), do: GenServer.call(__MODULE__, {:enqueue, id, parts})
  def available(limit \\ 10), do: GenServer.call(__MODULE__, {:available, limit})
  def register_agent(agent), do: GenServer.call(__MODULE__, {:register_agent, agent})

  def submit_bid(work_id, agent_id, bid),
    do: GenServer.call(__MODULE__, {:submit_bid, work_id, agent_id, bid})

  def ranked_bids(work_id), do: GenServer.call(__MODULE__, {:ranked_bids, work_id})
  def subscribe(pid, agent_id), do: GenServer.call(__MODULE__, {:subscribe, pid, agent_id})
  def unsubscribe(pid), do: GenServer.call(__MODULE__, {:unsubscribe, pid})
  def events(task_id, limit \\ 100), do: GenServer.call(__MODULE__, {:events, task_id, limit})

  def subscribe_events(pid, task_id),
    do: GenServer.call(__MODULE__, {:subscribe_events, pid, task_id})

  def unsubscribe_events(pid), do: GenServer.call(__MODULE__, {:unsubscribe_events, pid})

  def claim(id, agent_id, lease_seconds),
    do: GenServer.call(__MODULE__, {:claim, id, agent_id, lease_seconds})

  def complete(id, agent_id, state, result),
    do: GenServer.call(__MODULE__, {:complete, id, agent_id, state, result})

  def heartbeat(id, agent_id, lease_seconds),
    do: GenServer.call(__MODULE__, {:heartbeat, id, agent_id, lease_seconds})

  def get(id), do: GenServer.call(__MODULE__, {:get, id})

  def acknowledge_event(event_id, consumer_id),
    do: GenServer.call(__MODULE__, {:ack_event, event_id, consumer_id})

  @impl true
  def init(_) do
    ensure_subscribers_table()

    case System.get_env("COMPANY_DATABASE_URL") do
      nil ->
        {:ok, %{memory: %{work: %{}, agents: %{}, bids: %{}, events: []}}}

      url ->
        opts = db_opts(url)
        {:ok, db} = Postgrex.start_link(opts)

        Postgrex.query!(
          db,
          """
          CREATE TABLE IF NOT EXISTS company.hive_work_items (
            id TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            state TEXT NOT NULL DEFAULT 'available',
            claimed_by TEXT,
            lease_expires_at TIMESTAMPTZ,
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
          )
          """,
          []
        )

        Postgrex.query!(
          db,
          """
          CREATE TABLE IF NOT EXISTS company.hive_events (
            event_id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            task_id TEXT,
            sender TEXT NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            attempt INTEGER NOT NULL DEFAULT 1
          )
          """,
          []
        )

        Postgrex.query!(
          db,
          "CREATE INDEX IF NOT EXISTS hive_events_task_idx ON company.hive_events (task_id, occurred_at)",
          []
        )

        Postgrex.query!(
          db,
          """
          CREATE TABLE IF NOT EXISTS company.hive_event_consumptions (
            event_id TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (event_id, consumer_id)
          )
          """,
          []
        )

        Postgrex.query!(
          db,
          "CREATE INDEX IF NOT EXISTS hive_work_available_idx ON company.hive_work_items (state, created_at)",
          []
        )

        Postgrex.query!(
          db,
          """
          CREATE TABLE IF NOT EXISTS company.hive_work_bids (
            bid_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES company.hive_work_items(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            interested BOOLEAN NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            approach TEXT NOT NULL DEFAULT '',
            estimated_cost DOUBLE PRECISION NOT NULL,
            expected_benefit DOUBLE PRECISION NOT NULL,
            risk TEXT NOT NULL DEFAULT '',
            proposal JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (work_id, agent_id)
          )
          """,
          []
        )

        Postgrex.query!(
          db,
          "CREATE INDEX IF NOT EXISTS hive_work_bids_rank_idx ON company.hive_work_bids (work_id, confidence, expected_benefit)",
          []
        )

        Postgrex.query!(
          db,
          """
          CREATE TABLE IF NOT EXISTS company.hive_agents (
            id TEXT PRIMARY KEY,
            endpoint TEXT,
            capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
          )
          """,
          []
        )

        {:ok, %{db: db}}
    end
  end

  @impl true
  def handle_call({:enqueue, id, parts}, _from, %{memory: _memory} = state) do
    case state.memory.work[id] do
      nil ->
        item = %{id: id, payload: %{"parts" => parts}, state: "available", claimed_by: nil}
        event = event("work.created", id, %{"parts" => parts})
        notify_subscribers(id)
        notify_event_subscribers(event)

        {:reply, {:ok, item},
         state
         |> put_in([:memory, :work, id], item)
         |> update_in([:memory, :events], &[event | &1])}

      item ->
        {:reply, {:ok, item}, state}
    end
  end

  def handle_call({:enqueue, id, parts}, _from, %{db: db} = state) do
    payload = Jason.encode!(%{"parts" => parts})

    created =
      transaction!(db, fn tx ->
        inserted =
          Postgrex.query!(
            tx,
            "INSERT INTO company.hive_work_items (id, payload) VALUES ($1, $2::jsonb) ON CONFLICT (id) DO NOTHING RETURNING id",
            [id, payload]
          )

        case inserted.rows do
          [] ->
            false

          _ ->
            persist_event(tx, event("work.created", id, %{"parts" => parts}))
            true
        end
      end)

    if created do
      Postgrex.query!(db, "SELECT pg_notify($1, $2)", [@channel, id])
      notify_subscribers(id)
    end

    {:reply, get_db(db, id), state}
  end

  def handle_call({:available, limit}, _from, %{memory: memory} = state) do
    items =
      memory.work |> Map.values() |> Enum.filter(&(&1.state == "available")) |> Enum.take(limit)

    {:reply, {:ok, items}, state}
  end

  def handle_call({:available, limit}, _from, %{db: db} = state) do
    Postgrex.query!(
      db,
      "UPDATE company.hive_work_items SET state = 'available', claimed_by = NULL, lease_expires_at = NULL, updated_at = now() WHERE state = 'claimed' AND lease_expires_at < now()",
      []
    )

    result =
      Postgrex.query!(
        db,
        "SELECT id, payload, state, claimed_by FROM company.hive_work_items WHERE state = 'available' ORDER BY created_at LIMIT $1",
        [limit]
      )

    {:reply, {:ok, rows(result)}, state}
  end

  def handle_call({:events, task_id, limit}, _from, %{memory: memory} = state) do
    {:reply, {:ok, memory.events |> Enum.filter(&(&1["task_id"] == task_id)) |> Enum.take(limit)},
     state}
  end

  def handle_call({:events, task_id, limit}, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        "SELECT event_id, topic, task_id, sender, occurred_at, payload, attempt FROM company.hive_events WHERE task_id = $1 ORDER BY occurred_at LIMIT $2",
        [task_id, limit]
      )

    {:reply, {:ok, rows(result)}, state}
  end

  def handle_call({:register_agent, agent}, _from, %{memory: _memory} = state) do
    {:reply, :ok, put_in(state.memory.agents[agent["id"]], agent)}
  end

  def handle_call({:register_agent, agent}, _from, %{db: db} = state) do
    Postgrex.query!(
      db,
      "INSERT INTO company.hive_agents (id, endpoint, capabilities) VALUES ($1, $2, $3::jsonb) ON CONFLICT (id) DO UPDATE SET endpoint = EXCLUDED.endpoint, capabilities = EXCLUDED.capabilities, last_seen_at = now()",
      [agent["id"], agent["endpoint"], Jason.encode!(agent["capabilities"] || %{})]
    )

    {:reply, :ok, state}
  end

  def handle_call({:submit_bid, work_id, agent_id, bid}, _from, %{memory: memory} = state) do
    with {:ok, normalized} <- normalize_bid(bid),
         true <- Map.has_key?(memory.work, work_id) do
      item =
        Map.merge(normalized, %{
          "bid_id" => "bid_" <> random_id(),
          "work_id" => work_id,
          "agent_id" => agent_id
        })

      {:reply, {:ok, item}, put_in(state.memory.bids[{work_id, agent_id}], item)}
    else
      false -> {:reply, {:error, :work_not_found}, state}
      {:error, reason} -> {:reply, {:error, reason}, state}
    end
  end

  def handle_call({:submit_bid, work_id, agent_id, bid}, _from, %{db: db} = state) do
    with {:ok, normalized} <- normalize_bid(bid),
         {:ok, _work} <- get_db(db, work_id) do
      result =
        transaction!(db, fn tx ->
          Postgrex.query!(
            tx,
            """
            INSERT INTO company.hive_work_bids
              (bid_id, work_id, agent_id, interested, confidence, approach, estimated_cost, expected_benefit, risk, proposal)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT (work_id, agent_id) DO UPDATE SET
              interested = EXCLUDED.interested, confidence = EXCLUDED.confidence,
              approach = EXCLUDED.approach, estimated_cost = EXCLUDED.estimated_cost,
              expected_benefit = EXCLUDED.expected_benefit, risk = EXCLUDED.risk,
              proposal = EXCLUDED.proposal, created_at = now()
            RETURNING bid_id, work_id, agent_id, interested, confidence, approach,
              estimated_cost, expected_benefit, risk, proposal, created_at
            """,
            [
              "bid_" <> random_id(),
              work_id,
              agent_id,
              normalized["interested"],
              normalized["confidence"],
              normalized["approach"],
              normalized["estimated_cost"],
              normalized["expected_benefit"],
              normalized["risk"],
              Jason.encode!(bid)
            ]
          )
          |> rows()
          |> List.first()
        end)

      {:reply, {:ok, result}, state}
    else
      {:error, :not_found} -> {:reply, {:error, :work_not_found}, state}
      {:error, reason} -> {:reply, {:error, reason}, state}
    end
  end

  def handle_call({:ranked_bids, work_id}, _from, %{memory: memory} = state) do
    bids =
      memory.bids
      |> Enum.filter(fn {{id, _agent}, _bid} -> id == work_id end)
      |> Enum.map(fn {_key, bid} -> Map.put(bid, "score", score(bid)) end)
      |> Enum.sort_by(& &1["score"], :desc)

    {:reply, {:ok, bids}, state}
  end

  def handle_call({:ranked_bids, work_id}, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        """
        SELECT bid_id, work_id, agent_id, interested, confidence, approach,
          estimated_cost, expected_benefit, risk, proposal, created_at,
          CASE WHEN interested THEN confidence * expected_benefit / GREATEST(estimated_cost, 0.01) ELSE 0 END AS score
        FROM company.hive_work_bids WHERE work_id = $1
        ORDER BY score DESC, created_at ASC
        """,
        [work_id]
      )

    {:reply, {:ok, rows(result)}, state}
  end

  def handle_call({:subscribe, pid, agent_id}, _from, state) do
    :ets.insert(:hive_subscribers, {:work, pid, agent_id})
    Process.monitor(pid)
    {:reply, :ok, state}
  end

  def handle_call({:unsubscribe, pid}, _from, state) do
    :ets.match_delete(:hive_subscribers, {:work, pid, :_})
    :ets.match_delete(:hive_subscribers, {:event, pid, :_})
    {:reply, :ok, state}
  end

  def handle_call({:subscribe_events, pid, task_id}, _from, state) do
    :ets.insert(:hive_subscribers, {:event, pid, task_id})
    Process.monitor(pid)
    {:reply, :ok, state}
  end

  def handle_call({:unsubscribe_events, pid}, _from, state) do
    :ets.match_delete(:hive_subscribers, {:event, pid, :_})
    {:reply, :ok, state}
  end

  def handle_call({:claim, id, agent_id, _lease_seconds}, _from, %{memory: memory} = state) do
    case memory.work[id] do
      %{state: "available"} = item ->
        claimed = %{item | state: "claimed", claimed_by: agent_id}
        event = event("work.allocated", id, %{"agent_id" => agent_id})
        started = event("work.started", id, %{"agent_id" => agent_id})

        next =
          state
          |> put_in([:memory, :work, id], claimed)
          |> update_in([:memory, :events], &[started, event | &1])

        notify_event_subscribers(started)
        notify_event_subscribers(event)
        {:reply, {:ok, claimed}, next}

      _ ->
        {:reply, {:error, :unavailable}, state}
    end
  end

  def handle_call({:claim, id, agent_id, lease_seconds}, _from, %{db: db} = state) do
    allocated = event("work.allocated", id, %{"agent_id" => agent_id})
    started = event("work.started", id, %{"agent_id" => agent_id})

    result =
      transaction!(db, fn tx ->
        result =
          Postgrex.query!(
            tx,
            "UPDATE company.hive_work_items SET state = 'claimed', claimed_by = $2, lease_expires_at = now() + ($3 || ' seconds')::interval, updated_at = now() WHERE id = $1 AND (state = 'available' OR (state = 'claimed' AND lease_expires_at < now())) RETURNING id, payload, state, claimed_by",
            [id, agent_id, Integer.to_string(lease_seconds)]
          )

        case rows(result) do
          [item] ->
            persist_event(tx, allocated)
            persist_event(tx, started)
            {:ok, item}

          _ ->
            {:error, :unavailable}
        end
      end)

    if match?({:ok, _}, result) do
      notify_event_subscribers(allocated)
      notify_event_subscribers(started)
    end

    {:reply, result, state}
  end

  def handle_call(
        {:complete, id, agent_id, final_state, result},
        _from,
        %{memory: memory} = state
      ) do
    case memory.work[id] do
      %{claimed_by: ^agent_id} = item ->
        completed = Map.merge(item, %{state: final_state, result: result})

        topic =
          if final_state == "completed", do: "engineering.completed", else: "engineering.failed"

        event =
          event(topic, id, %{
            "agent_id" => agent_id,
            "state" => final_state,
            "result" => result || %{}
          })

        notify_event_subscribers(event)

        next =
          state
          |> put_in([:memory, :work, id], completed)
          |> update_in([:memory, :events], &[event | &1])

        {:reply, {:ok, completed}, next}

      _ ->
        {:reply, {:error, :not_owner}, state}
    end
  end

  def handle_call({:complete, id, agent_id, final_state, result}, _from, %{db: db} = state) do
    topic = if final_state == "completed", do: "engineering.completed", else: "engineering.failed"

    completion_event =
      event(topic, id, %{
        "agent_id" => agent_id,
        "state" => final_state,
        "result" => result || %{}
      })

    result =
      transaction!(db, fn tx ->
        updated =
          Postgrex.query!(
            tx,
            "UPDATE company.hive_work_items SET state = $3, result = $4::jsonb, updated_at = now() WHERE id = $1 AND claimed_by = $2 RETURNING id, payload, state, claimed_by, result",
            [id, agent_id, final_state, Jason.encode!(result || %{})]
          )

        case rows(updated) do
          [item] ->
            persist_event(tx, completion_event)
            {:ok, item}

          _ ->
            {:error, :not_owner}
        end
      end)

    if match?({:ok, _}, result), do: notify_event_subscribers(completion_event)
    {:reply, result, state}
  end

  def handle_call({:heartbeat, id, agent_id, _lease_seconds}, _from, %{memory: memory} = state) do
    case memory.work[id] do
      %{state: "claimed", claimed_by: ^agent_id} = item -> {:reply, {:ok, item}, state}
      _ -> {:reply, {:error, :not_owner}, state}
    end
  end

  def handle_call({:heartbeat, id, agent_id, lease_seconds}, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        "UPDATE company.hive_work_items SET lease_expires_at = now() + ($3 || ' seconds')::interval, updated_at = now() WHERE id = $1 AND state = 'claimed' AND claimed_by = $2 AND lease_expires_at >= now() RETURNING id, payload, state, claimed_by, lease_expires_at",
        [id, agent_id, Integer.to_string(lease_seconds)]
      )

    {:reply,
     case rows(result) do
       [item] -> {:ok, item}
       _ -> {:error, :not_owner}
     end, state}
  end

  def handle_call({:ack_event, _event_id, _consumer_id}, _from, %{memory: _memory} = state),
    do: {:reply, :ok, state}

  def handle_call({:ack_event, event_id, consumer_id}, _from, %{db: db} = state) do
    Postgrex.query!(
      db,
      "INSERT INTO company.hive_event_consumptions (event_id, consumer_id) VALUES ($1, $2) ON CONFLICT (event_id, consumer_id) DO UPDATE SET acknowledged_at = now()",
      [event_id, consumer_id]
    )

    {:reply, :ok, state}
  end

  def handle_call({:get, id}, _from, %{memory: memory} = state),
    do: {:reply, Map.get(memory.work, id), state}

  def handle_call({:get, id}, _from, %{db: db} = state), do: {:reply, get_db(db, id), state}

  @impl true
  def handle_info({:DOWN, _ref, :process, pid, _reason}, state) do
    :ets.match_delete(:hive_subscribers, {:work, pid, :_})
    :ets.match_delete(:hive_subscribers, {:event, pid, :_})
    {:noreply, state}
  end

  defp get_db(db, id) do
    case rows(
           Postgrex.query!(
             db,
             "SELECT id, payload, state, claimed_by, result FROM company.hive_work_items WHERE id = $1",
             [id]
           )
         ) do
      [item] -> {:ok, item}
      _ -> {:error, :not_found}
    end
  end

  defp event(topic, task_id, payload) do
    %{
      "event_id" => "evt_" <> Base.encode16(:crypto.strong_rand_bytes(8), case: :lower),
      "topic" => topic,
      "task_id" => task_id,
      "sender" => System.get_env("HIVE_AGENT_ID", "hive-coordinator"),
      "occurred_at" => DateTime.utc_now() |> DateTime.to_iso8601(),
      "payload" => payload,
      "attempt" => 1
    }
  end

  defp normalize_bid(bid) do
    interested = Map.get(bid, "interested", true)
    confidence = number(Map.get(bid, "confidence", 0))
    estimated_cost = number(Map.get(bid, "estimated_cost", 0))
    expected_benefit = number(Map.get(bid, "expected_benefit", 0))

    cond do
      not is_boolean(interested) ->
        {:error, :invalid_bid}

      confidence < 0 or confidence > 1 ->
        {:error, :invalid_bid}

      estimated_cost < 0 or expected_benefit < 0 ->
        {:error, :invalid_bid}

      true ->
        {:ok,
         %{
           "interested" => interested,
           "confidence" => confidence,
           "approach" => Map.get(bid, "approach", ""),
           "estimated_cost" => estimated_cost,
           "expected_benefit" => expected_benefit,
           "risk" => Map.get(bid, "risk", "")
         }}
    end
  end

  defp number(value) when is_integer(value), do: value * 1.0
  defp number(value) when is_float(value), do: value

  defp number(value) when is_binary(value) do
    case Float.parse(value) do
      {number, ""} -> number
      _ -> -1.0
    end
  end

  defp number(_), do: -1.0

  defp score(bid),
    do:
      if(bid["interested"],
        do: bid["confidence"] * bid["expected_benefit"] / max(bid["estimated_cost"], 0.01),
        else: 0.0
      )

  defp random_id, do: Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)

  defp persist_event(db, event) do
    Postgrex.query!(
      db,
      "INSERT INTO company.hive_events (event_id, topic, task_id, sender, occurred_at, payload, attempt) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
      [
        event["event_id"],
        event["topic"],
        event["task_id"],
        event["sender"],
        event["occurred_at"],
        Jason.encode!(event["payload"]),
        event["attempt"]
      ]
    )
  end

  defp transaction!(db, fun) do
    case Postgrex.transaction(db, fun) do
      {:ok, value} -> value
      {:error, reason} -> raise "Hive database transaction failed: #{inspect(reason)}"
    end
  end

  defp rows(%Postgrex.Result{columns: columns, rows: values}),
    do: Enum.map(values, &decode_row(Map.new(Enum.zip(columns, &1))))

  defp decode_row(row) do
    Enum.reduce(["payload", "result"], row, fn key, acc ->
      case Map.get(acc, key) do
        value when is_binary(value) -> Map.put(acc, key, Jason.decode!(value))
        _ -> acc
      end
    end)
  end

  defp ensure_subscribers_table do
    case :ets.whereis(:hive_subscribers) do
      :undefined -> :ets.new(:hive_subscribers, [:named_table, :public, :set])
      _ -> :hive_subscribers
    end
  end

  defp notify_subscribers(id) do
    for {:work, pid, _agent_id} <- :ets.tab2list(:hive_subscribers),
        do: send(pid, {:hive_work_available, id})
  end

  defp notify_event_subscribers(event) do
    task_id = event["task_id"]

    for {:event, pid, subscribed_task_id} <- :ets.tab2list(:hive_subscribers),
        subscribed_task_id == task_id,
        do: send(pid, {:hive_event_available, task_id})
  end

  defp db_opts(url) do
    uri = URI.parse(url)

    [
      hostname: uri.host,
      port: uri.port || 5432,
      username: uri.userinfo |> String.split(":") |> hd() |> URI.decode(),
      password: uri.userinfo |> String.split(":") |> List.last() |> URI.decode(),
      database: String.trim_leading(uri.path || "", "/"),
      pool_size: 5
    ]
  end
end
