defmodule Hive.Work do
  @moduledoc false

  use GenServer

  @channel "hive_work"

  def start_link(_), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)

  def enqueue(id, parts), do: GenServer.call(__MODULE__, {:enqueue, id, parts})
  def available(limit \\ 10), do: GenServer.call(__MODULE__, {:available, limit})
  def register_agent(agent), do: GenServer.call(__MODULE__, {:register_agent, agent})

  def claim(id, agent_id, lease_seconds),
    do: GenServer.call(__MODULE__, {:claim, id, agent_id, lease_seconds})

  def complete(id, agent_id, state, result),
    do: GenServer.call(__MODULE__, {:complete, id, agent_id, state, result})

  def get(id), do: GenServer.call(__MODULE__, {:get, id})

  @impl true
  def init(_) do
    case System.get_env("COMPANY_DATABASE_URL") do
      nil ->
        {:ok, %{memory: %{work: %{}, agents: %{}}}}

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
          "CREATE INDEX IF NOT EXISTS hive_work_available_idx ON company.hive_work_items (state, created_at)",
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
    item = %{id: id, payload: %{"parts" => parts}, state: "available", claimed_by: nil}
    {:reply, {:ok, item}, put_in(state.memory.work[id], item)}
  end

  def handle_call({:enqueue, id, parts}, _from, %{db: db} = state) do
    payload = Jason.encode!(%{"parts" => parts})

    Postgrex.query!(
      db,
      "INSERT INTO company.hive_work_items (id, payload) VALUES ($1, $2::jsonb) ON CONFLICT (id) DO NOTHING",
      [id, payload]
    )

    Postgrex.query!(db, "SELECT pg_notify($1, $2)", [@channel, id])
    {:reply, get_db(db, id), state}
  end

  def handle_call({:available, limit}, _from, %{memory: memory} = state) do
    items =
      memory.work |> Map.values() |> Enum.filter(&(&1.state == "available")) |> Enum.take(limit)

    {:reply, {:ok, items}, state}
  end

  def handle_call({:available, limit}, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        "UPDATE company.hive_work_items SET state = 'available', claimed_by = NULL, lease_expires_at = NULL, updated_at = now() WHERE state = 'claimed' AND lease_expires_at < now(); SELECT id, payload, state, claimed_by FROM company.hive_work_items WHERE state = 'available' ORDER BY created_at LIMIT $1",
        [limit]
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

  def handle_call({:claim, id, agent_id, _lease_seconds}, _from, %{memory: memory} = state) do
    case memory.work[id] do
      %{state: "available"} = item ->
        claimed = %{item | state: "claimed", claimed_by: agent_id}
        {:reply, {:ok, claimed}, put_in(state.memory.work[id], claimed)}

      _ ->
        {:reply, {:error, :unavailable}, state}
    end
  end

  def handle_call({:claim, id, agent_id, lease_seconds}, _from, %{db: db} = state) do
    result =
      Postgrex.query!(
        db,
        "UPDATE company.hive_work_items SET state = 'claimed', claimed_by = $2, lease_expires_at = now() + ($3 || ' seconds')::interval, updated_at = now() WHERE id = $1 AND (state = 'available' OR (state = 'claimed' AND lease_expires_at < now())) RETURNING id, payload, state, claimed_by",
        [id, agent_id, lease_seconds]
      )

    {:reply,
     case rows(result) do
       [item] -> {:ok, item}
       _ -> {:error, :unavailable}
     end, state}
  end

  def handle_call(
        {:complete, id, agent_id, final_state, result},
        _from,
        %{memory: memory} = state
      ) do
    case memory.work[id] do
      %{claimed_by: ^agent_id} = item ->
        completed = Map.merge(item, %{state: final_state, result: result})
        {:reply, {:ok, completed}, put_in(state.memory.work[id], completed)}

      _ ->
        {:reply, {:error, :not_owner}, state}
    end
  end

  def handle_call({:complete, id, agent_id, final_state, result}, _from, %{db: db} = state) do
    updated =
      Postgrex.query!(
        db,
        "UPDATE company.hive_work_items SET state = $3, result = $4::jsonb, updated_at = now() WHERE id = $1 AND claimed_by = $2 RETURNING id, payload, state, claimed_by, result",
        [id, agent_id, final_state, Jason.encode!(result || %{})]
      )

    {:reply,
     case rows(updated) do
       [item] -> {:ok, item}
       _ -> {:error, :not_owner}
     end, state}
  end

  def handle_call({:get, id}, _from, %{memory: memory} = state),
    do: {:reply, Map.get(memory.work, id), state}

  def handle_call({:get, id}, _from, %{db: db} = state), do: {:reply, get_db(db, id), state}

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

  defp rows(%Postgrex.Result{columns: columns, rows: values}),
    do: Enum.map(values, &Map.new(Enum.zip(columns, &1)))

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
