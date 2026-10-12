defmodule Hive.ReaperTest do
  use ExUnit.Case
  import Plug.Test

  @opts Hive.Router.init([])

  setup do
    Application.put_env(:hive, :max_attempts, 2)
    on_exit(fn -> Application.delete_env(:hive, :max_attempts) end)
    :ok
  end

  test "reaps an expired lease without any other caller and redispatches it" do
    id = create_task!("reap-redispatch")
    bid!(id, "ghost")
    assert allocate!(id, 1)["claimed_by"] == "ghost"

    :ok = Hive.Work.subscribe(self(), "watcher")
    Process.sleep(1100)

    assert {:ok, %{redispatched: [^id], escalated: []}} = Hive.Work.reap()
    assert_receive {:hive_work_available, ^id}

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "available"

    {:ok, events} = Hive.Work.events(id)
    failed = Enum.find(events, &(&1["topic"] == "engineering.failed"))
    assert failed["payload"]["reason"] == "lease_expired"
    assert failed["payload"]["retry_state"] == "available"
    refute Enum.any?(events, &(&1["topic"] == "work.escalated"))

    assert {:ok, %{redispatched: [], escalated: []}} = Hive.Work.reap()
  end

  test "escalates instead of redispatching once max attempts are spent" do
    id = create_task!("reap-escalate")

    for agent <- ["a1", "a2"] do
      bid!(id, agent)
      assert allocate!(id, 1)["claimed_by"] == agent
      Process.sleep(1100)
      assert {:ok, _} = Hive.Work.reap()
    end

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "escalated"

    {:ok, events} = Hive.Work.events(id)
    escalated = Enum.find(events, &(&1["topic"] == "work.escalated"))
    assert escalated["payload"]["reason"] == "max_attempts_exceeded"
    assert escalated["payload"]["max_attempts"] == 2
    assert escalated["attempt"] == 2

    assert {:ok, available} = Hive.Work.available(100)
    refute Enum.any?(available, fn item -> item[:id] == id or item["id"] == id end)
  end

  test "a heartbeating lease is not reaped" do
    id = create_task!("reap-alive")
    bid!(id, "busy")
    assert allocate!(id, 2)["claimed_by"] == "busy"

    assert {:ok, %{redispatched: [], escalated: []}} = Hive.Work.reap()
    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "claimed"
  end

  defp state_of(work), do: work[:state] || work["state"]

  defp post_json(path, body) do
    conn(:post, path, Jason.encode!(body))
    |> Plug.Conn.put_req_header("content-type", "application/json")
    |> Hive.Router.call(@opts)
  end

  defp create_task!(text) do
    post_json("/", %{
      "jsonrpc" => "2.0",
      "id" => 1,
      "method" => "message/send",
      "params" => %{"message" => %{"parts" => [%{"text" => text}]}}
    })
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
    |> get_in(["result", "id"])
  end

  defp bid!(id, agent) do
    post_json("/work/#{id}/bids", %{
      "agent_id" => agent,
      "interested" => true,
      "confidence" => 0.9,
      "approach" => "test",
      "estimated_cost" => 1,
      "expected_benefit" => 5
    })
  end

  defp allocate!(id, lease_seconds) do
    post_json("/work/#{id}/allocate", %{"lease_seconds" => lease_seconds})
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
  end
end
