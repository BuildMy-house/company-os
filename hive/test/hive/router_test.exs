defmodule Hive.RouterTest do
  use ExUnit.Case, async: true
  import Plug.Test

  @opts Hive.Router.init([])

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
  end
end
