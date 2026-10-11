defmodule Hive.Heartbeat do
  @moduledoc """
  Emits a `startup` event once and a `heartbeat` event every
  HIVE_HEARTBEAT_MS (default 5 minutes) to Axiom via Hive.Telemetry, so
  "is the coordinator alive" is answerable from the bmh-company dataset.
  Fail-open: Hive.Telemetry is a no-op without AXIOM_TOKEN and a broken
  Axiom call never reaches this process. Payloads carry only counts and
  uptime, never task contents.
  """
  use GenServer

  @default_interval_ms 300_000

  def start_link(_), do: GenServer.start_link(__MODULE__, [], name: __MODULE__)

  @doc false
  def heartbeat_payload(event, started_ms, now_ms, agent_count) do
    %{
      "event" => event,
      "uptime_s" => div(now_ms - started_ms, 1000),
      "registered_agents" => agent_count
    }
  end

  @impl true
  def init(_) do
    started = System.monotonic_time(:millisecond)
    Hive.Telemetry.emit(heartbeat_payload("startup", started, started, 0))
    schedule()
    {:ok, %{started: started}}
  end

  @impl true
  def handle_info(:beat, %{started: started} = state) do
    Hive.Telemetry.emit(
      heartbeat_payload("heartbeat", started, System.monotonic_time(:millisecond), agent_count())
    )

    schedule()
    {:noreply, state}
  end

  def handle_info(_other, state), do: {:noreply, state}

  defp schedule do
    interval =
      case Integer.parse(System.get_env("HIVE_HEARTBEAT_MS", "")) do
        {ms, ""} when ms > 0 -> ms
        _ -> @default_interval_ms
      end

    Process.send_after(self(), :beat, interval)
  end

  defp agent_count do
    case Hive.Work.agents() do
      {:ok, list} when is_list(list) -> length(list)
      _ -> 0
    end
  catch
    _, _ -> 0
  end
end
