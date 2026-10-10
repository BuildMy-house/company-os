defmodule Hive.Work do
  @moduledoc false

  use GenServer

  @channel "hive_work"
  @tiers ~w(cheap standard power)
  @default_scoring %{"confidence_weight" => 1.0, "benefit_weight" => 1.0, "cost_weight" => 1.0}

  def start_link(_), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)

  def enqueue(id, parts, metadata \\ %{}),
    do: GenServer.call(__MODULE__, {:enqueue, id, parts, metadata})

  def available(limit \\ 10), do: GenServer.call(__MODULE__, {:available, limit})
  def register_agent(agent), do: GenServer.call(__MODULE__, {:register_agent, agent})
  def agents, do: GenServer.call(__MODULE__, :agents)

  def resolve_id(id_or_slug), do: GenServer.call(__MODULE__, {:resolve_id, id_or_slug})

  def submit_bid(work_ref, agent_id, bid),
    do:
      with_id(work_ref, fn id -> GenServer.call(__MODULE__, {:submit_bid, id, agent_id, bid}) end)

  def ranked_bids(work_ref, limit \\ 4),
    do: with_id(work_ref, fn id -> GenServer.call(__MODULE__, {:ranked_bids, id, limit}) end)

  def allocate(work_ref, lease_seconds \\ 900),
    do: allocate_after_expiry_recovery(work_ref, lease_seconds)

  def scoring, do: GenServer.call(__MODULE__, :scoring)
  def set_scoring(config), do: GenServer.call(__MODULE__, {:set_scoring, config})

  def subscribe(pid, agent_id), do: GenServer.call(__MODULE__, {:subscribe, pid, agent_id})
  def unsubscribe(pid), do: GenServer.call(__MODULE__, {:unsubscribe, pid})

  def events(task_ref, limit \\ 100),
    do: with_id(task_ref, fn id -> GenServer.call(__MODULE__, {:events, id, limit}) end)

  def subscribe_events(pid, task_ref),
    do: with_id(task_ref, fn id -> GenServer.call(__MODULE__, {:subscribe_events, pid, id}) end)

  def unsubscribe_events(pid), do: GenServer.call(__MODULE__, {:unsubscribe_events, pid})

  def claim(work_ref, agent_id, lease_seconds),
    do: claim_after_expiry_recovery(work_ref, agent_id, lease_seconds)

  def complete(work_ref, agent_id, state, result) when state in ["completed", "failed"],
    do: complete_after_expiry_recovery(work_ref, agent_id, state, result)

  def complete(_work_ref, _agent_id, _state, _result), do: {:error, :invalid_state}

  def heartbeat(work_ref, agent_id, lease_seconds),
    do: heartbeat_after_expiry_recovery(work_ref, agent_id, lease_seconds)

  def get(id_or_slug),
    do: with_id(id_or_slug, fn id -> GenServer.call(__MODULE__, {:get, id}) end)

  def acknowledge_event(event_id, consumer_id),
    do: GenServer.call(__MODULE__, {:ack_event, event_id, consumer_id})

  @impl true
  def init(_) do
    ensure_subscribers_table()

    case System.get_env("COMPANY_DATABASE_URL") do
      nil ->
        {:ok,
         %{memory: %{work: %{}, agents: %{}, bids: %{}, events: [], scoring: @default_scoring}}}

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
          attempt INTEGER NOT NULL DEFAULT 0,
          last_failure JSONB,
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
          "ALTER TABLE company.hive_work_items ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0",
          []
        )

        Postgrex.query!(
          db,
          "ALTER TABLE company.hive_work_items ADD COLUMN IF NOT EXISTS last_failure JSONB",
          []
        )

        Postgrex.query!(
          db,
          "CREATE UNIQUE INDEX IF NOT EXISTS hive_work_slug_idx ON company.hive_work_items ((payload->>'slug')) WHERE payload ? 'slug'",
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

        Postgrex.query!(
          db,
          """
          CREATE TABLE IF NOT EXISTS company.hive_scoring_config (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            confidence_weight DOUBLE PRECISION NOT NULL DEFAULT 1,
            benefit_weight DOUBLE PRECISION NOT NULL DEFAULT 1,
            cost_weight DOUBLE PRECISION NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
          )
          """,
          []
        )

        Postgrex.query!(
          db,
          "INSERT INTO company.hive_scoring_config (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING",
          []
        )

        {:ok, %{db: db, scoring: load_scoring(db)}}
    end
  end

  @impl true
  def handle_call({:enqueue, id, parts, metadata}, _from, %{memory: _memory} = state) do
    case work_payload(parts, metadata) do
      {:ok, payload} ->
        case {state.memory.work[id], find_slug(state.memory.work, payload["slug"])} do
          {nil, existing_id} when not is_nil(existing_id) and existing_id != id ->
            {:reply, {:error, :slug_taken}, state}

          {nil, nil} ->
            item =
              Map.merge(
                %{
                  id: id,
                  payload: payload,
                  state: "available",
                  claimed_by: nil,
                  attempt: 0,
                  last_failure: nil
                },
                identity(payload)
              )

            event =
              event(
                "work.created",
                id,
                %{"slug" => payload["slug"], "title" => payload["title"]},
                0
              )

            notify_subscribers(id)
            notify_event_subscribers(event)

            {:reply, {:ok, item},
             state
             |> put_in([:memory, :work, id], item)
             |> update_in([:memory, :events], &[event | &1])}

          {item, _existing_id} ->
            {:reply, {:ok, item}, state}
        end

      error ->
        {:reply, error, state}
    end
  end

  def handle_call({:enqueue, id, parts, metadata}, _from, %{db: db} = state) do
    case work_payload(parts, metadata) do
      {:ok, decoded_payload} ->
        payload = Jason.encode!(decoded_payload)

        created =
          transaction!(db, fn tx ->
            existing_slug =
              Postgrex.query!(
                tx,
                "SELECT id FROM company.hive_work_items WHERE payload->>'slug' = $1 AND id <> $2 LIMIT 1",
                [decoded_payload["slug"], id]
              )
              |> rows()

            if existing_slug != [],
              do: {:error, :slug_taken},
              else: do_enqueue(tx, id, payload, decoded_payload)
          end)

        case created do
          {:error, :slug_taken} ->
            {:reply, {:error, :slug_taken}, state}

          {created?, events} ->
            if created? do
              Postgrex.query!(db, "SELECT pg_notify($1, $2)", [@channel, id])
              Enum.each(events, &notify_event_subscribers/1)
              notify_subscribers(id)
            end

            {:reply, get_db(db, id), state}
        end

      error ->
        {:reply, error, state}
    end
  end

  def handle_call({:resolve_id, ref}, _from, %{memory: memory} = state) do
    resolved = if Map.has_key?(memory.work, ref), do: ref, else: find_slug(memory.work, ref)
    {:reply, if(resolved, do: {:ok, resolved}, else: {:error, :not_found}), state}
  end

  def handle_call({:resolve_id, ref}, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        "SELECT id FROM company.hive_work_items WHERE id = $1 OR payload->>'slug' = $1 ORDER BY CASE WHEN id = $1 THEN 0 ELSE 1 END LIMIT 1",
        [ref]
      )

    resolved =
      case result.rows do
        [[id]] -> id
        _ -> nil
      end

    {:reply, if(resolved, do: {:ok, resolved}, else: {:error, :not_found}), state}
  end

  def handle_call({:available, limit}, _from, %{memory: memory} = state) do
    now = DateTime.utc_now()

    expired =
      memory.work
      |> Enum.filter(fn {_id, item} ->
        lease = Map.get(item, :lease_expires_at)

        item.state == "claimed" and is_struct(lease, DateTime) and
          DateTime.compare(lease, now) == :lt
      end)

    {work, failure_events} =
      Enum.reduce(expired, {memory.work, []}, fn {id, item}, {items, events} ->
        failure = %{
          "agent_id" => item.claimed_by,
          "reason" => "lease_expired",
          "retry_state" => "available",
          "slug" => item["slug"]
        }

        failed =
          Map.merge(item, %{
            state: "available",
            claimed_by: nil,
            lease_expires_at: nil,
            last_failure: failure
          })

        event = event("engineering.failed", id, failure, item.attempt || 0)
        {Map.put(items, id, failed), [event | events]}
      end)

    bids =
      Enum.reduce(expired, memory.bids, fn {id, item}, acc ->
        Map.delete(acc, {id, item.claimed_by})
      end)

    items = work |> Map.values() |> Enum.filter(&(&1.state == "available")) |> Enum.take(limit)
    Enum.each(failure_events, &notify_event_subscribers/1)

    next_memory = %{
      memory
      | work: work,
        bids: bids,
        events: Enum.reverse(failure_events) ++ memory.events
    }

    {:reply, {:ok, items}, %{state | memory: next_memory}}
  end

  def handle_call({:available, limit}, _from, %{db: db} = state) do
    failure_events = transaction!(db, &recover_expired_db/1)
    Enum.each(failure_events, &notify_event_subscribers/1)

    result =
      Postgrex.query!(
        db,
        "SELECT id, payload, state, claimed_by, lease_expires_at, attempt, last_failure FROM company.hive_work_items WHERE state = 'available' ORDER BY created_at LIMIT $1",
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
        "SELECT event_id, topic, task_id, sender, occurred_at, payload, attempt FROM company.hive_events WHERE task_id = $1 ORDER BY occurred_at DESC LIMIT $2",
        [task_id, limit]
      )

    {:reply, {:ok, rows(result)}, state}
  end

  def handle_call(:agents, _from, %{memory: memory} = state),
    do: {:reply, {:ok, Map.values(memory.agents)}, state}

  def handle_call(:agents, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        "SELECT id, endpoint, capabilities, last_seen_at FROM company.hive_agents ORDER BY id",
        []
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
          "agent_id" => agent_id,
          "created_at" => DateTime.utc_now()
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

  def handle_call({:ranked_bids, work_id, limit}, _from, %{memory: memory} = state) do
    bids =
      memory.bids
      |> Enum.filter(fn {{id, _agent}, _bid} -> id == work_id end)
      |> Enum.map(fn {_key, bid} -> Map.put(bid, "score", score(bid, memory.scoring)) end)
      |> Enum.sort(&rank_order/2)
      |> Enum.take(limit)

    {:reply, {:ok, bids}, state}
  end

  def handle_call({:ranked_bids, work_id, limit}, _from, %{db: db, scoring: scoring} = state) do
    result =
      Postgrex.query!(
        db,
        """
        SELECT bid_id, work_id, agent_id, interested, confidence, approach,
          estimated_cost, expected_benefit, risk, proposal, created_at,
          CASE WHEN interested THEN
            POWER(GREATEST(confidence, 0.0001), $2) * POWER(GREATEST(expected_benefit, 0.0001), $3) /
            POWER(GREATEST(estimated_cost, 0.01), $4)
          ELSE 0 END AS score
        FROM company.hive_work_bids WHERE work_id = $1
        ORDER BY score DESC, created_at DESC
        LIMIT $5
        """,
        [
          work_id,
          scoring["confidence_weight"],
          scoring["benefit_weight"],
          scoring["cost_weight"],
          limit
        ]
      )

    {:reply, {:ok, rows(result)}, state}
  end

  def handle_call(:scoring, _from, %{memory: memory} = state),
    do: {:reply, {:ok, memory.scoring}, state}

  def handle_call(:scoring, _from, %{scoring: scoring} = state),
    do: {:reply, {:ok, scoring}, state}

  def handle_call({:set_scoring, config}, _from, %{memory: _memory} = state) do
    case normalize_scoring(config) do
      {:ok, scoring} -> {:reply, {:ok, scoring}, put_in(state.memory.scoring, scoring)}
      error -> {:reply, error, state}
    end
  end

  def handle_call({:set_scoring, config}, _from, %{db: db} = state) do
    case normalize_scoring(config) do
      {:ok, scoring} ->
        Postgrex.query!(
          db,
          "UPDATE company.hive_scoring_config SET confidence_weight = $1, benefit_weight = $2, cost_weight = $3, updated_at = now() WHERE id = TRUE",
          [scoring["confidence_weight"], scoring["benefit_weight"], scoring["cost_weight"]]
        )

        {:reply, {:ok, scoring}, %{state | scoring: scoring}}

      error ->
        {:reply, error, state}
    end
  end

  def handle_call({:allocate, work_id, lease_seconds}, _from, %{memory: memory} = state) do
    winner =
      memory.bids
      |> Enum.filter(fn {{id, _agent}, bid} -> id == work_id and bid["interested"] end)
      |> Enum.map(fn {_key, bid} -> Map.put(bid, "score", score(bid, memory.scoring)) end)
      |> Enum.sort(&rank_order/2)
      |> List.first()

    with %{state: "available"} = work <- memory.work[work_id],
         %{"agent_id" => agent_id} <- winner do
      attempt = (work.attempt || 0) + 1

      allocated =
        Map.merge(work, %{
          state: "claimed",
          claimed_by: agent_id,
          lease_expires_at: DateTime.add(DateTime.utc_now(), lease_seconds, :second),
          attempt: attempt
        })

      allocation =
        event(
          "work.allocated",
          work_id,
          %{
            "agent_id" => agent_id,
            "score" => score(winner, memory.scoring),
            "slug" => work["slug"]
          },
          attempt
        )

      started =
        event("work.started", work_id, %{"agent_id" => agent_id, "slug" => work["slug"]}, attempt)

      next = put_in(state.memory.work[work_id], allocated)
      next = update_in(next.memory.events, &[started, allocation | &1])
      notify_event_subscribers(allocation)
      notify_event_subscribers(started)
      {:reply, {:ok, Map.put(allocated, :allocated_to, agent_id)}, next}
    else
      nil -> {:reply, {:error, :work_not_found}, state}
      %{state: _} -> {:reply, {:error, :no_bids}, state}
      _ -> {:reply, {:error, :no_bids}, state}
    end
  end

  def handle_call({:allocate, work_id, lease_seconds}, _from, %{db: db, scoring: scoring} = state) do
    result =
      transaction!(db, fn tx ->
        winner =
          Postgrex.query!(
            tx,
            "SELECT agent_id, POWER(GREATEST(confidence, 0.0001), $2) * POWER(GREATEST(expected_benefit, 0.0001), $3) / POWER(GREATEST(estimated_cost, 0.01), $4) AS score FROM company.hive_work_bids WHERE work_id = $1 AND interested ORDER BY score DESC, created_at DESC LIMIT 1",
            [
              work_id,
              scoring["confidence_weight"],
              scoring["benefit_weight"],
              scoring["cost_weight"]
            ]
          )
          |> rows()
          |> List.first()

        case winner do
          nil ->
            {:error, :no_bids}

          %{"agent_id" => agent_id, "score" => score} ->
            updated =
              Postgrex.query!(
                tx,
                "UPDATE company.hive_work_items SET state = 'claimed', claimed_by = $2, lease_expires_at = now() + ($3 || ' seconds')::interval, attempt = attempt + 1, updated_at = now() WHERE id = $1 AND state = 'available' RETURNING id, payload, state, claimed_by, lease_expires_at, attempt, last_failure",
                [work_id, agent_id, Integer.to_string(lease_seconds)]
              )

            case rows(updated) do
              [item] ->
                slug = get_in(item, ["payload", "slug"])

                allocation =
                  event(
                    "work.allocated",
                    work_id,
                    %{"agent_id" => agent_id, "score" => score, "slug" => slug},
                    item["attempt"]
                  )

                started =
                  event(
                    "work.started",
                    work_id,
                    %{"agent_id" => agent_id, "slug" => slug},
                    item["attempt"]
                  )

                persist_event(tx, allocation)
                persist_event(tx, started)
                {:ok, Map.put(item, "allocated_to", agent_id), allocation, started}

              _ ->
                {:error, :unavailable}
            end
        end
      end)

    case result do
      {:ok, item, allocation, started} ->
        notify_event_subscribers(allocation)
        notify_event_subscribers(started)
        {:reply, {:ok, item}, state}

      error ->
        {:reply, error, state}
    end
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

  def handle_call({:claim, id, agent_id, lease_seconds}, _from, %{memory: memory} = state) do
    now = DateTime.utc_now()

    expired_owner =
      case memory.work[id] do
        %{state: "claimed"} = item ->
          lease = Map.get(item, :lease_expires_at)

          if is_struct(lease, DateTime) and DateTime.compare(lease, now) == :lt,
            do: Map.get(item, :claimed_by)

        _ ->
          nil
      end

    item = memory.work[id]

    if is_map(item) and (item.state == "available" or is_binary(expired_owner)) do
      attempt = (item.attempt || 0) + 1

      failure =
        if expired_owner,
          do: %{
            "agent_id" => expired_owner,
            "reason" => "lease_expired",
            "retry_state" => "reassigned",
            "reassigned_to" => agent_id,
            "slug" => item["slug"]
          },
          else: nil

      claimed =
        Map.merge(item, %{
          state: "claimed",
          claimed_by: agent_id,
          lease_expires_at: DateTime.add(now, lease_seconds, :second),
          attempt: attempt,
          last_failure: failure || item.last_failure
        })

      failure_event = if failure, do: event("engineering.failed", id, failure, item.attempt || 0)

      allocated =
        event("work.allocated", id, %{"agent_id" => agent_id, "slug" => item["slug"]}, attempt)

      started =
        event("work.started", id, %{"agent_id" => agent_id, "slug" => item["slug"]}, attempt)

      next =
        if expired_owner,
          do: put_in(state.memory.bids, Map.delete(state.memory.bids, {id, expired_owner})),
          else: state

      next =
        next
        |> put_in([:memory, :work, id], claimed)
        |> update_in(
          [:memory, :events],
          &([started, allocated] ++ if(failure_event, do: [failure_event], else: []) ++ &1)
        )

      if failure_event, do: notify_event_subscribers(failure_event)
      notify_event_subscribers(started)
      notify_event_subscribers(allocated)
      {:reply, {:ok, claimed}, next}
    else
      {:reply, {:error, :unavailable}, state}
    end
  end

  def handle_call({:claim, id, agent_id, lease_seconds}, _from, %{db: db} = state) do
    result =
      transaction!(db, fn tx ->
        previous =
          Postgrex.query!(
            tx,
            "SELECT claimed_by, attempt, payload FROM company.hive_work_items WHERE id = $1 AND state = 'claimed' AND lease_expires_at < now() FOR UPDATE",
            [id]
          )
          |> rows()
          |> List.first()

        failure_event =
          case previous do
            %{"claimed_by" => expired_owner, "attempt" => attempt, "payload" => payload}
            when is_binary(expired_owner) ->
              failure = %{
                "agent_id" => expired_owner,
                "reason" => "lease_expired",
                "retry_state" => "reassigned",
                "reassigned_to" => agent_id,
                "slug" => get_in(payload, ["slug"])
              }

              failed_event = event("engineering.failed", id, failure, attempt)

              Postgrex.query!(
                tx,
                "DELETE FROM company.hive_work_bids WHERE work_id = $1 AND agent_id = $2",
                [id, expired_owner]
              )

              Postgrex.query!(
                tx,
                "UPDATE company.hive_work_items SET state = 'available', claimed_by = NULL, lease_expires_at = NULL, last_failure = $2::jsonb, updated_at = now() WHERE id = $1",
                [id, Jason.encode!(failure)]
              )

              persist_event(tx, failed_event)
              {failed_event, payload}

            _ ->
              nil
          end

        result =
          Postgrex.query!(
            tx,
            "UPDATE company.hive_work_items SET state = 'claimed', claimed_by = $2, lease_expires_at = now() + ($3 || ' seconds')::interval, attempt = attempt + 1, updated_at = now() WHERE id = $1 AND state = 'available' RETURNING id, payload, state, claimed_by, lease_expires_at, attempt, last_failure",
            [id, agent_id, Integer.to_string(lease_seconds)]
          )

        case rows(result) do
          [item] ->
            slug = get_in(item, ["payload", "slug"])

            allocated =
              event(
                "work.allocated",
                id,
                %{"agent_id" => agent_id, "slug" => slug},
                item["attempt"]
              )

            started =
              event(
                "work.started",
                id,
                %{"agent_id" => agent_id, "slug" => slug},
                item["attempt"]
              )

            persist_event(tx, allocated)
            persist_event(tx, started)
            {:ok, item, allocated, started, failure_event}

          _ ->
            {:error, :unavailable}
        end
      end)

    case result do
      {:ok, _item, allocated, started, failure_event} ->
        if failure_event, do: notify_event_subscribers(elem(failure_event, 0))

        notify_event_subscribers(allocated)
        notify_event_subscribers(started)

      _ ->
        :ok
    end

    {:reply,
     case result do
       {:ok, item, _allocated, _started, _failure} -> {:ok, item}
       other -> other
     end, state}
  end

  def handle_call(
        {:complete, id, agent_id, final_state, result},
        _from,
        %{memory: memory} = state
      ) do
    result = result || %{}

    case memory.work[id] do
      %{claimed_by: ^agent_id} = item ->
        completed =
          Map.merge(item, %{
            state: final_state,
            result: result,
            claimed_by: nil,
            lease_expires_at: nil
          })

        topic =
          if final_state == "completed", do: "engineering.completed", else: "engineering.failed"

        event =
          event(
            topic,
            id,
            %{
              "agent_id" => agent_id,
              "state" => final_state,
              "result" => result,
              "slug" => item["slug"]
            },
            item.attempt || 0
          )

        notify_event_subscribers(event)

        next =
          state
          |> put_in([:memory, :work, id], completed)
          |> update_in([:memory, :events], &[event | &1])

        {:reply, {:ok, completed}, next}

      %{state: ^final_state, result: ^result, claimed_by: nil, attempt: attempt} = item ->
        topic =
          if final_state == "completed", do: "engineering.completed", else: "engineering.failed"

        completion_event =
          Enum.find(memory.events, fn existing ->
            payload = existing["payload"] || %{}

            existing["topic"] == topic && existing["task_id"] == id &&
              existing["attempt"] == attempt &&
              payload["agent_id"] == agent_id && payload["state"] == final_state &&
              payload["result"] == result
          end)

        if completion_event do
          notify_event_subscribers(completion_event)
          {:reply, {:ok, item}, state}
        else
          {:reply, {:error, :not_owner}, state}
        end

      _ ->
        {:reply, {:error, :not_owner}, state}
    end
  end

  def handle_call({:complete, id, agent_id, final_state, result}, _from, %{db: db} = state) do
    topic = if final_state == "completed", do: "engineering.completed", else: "engineering.failed"

    result =
      transaction!(db, fn tx ->
        updated =
          Postgrex.query!(
            tx,
            "UPDATE company.hive_work_items SET state = $3, result = $4::jsonb, claimed_by = NULL, lease_expires_at = NULL, updated_at = now() WHERE id = $1 AND claimed_by = $2 AND state = 'claimed' AND lease_expires_at >= now() RETURNING id, payload, state, claimed_by, lease_expires_at, attempt, last_failure, result",
            [id, agent_id, final_state, Jason.encode!(result || %{})]
          )

        case rows(updated) do
          [item] ->
            completion_event =
              event(
                topic,
                id,
                %{
                  "agent_id" => agent_id,
                  "state" => final_state,
                  "result" => result || %{},
                  "slug" => get_in(item, ["payload", "slug"])
                },
                item["attempt"]
              )

            persist_event(tx, completion_event)
            {:ok, item, completion_event}

          _ ->
            encoded_result = Jason.encode!(result || %{})

            completed =
              Postgrex.query!(
                tx,
                "SELECT id, payload, state, claimed_by, lease_expires_at, attempt, last_failure, result FROM company.hive_work_items WHERE id = $1 AND state = $2 AND claimed_by IS NULL AND result = $3::jsonb",
                [id, final_state, encoded_result]
              )

            case rows(completed) do
              [item] ->
                prior_event =
                  Postgrex.query!(
                    tx,
                    "SELECT event_id, topic, task_id, sender, occurred_at, payload, attempt FROM company.hive_events WHERE task_id = $1 AND topic = $2 AND attempt = $3 AND payload->>'agent_id' = $4 AND payload->>'state' = $5 AND payload->'result' = $6::jsonb ORDER BY occurred_at DESC LIMIT 1",
                    [id, topic, item["attempt"], agent_id, final_state, encoded_result]
                  )

                case rows(prior_event) do
                  [completion_event] -> {:ok, item, completion_event}
                  _ -> {:error, :not_owner}
                end

              _ ->
                {:error, :not_owner}
            end
        end
      end)

    case result do
      {:ok, item, completion_event} ->
        notify_event_subscribers(completion_event)
        {:reply, {:ok, item}, state}

      error ->
        {:reply, error, state}
    end
  end

  def handle_call({:heartbeat, id, agent_id, lease_seconds}, _from, %{memory: memory} = state) do
    now = DateTime.utc_now()

    case memory.work[id] do
      %{state: "claimed", claimed_by: ^agent_id} = item ->
        lease = Map.get(item, :lease_expires_at)

        if is_struct(lease, DateTime) and DateTime.compare(lease, now) == :lt do
          {:reply, {:error, :not_owner}, state}
        else
          renewed = Map.put(item, :lease_expires_at, DateTime.add(now, lease_seconds, :second))
          {:reply, {:ok, renewed}, put_in(state.memory.work[id], renewed)}
        end

      _ ->
        {:reply, {:error, :not_owner}, state}
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

  def handle_call({:get, id}, _from, %{memory: memory} = state) do
    case Map.fetch(memory.work, id) do
      {:ok, item} -> {:reply, {:ok, item}, state}
      :error -> {:reply, {:error, :not_found}, state}
    end
  end

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
             "SELECT id, payload, state, claimed_by, lease_expires_at, attempt, last_failure, result FROM company.hive_work_items WHERE id = $1",
             [id]
           )
         ) do
      [item] -> {:ok, item}
      _ -> {:error, :not_found}
    end
  end

  defp event(topic, task_id, payload, attempt) do
    %{
      "event_id" => "evt_" <> Base.encode16(:crypto.strong_rand_bytes(8), case: :lower),
      "topic" => topic,
      "task_id" => task_id,
      "sender" => System.get_env("HIVE_AGENT_ID", "hive-coordinator"),
      "occurred_at" => DateTime.utc_now(),
      "payload" => payload,
      "attempt" => attempt
    }
  end

  defp do_enqueue(tx, id, payload, decoded_payload) do
    inserted =
      Postgrex.query!(
        tx,
        "INSERT INTO company.hive_work_items (id, payload) VALUES ($1, $2::jsonb) ON CONFLICT DO NOTHING RETURNING id",
        [id, payload]
      )

    case inserted.rows do
      [] ->
        existing_id =
          Postgrex.query!(tx, "SELECT id FROM company.hive_work_items WHERE id = $1", [id]).rows

        if existing_id == [], do: {:error, :slug_taken}, else: {false, []}

      _ ->
        created =
          event(
            "work.created",
            id,
            %{"slug" => decoded_payload["slug"], "title" => decoded_payload["title"]},
            0
          )

        persist_event(tx, created)
        {true, [created]}
    end
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

  defp normalize_scoring(config) do
    scoring = %{
      "confidence_weight" => number(Map.get(config, "confidence_weight", 1)),
      "benefit_weight" => number(Map.get(config, "benefit_weight", 1)),
      "cost_weight" => number(Map.get(config, "cost_weight", 1))
    }

    if Enum.all?(scoring, fn {_key, value} -> value >= 0 end) and scoring["cost_weight"] > 0,
      do: {:ok, scoring},
      else: {:error, :invalid_scoring}
  end

  defp score(bid, scoring) do
    if bid["interested"] do
      :math.pow(max(bid["confidence"], 0.0001), scoring["confidence_weight"]) *
        :math.pow(max(bid["expected_benefit"], 0.0001), scoring["benefit_weight"]) /
        :math.pow(max(bid["estimated_cost"], 0.01), scoring["cost_weight"])
    else
      0.0
    end
  end

  defp rank_order(a, b) do
    case {a["score"], b["score"]} do
      {score, score} -> DateTime.compare(a["created_at"], b["created_at"]) == :gt
      {a_score, b_score} -> a_score > b_score
    end
  end

  defp random_id, do: Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)

  defp with_id(ref, fun) do
    case resolve_id(ref) do
      {:ok, id} -> fun.(id)
      error -> error
    end
  end

  defp recover_expired do
    GenServer.call(__MODULE__, {:available, 0})
  end

  defp allocate_after_expiry_recovery(ref, lease_seconds) do
    _ = recover_expired()
    with_id(ref, fn id -> GenServer.call(__MODULE__, {:allocate, id, lease_seconds}) end)
  end

  defp claim_after_expiry_recovery(ref, agent_id, lease_seconds) do
    _ = recover_expired()
    with_id(ref, fn id -> GenServer.call(__MODULE__, {:claim, id, agent_id, lease_seconds}) end)
  end

  defp complete_after_expiry_recovery(ref, agent_id, final_state, result) do
    _ = recover_expired()

    with_id(ref, fn id ->
      GenServer.call(__MODULE__, {:complete, id, agent_id, final_state, result})
    end)
  end

  defp heartbeat_after_expiry_recovery(ref, agent_id, lease_seconds) do
    _ = recover_expired()

    with_id(ref, fn id ->
      GenServer.call(__MODULE__, {:heartbeat, id, agent_id, lease_seconds})
    end)
  end

  defp work_payload(parts, metadata) do
    metadata = if is_map(metadata), do: metadata, else: %{}
    title = metadata["title"] || metadata["name"] || message_title(parts)
    slug = metadata["slug"] || slugify(title)

    if is_binary(title) and is_binary(slug) and Regex.match?(~r/^[a-z0-9]+(?:-[a-z0-9]+)*$/, slug) do
      payload = %{"parts" => parts, "title" => String.trim(title), "slug" => slug}

      # Worker tiering (hive/README.md "Worker tiers"): only a known tier is stored;
      # absent/unknown means "standard" to workers, so existing queued items are unaffected.
      case metadata["tier"] do
        tier when tier in @tiers -> {:ok, Map.put(payload, "tier", tier)}
        _ -> {:ok, payload}
      end
    else
      {:error, :invalid_slug}
    end
  end

  defp message_title(parts) do
    parts
    |> Enum.find_value("Untitled work", fn
      %{"text" => text} when is_binary(text) ->
        text |> String.split("\n", parts: 2) |> hd() |> String.trim()

      _ ->
        nil
    end)
    |> String.slice(0, 100)
  end

  defp slugify(title) do
    title
    |> String.downcase()
    |> String.replace(~r/[^a-z0-9]+/u, "-")
    |> String.trim("-")
    |> String.slice(0, 64)
    |> String.trim_trailing("-")
  end

  defp identity(payload), do: Map.take(payload, ["title", "slug"])

  defp find_slug(work, slug) do
    Enum.find_value(work, fn {id, item} ->
      if get_in(item, [:payload, "slug"]) == slug, do: id
    end)
  end

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

  defp recover_expired_db(tx) do
    expired =
      Postgrex.query!(
        tx,
        "SELECT id, claimed_by, attempt, payload FROM company.hive_work_items WHERE state = 'claimed' AND lease_expires_at < now() FOR UPDATE",
        []
      )
      |> rows()

    Enum.map(expired, fn %{
                           "id" => id,
                           "claimed_by" => agent_id,
                           "attempt" => attempt,
                           "payload" => payload
                         } ->
      failure = %{
        "agent_id" => agent_id,
        "reason" => "lease_expired",
        "retry_state" => "available",
        "slug" => get_in(payload, ["slug"])
      }

      failed_event = event("engineering.failed", id, failure, attempt)

      Postgrex.query!(
        tx,
        "DELETE FROM company.hive_work_bids WHERE work_id = $1 AND agent_id = $2",
        [id, agent_id]
      )

      Postgrex.query!(
        tx,
        "UPDATE company.hive_work_items SET state = 'available', claimed_by = NULL, lease_expires_at = NULL, last_failure = $2::jsonb, updated_at = now() WHERE id = $1 AND state = 'claimed' AND lease_expires_at < now()",
        [id, Jason.encode!(failure)]
      )

      persist_event(tx, failed_event)
      failed_event
    end)
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
    decoded =
      Enum.reduce(["payload", "last_failure", "result"], row, fn key, acc ->
        case Map.get(acc, key) do
          value when is_binary(value) -> Map.put(acc, key, Jason.decode!(value))
          _ -> acc
        end
      end)

    Map.merge(decoded, identity(decoded["payload"] || %{}))
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

  defp load_scoring(db) do
    case Postgrex.query!(
           db,
           "SELECT confidence_weight, benefit_weight, cost_weight FROM company.hive_scoring_config WHERE id = TRUE",
           []
         )
         |> rows()
         |> List.first() do
      %{"confidence_weight" => confidence, "benefit_weight" => benefit, "cost_weight" => cost} ->
        %{"confidence_weight" => confidence, "benefit_weight" => benefit, "cost_weight" => cost}

      _ ->
        @default_scoring
    end
  end
end
