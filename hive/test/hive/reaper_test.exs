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

  describe "re-offering unbid work" do
    setup do
      Application.put_env(:hive, :reoffer_after_seconds, 1)
      Application.put_env(:hive, :max_reoffers, 2)

      on_exit(fn ->
        Application.delete_env(:hive, :reoffer_after_seconds)
        Application.delete_env(:hive, :max_reoffers)
      end)
    end

    test "relaxes tier, adds incentive, clears declines and escalates when spent" do
      id = create_task!("reoffer-unbid", %{"tier" => "power"})
      decline!(id, "picky")
      Process.sleep(1100)

      assert id in reaped(:reoffered)
      payload = payload_of(id)
      assert payload["tier"] == "standard"
      assert payload["reoffers"] == 1
      assert payload["incentive"] == 1.5

      {:ok, events} = Hive.Work.events(id)
      reoffer = Enum.find(events, &(&1["topic"] == "work.reoffered"))
      assert reoffer["payload"]["tier_from"] == "power"
      assert reoffer["payload"]["tier_to"] == "standard"

      # the decline was cleared, so the same worker can look again
      assert {:ok, available} = Hive.Work.available_for("picky", 100)
      assert Enum.any?(available, fn item -> (item[:id] || item["id"]) == id end)

      # not stale again straight away
      refute id in reaped(:reoffered)

      Process.sleep(1100)
      assert id in reaped(:reoffered)
      assert payload_of(id)["tier"] == "cheap"
      assert payload_of(id)["incentive"] == 2.0

      Process.sleep(1100)
      assert id in reaped(:escalated)
      {:ok, work} = Hive.Work.get(id)
      assert state_of(work) == "escalated"

      {:ok, events} = Hive.Work.events(id)
      escalated = Enum.find(events, &(&1["topic"] == "work.escalated"))
      assert escalated["payload"]["reason"] == "no_bids_after_reoffers"
    end

    test "work with an interested bid is left alone" do
      id = create_task!("reoffer-wanted", %{"tier" => "power"})
      bid!(id, "keen")
      Process.sleep(1100)

      refute id in reaped(:reoffered)
      assert payload_of(id)["tier"] == "power"

      # claim it so it does not linger in the shared available list
      assert allocate!(id, 60)["claimed_by"] == "keen"
    end
  end

  defp reaped(key) do
    {:ok, summary} = Hive.Work.reap()
    Map.fetch!(summary, key)
  end

  defp payload_of(id) do
    {:ok, work} = Hive.Work.get(id)
    work[:payload] || work["payload"]
  end

  defp decline!(id, agent) do
    post_json("/work/#{id}/bids", %{
      "agent_id" => agent,
      "interested" => false,
      "confidence" => 0.1,
      "approach" => "no",
      "estimated_cost" => 1,
      "expected_benefit" => 0
    })
  end

  defp state_of(work), do: work[:state] || work["state"]

  defp post_json(path, body) do
    conn(:post, path, Jason.encode!(body))
    |> Plug.Conn.put_req_header("content-type", "application/json")
    |> Hive.Router.call(@opts)
  end

  defp create_task!(text, metadata \\ %{}) do
    post_json("/", %{
      "jsonrpc" => "2.0",
      "id" => 1,
      "method" => "message/send",
      "params" => %{"message" => %{"parts" => [%{"text" => text}], "metadata" => metadata}}
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
