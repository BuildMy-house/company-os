defmodule Hive.CancelTest do
  # Terminal-cancel: valid from available/claimed/escalated; 404 unknown id; 409
  # already terminal; cancelled items leave the available feed and bidding. Runs on
  # the in-memory store, or on a dedicated *_test Postgres when COMPANY_DATABASE_URL
  # is set (see hive/README.md "Test database safety").
  use ExUnit.Case
  import Plug.Test

  @opts Hive.Router.init([])

  setup do
    on_exit(fn -> Hive.Work.unsubscribe(self()) end)
    :ok
  end

  test "cancels claimed work: stores reason and cancelled_at, releases lease" do
    id = create_task!("cancel-claimed")
    bid!(id, "claimer", 0.9, 1.0, 9)
    assert allocate!(id)["claimed_by"] == "claimer"

    conn = post_json("/work/#{id}/cancel", %{"reason" => "duplicate request"})
    assert conn.status == 200

    work = Jason.decode!(conn.resp_body)
    assert {work["state"], work["claimed_by"]} == {"cancelled", nil}
    assert work["result"]["reason"] == "duplicate request"
    assert work["result"]["cancelled_at"]

    {:ok, events} = Hive.Work.events(id)
    cancelled = Enum.find(events, &(&1["topic"] == "work.cancelled"))
    assert cancelled["payload"]["reason"] == "duplicate request"
  end

  test "cancels available work: removed from available feed and bids are dropped" do
    id = create_task!("cancel-available")
    bid!(id, "bidder", 0.9, 1.0, 9)
    assert state_of(id) == "available"

    assert post_json("/work/#{id}/cancel", %{"reason" => "superseded"}).status == 200
    assert state_of(id) == "cancelled"

    refute id in Enum.map(get_json("/work?limit=100")["work"], & &1["id"])
    assert post_json("/work/#{id}/bids", %{"agent_id" => "late-bidder"}).status == 404
    assert get_json("/work/#{id}/bids")["bids"] == []
    assert post_json("/work/#{id}/allocate", %{"lease_seconds" => 900}).status == 409
  end

  test "cancels escalated work" do
    Application.put_env(:hive, :max_attempts, 1)

    on_exit(fn -> Application.delete_env(:hive, :max_attempts) end)

    id = create_task!("cancel-escalated")
    bid!(id, "ghost", 0.9, 0.1, 5)
    assert allocate!(id, 1)["claimed_by"] == "ghost"
    Process.sleep(1100)
    assert {:ok, %{redispatched: [], escalated: [^id]}} = Hive.Work.reap()
    assert state_of(id) == "escalated"

    conn = post_json("/work/#{id}/cancel", nil)
    assert conn.status == 200
    assert Jason.decode!(conn.resp_body)["state"] == "cancelled"
  end

  test "404 for unknown work id" do
    assert post_json("/work/task_does_not_exist/cancel", nil).status == 404
  end

  test "409 when already cancelled or completed" do
    id = create_task!("cancel-twice")
    assert post_json("/work/#{id}/cancel", %{"reason" => "first"}).status == 200
    assert post_json("/work/#{id}/cancel", %{"reason" => "second"}).status == 409

    id = create_task!("cancel-completed")
    bid!(id, "worker", 0.9, 0.1, 5)
    assert allocate!(id)["claimed_by"] == "worker"

    assert post_json("/work/#{id}/complete", %{
             "agent_id" => "worker",
             "state" => "completed",
             "result" => feedback_result("done")
           }).status == 200

    assert post_json("/work/#{id}/cancel", %{"reason" => "too late"}).status == 409
  end

  defp state_of(id) do
    {:ok, work} = Hive.Work.get(id)
    work[:state] || work["state"]
  end

  defp get_json(path),
    do: conn(:get, path) |> Hive.Router.call(@opts) |> Map.fetch!(:resp_body) |> Jason.decode!()

  defp post_json(path, body) do
    conn(:post, path, if(body, do: Jason.encode!(body), else: ""))
    |> Plug.Conn.put_req_header("content-type", "application/json")
    |> Hive.Router.call(@opts)
  end

  defp create_task!(slug) do
    post_json("/", %{
      "jsonrpc" => "2.0",
      "id" => 1,
      "method" => "message/send",
      "params" => %{
        "message" => %{"parts" => [%{"text" => slug}]},
        "metadata" => %{"slug" => slug}
      }
    })
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
    |> get_in(["result", "id"])
  end

  defp bid!(work_id, agent_id, confidence, cost, benefit) do
    response =
      post_json("/work/#{work_id}/bids", %{
        "agent_id" => agent_id,
        "confidence" => confidence,
        "approach" => "test",
        "estimated_cost" => cost,
        "expected_benefit" => benefit
      })

    assert response.status == 201
  end

  defp allocate!(work_id, lease_seconds \\ 900) do
    post_json("/work/#{work_id}/allocate", %{"lease_seconds" => lease_seconds})
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
  end

  defp feedback_result(output) do
    %{
      "output" => output,
      "feedback" => %{
        "calibration" => %{
          "estimated_cost" => 1,
          "actual_cost" => 1,
          "estimated_benefit" => 1,
          "actual_benefit" => 1
        },
        "friction" => ""
      }
    }
  end
end
