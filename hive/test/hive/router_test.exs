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
    assert task["status"]["state"] == "submitted"
    assert task["metadata"]["remote"]["status"]["state"] == "completed"
  end
end
