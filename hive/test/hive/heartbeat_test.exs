defmodule Hive.HeartbeatTest do
  use ExUnit.Case

  test "heartbeat payload carries only counts and uptime" do
    payload = Hive.Heartbeat.heartbeat_payload("heartbeat", 1_000, 61_000, 3)
    assert payload == %{"event" => "heartbeat", "uptime_s" => 60, "registered_agents" => 3}
  end

  test "telemetry envelope has the shared service/environment/role tags" do
    event = Hive.Telemetry.build_event(%{"event" => "startup"})
    assert event["role"] == "coordinator"
    assert is_binary(event["service"])
    assert is_binary(event["environment"])
    assert event["event"] == "startup"
    assert {:ok, _, _} = DateTime.from_iso8601(event["_time"])
  end

  test "emit is a no-op without a token" do
    System.delete_env("AXIOM_TOKEN")
    assert :ok == Hive.Telemetry.emit(%{"event" => "x"})
  end
end
