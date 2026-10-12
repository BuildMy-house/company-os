defmodule Hive.Reaper do
  @moduledoc false

  # Periodically recovers work whose lease expired with no heartbeat. Lease recovery
  # used to run only as a side effect of someone calling available/claim/heartbeat, so
  # an abandoned task could sit `claimed` (invisible to every worker) until then.

  use GenServer
  require Logger

  @default_interval_ms 30_000

  def start_link(_), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)

  def interval_ms do
    Application.get_env(:hive, :reaper_interval_ms) ||
      "HIVE_REAPER_INTERVAL_MS"
      |> System.get_env("#{@default_interval_ms}")
      |> String.to_integer()
  end

  @impl true
  def init(_) do
    schedule()
    {:ok, %{}}
  end

  @impl true
  def handle_info(:reap, state) do
    try do
      case Hive.Work.reap() do
        {:ok, %{redispatched: [], escalated: []}} ->
          :ok

        {:ok, summary} ->
          Logger.warning("hive reaper: #{inspect(summary)}")
      end
    catch
      kind, reason -> Logger.error("hive reaper failed: #{Exception.format(kind, reason)}")
    end

    schedule()
    {:noreply, state}
  end

  def handle_info(_message, state), do: {:noreply, state}

  # An interval of 0 or less disables the periodic sweep (reap/0 still works on demand).
  defp schedule do
    case interval_ms() do
      ms when ms > 0 -> Process.send_after(self(), :reap, ms)
      _ -> :ok
    end
  end
end
