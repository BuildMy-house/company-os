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

    claim_body = Jason.encode!(%{"agent_id" => "agent-1"})

    claimed =
      conn(:post, "/work/#{task_id}/claim", claim_body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert claimed["state"] == "claimed"

    complete_body =
      Jason.encode!(%{
        "agent_id" => "agent-1",
        "state" => "completed",
        "result" => %{"ok" => true}
      })

    completed =
      conn(:post, "/work/#{task_id}/complete", complete_body)
      |> Plug.Conn.put_req_header("content-type", "application/json")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert completed["state"] == "completed"

    assert {:ok, events} = Hive.Work.events(task_id)

    assert Enum.map(events, & &1["topic"]) == [
             "engineering.completed",
             "work.started",
             "work.allocated",
             "work.created"
           ]

    event_response =
      conn(:get, "/events?task_id=#{task_id}")
      |> Hive.Router.call(@opts)
      |> Map.fetch!(:resp_body)
      |> Jason.decode!()

    assert length(event_response["events"]) == 4
  end
end
