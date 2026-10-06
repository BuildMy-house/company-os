defmodule Hive.RouterTest do
  use ExUnit.Case
  import Plug.Test

  @opts Hive.Router.init([])

  setup do
    Application.put_env(:hive, :engineering_submit, fn parts ->
      {:ok, %{"id" => "engineering_1", "status" => %{"state" => "working"}, "parts" => parts}}
    end)

    Application.put_env(:hive, :engineering_get, fn "engineering_1" ->
      {:ok, %{"id" => "engineering_1", "status" => %{"state" => "completed"}}}
    end)

    on_exit(fn ->
      Application.delete_env(:hive, :engineering_submit)
      Application.delete_env(:hive, :engineering_get)
    end)

    :ok
  end

  test "publishes an agent card" do
    conn = conn(:get, "/.well-known/agent-card.json") |> Hive.Router.call(@opts)
    assert conn.status == 200
    assert Jason.decode!(conn.resp_body)["name"] == "buildmy.house Hive"
  end

  test "creates and retrieves an A2A task" do
    body =
      Jason.encode!(%{
        "jsonrpc" => "2.0",
        "id" => 1,
        "method" => "message/send",
        "params" => %{"message" => %{"parts" => [%{"text" => "test"}]}}
      })

    conn =
      conn(:post, "/", body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)

    response = Jason.decode!(conn.resp_body)
    task_id = response["result"]["id"]

    task =
      conn(:get, "/tasks/#{task_id}")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert conn.status == 200
    assert task["status"]["state"] == "available"
    assert task["name"] == "test"
    assert task["slug"] == "test"
    assert task["metadata"]["remote"] == nil

    by_slug =
      conn(:get, "/tasks/test")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert by_slug["id"] == task_id
    assert by_slug["slug"] == "test"
  end

  test "rejects duplicate and invalid human-readable slugs" do
    submit = fn params ->
      conn(
        :post,
        "/",
        Jason.encode!(%{
          "jsonrpc" => "2.0",
          "id" => 1,
          "method" => "message/send",
          "params" => params
        })
      )
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
    end

    message = %{"message" => %{"parts" => [%{"text" => "Unique human task"}]}}
    assert submit.(message).status == 200
    assert submit.(message).status == 409

    invalid = put_in(message, ["metadata"], %{"slug" => "Bad Slug"})
    assert submit.(invalid).status == 422
  end

  test "lists registered agents with endpoint and capabilities" do
    :ok =
      Hive.Work.register_agent(%{
        "id" => "listed-worker",
        "endpoint" => "http://listed-worker:8001",
        "capabilities" => %{"modes" => ["bid", "execute"]}
      })

    conn = conn(:get, "/agents") |> Hive.Router.call(@opts)
    assert conn.status == 200
    agents = Jason.decode!(conn.resp_body)["agents"]
    assert %{"endpoint" => "http://listed-worker:8001"} = Enum.find(agents, &(&1["id"] == "listed-worker"))
  end

  test "bids and lifecycle events accept the readable slug" do
    task_id = create_task!("Readable work slug")
    slug = "readable-work-slug"

    submit_bid!(slug, "worker", 0.8, 1, 3)
    allocated = allocate!(slug, 900)
    assert allocated["id"] == task_id
    assert allocated["payload"]["slug"] == slug

    {:ok, events} = Hive.Work.events(slug)
    assert Enum.map(events, & &1["topic"]) == ["work.started", "work.allocated", "work.created"]
    assert hd(events)["payload"]["slug"] == slug
  end

  test "restores an A2A task read from durable work" do
    task_id = "task_" <> Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)
    assert {:ok, _} = Hive.Work.enqueue(task_id, [%{"text" => "durable"}])

    conn = conn(:get, "/tasks/#{task_id}") |> Hive.Router.call(@opts)
    task = Jason.decode!(conn.resp_body)

    assert conn.status == 200
    assert task["id"] == task_id
    assert task["status"]["state"] == "available"
    assert task["metadata"]["remote"] == nil
  end

  test "an agent claims and completes queued work" do
    body =
      Jason.encode!(%{
        "jsonrpc" => "2.0",
        "id" => 3,
        "method" => "message/send",
        "params" => %{"message" => %{"parts" => [%{"text" => "queued"}]}}
      })

    response =
      conn(:post, "/", body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    task_id = response["result"]["id"]

    for {agent_id, confidence, benefit, cost} <- [{"slow", 0.9, 5, 2}, {"fast", 0.8, 4, 1}] do
      bid =
        conn(
          :post,
          "/work/#{task_id}/bids",
          Jason.encode!(%{
            "agent_id" => agent_id,
            "confidence" => confidence,
            "approach" => "test",
            "estimated_cost" => cost,
            "expected_benefit" => benefit
          })
        )
        |> Plug.Conn.put_req_header("content-type", "application/json")
        |> Hive.Router.call(@opts)

      assert bid.status == 201
    end

    ranked =
      conn(:get, "/work/#{task_id}/bids")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert Enum.map(ranked["bids"], & &1["agent_id"]) == ["fast", "slow"]

    claim_body = Jason.encode!(%{"agent_id" => "agent-1"})

    claimed =
      conn(:post, "/work/#{task_id}/claim", claim_body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert claimed["state"] == "claimed"

    heartbeat_body = Jason.encode!(%{"agent_id" => "agent-1", "lease_seconds" => 900})

    heartbeat =
      conn(:post, "/work/queued/heartbeat", heartbeat_body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert heartbeat["state"] == "claimed"

    claimed_task =
      conn(:get, "/tasks/queued")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert claimed_task["status"]["claimed_by"] == "agent-1"
    assert claimed_task["status"]["attempt"] == 1

    complete_body =
      Jason.encode!(%{
        "agent_id" => "agent-1",
        "state" => "completed",
        "result" => %{"ok" => true}
      })

    completed =
      conn(:post, "/work/queued/complete", complete_body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert completed["state"] == "completed"
    assert completed["claimed_by"] == nil
    assert completed["lease_expires_at"] == nil

    done_task =
      conn(:get, "/tasks/queued")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert done_task["status"]["state"] == "completed"
    assert done_task["status"]["claimed_by"] == nil

    assert {:ok, events} = Hive.Work.events("queued")

    assert Enum.map(events, & &1["topic"]) == [
             "engineering.completed",
             "work.started",
             "work.allocated",
             "work.created"
           ]

    assert {:ok, _already_completed} =
             Hive.Work.complete("queued", "agent-1", "completed", %{"ok" => true})

    assert {:ok, retry_events} = Hive.Work.events("queued")
    assert Enum.map(retry_events, & &1["topic"]) == Enum.map(events, & &1["topic"])

    assert {:error, :not_owner} =
             Hive.Work.complete("queued", "another-agent", "completed", %{"ok" => true})

    event_response =
      conn(:get, "/events?task_id=#{task_id}")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert length(event_response["events"]) == 4

    event_id = hd(event_response["events"])["event_id"]

    ack =
      conn(:post, "/events/#{event_id}/ack", Jason.encode!(%{"consumer_id" => "hermees"}))
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert ack["acknowledged"] == true
  end

  test "allocates available work to the highest-ranked bidder" do
    body =
      Jason.encode!(%{
        "jsonrpc" => "2.0",
        "id" => 4,
        "method" => "message/send",
        "params" => %{"message" => %{"parts" => [%{"text" => "allocate"}]}}
      })

    task_id =
      conn(:post, "/", body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()
      |> get_in(["result", "id"])

    for {agent_id, confidence, benefit, cost} <- [{"low", 0.5, 2, 2}, {"high", 0.9, 5, 1}] do
      conn(
        :post,
        "/work/#{task_id}/bids",
        Jason.encode!(%{
          "agent_id" => agent_id,
          "confidence" => confidence,
          "approach" => "test",
          "estimated_cost" => cost,
          "expected_benefit" => benefit
        })
      )
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
    end

    allocated =
      conn(:post, "/work/#{task_id}/allocate", Jason.encode!(%{"lease_seconds" => 900}))
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert allocated["claimed_by"] == "high"
    assert allocated["allocated_to"] == "high"

    task =
      conn(:get, "/tasks/#{task_id}")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert task["status"]["state"] == "claimed"
  end

  test "prefers the most recent bid when scores tie" do
    task_id = create_task!("tie-break")

    submit_bid!(task_id, "first", 0.8, 1, 1)
    submit_bid!(task_id, "second", 0.8, 1, 1)

    assert allocate!(task_id, 900)["claimed_by"] == "second"
  end

  test "a resubmitted bid wins ties with its refreshed timestamp" do
    task_id = create_task!("upsert-recency")

    submit_bid!(task_id, "early", 0.8, 1, 1)
    submit_bid!(task_id, "later", 0.8, 1, 1)
    submit_bid!(task_id, "early", 0.8, 1, 1)

    assert allocate!(task_id, 900)["claimed_by"] == "early"
  end

  test "purges the expired assignee bid and reassigns the work" do
    task_id = create_task!("stale-bid")

    submit_bid!(task_id, "stale", 0.9, 1, 5)
    assert allocate!(task_id, 1)["claimed_by"] == "stale"

    Process.sleep(1100)
    assert {:ok, _} = Hive.Work.available(10)

    task =
      conn(:get, "/tasks/stale-bid")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert task["status"]["attempt"] == 1
    assert task["status"]["last_failure"]["reason"] == "lease_expired"
    {:ok, events} = Hive.Work.events("stale-bid")
    assert Enum.any?(events, &(&1["topic"] == "engineering.failed" and &1["attempt"] == 1))

    {:ok, bids} = Hive.Work.ranked_bids(task_id)
    refute Enum.any?(bids, &(&1["agent_id"] == "stale"))

    submit_bid!(task_id, "fresh", 0.8, 1, 4)
    assert allocate!(task_id, 900)["claimed_by"] == "fresh"
  end

  test "claiming expired work purges the previous holder's bid" do
    task_id = create_task!("claim-expired")

    submit_bid!(task_id, "stale", 0.9, 1, 5)
    assert allocate!(task_id, 1)["claimed_by"] == "stale"

    Process.sleep(1100)

    claimed = post_json("/work/#{task_id}/claim", %{"agent_id" => "fresh"})
    assert claimed.status == 200

    {:ok, bids} = Hive.Work.ranked_bids(task_id)
    refute Enum.any?(bids, &(&1["agent_id"] == "stale"))

    {:ok, events} = Hive.Work.events(task_id)

    assert Enum.map(events, & &1["topic"]) == [
             "work.started",
             "work.allocated",
             "engineering.failed",
             "work.started",
             "work.allocated",
             "work.created"
           ]
  end

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

  defp submit_bid!(work_id, agent_id, confidence, cost, benefit) do
    conn =
      post_json("/work/#{work_id}/bids", %{
        "agent_id" => agent_id,
        "confidence" => confidence,
        "approach" => "test",
        "estimated_cost" => cost,
        "expected_benefit" => benefit
      })

    assert conn.status == 201
  end

  defp allocate!(work_id, lease_seconds) do
    post_json("/work/#{work_id}/allocate", %{"lease_seconds" => lease_seconds})
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
  end
end
