defmodule Hive.EmptyCompletionTest do
  # A "completed" report with zero tool calls and a blank output is not success: the item
  # fails with reason empty_completion and returns to the retry pipeline exactly like a
  # lease_expired failure. Runs on the in-memory store, or on a dedicated *_test Postgres
  # when COMPANY_DATABASE_URL is set (see hive/README.md "Test database safety").
  use ExUnit.Case
  import Plug.Test

  @opts Hive.Router.init([])

  test "an empty completion fails with reason empty_completion and becomes available again" do
    id = create_task!("empty-completion")
    bid!(id, "lazy-worker")
    assert allocate!(id)["claimed_by"] == "lazy-worker"

    done =
      post_json("/work/#{id}/complete", %{
        "agent_id" => "lazy-worker",
        "state" => "completed",
        "result" => %{"output" => "", "tool_call_count" => 0, "feedback" => feedback()}
      })

    assert done.status == 200

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "available"
    assert attempt_of(work) == 1
    failure = work[:last_failure] || work["last_failure"]
    assert failure["reason"] == "empty_completion"
    assert failure["retry_state"] == "available"
    assert failure["agent_id"] == "lazy-worker"

    {:ok, events} = Hive.Work.events(id)
    failed = Enum.find(events, &(&1["topic"] == "engineering.failed"))
    assert failed["payload"]["reason"] == "empty_completion"
    assert failed["payload"]["retry_state"] == "available"
    refute Enum.any?(events, &(&1["topic"] == "engineering.completed"))

    # the item can be claimed and worked again
    bid!(id, "second-worker")
    assert allocate!(id)["claimed_by"] == "second-worker"
  end

  test "empty completions escalate once max attempts are spent" do
    Application.put_env(:hive, :max_attempts, 1)
    on_exit(fn -> Application.delete_env(:hive, :max_attempts) end)

    id = create_task!("empty-completion-escalate")
    bid!(id, "lazy-escalate")
    assert allocate!(id)["claimed_by"] == "lazy-escalate"

    post_json("/work/#{id}/complete", %{
      "agent_id" => "lazy-escalate",
      "state" => "completed",
      "result" => %{"output" => "   ", "tool_call_count" => 0, "feedback" => feedback()}
    })

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "escalated"

    {:ok, events} = Hive.Work.events(id)
    failed = Enum.find(events, &(&1["topic"] == "engineering.failed"))
    assert failed["payload"]["reason"] == "empty_completion"

    escalated = Enum.find(events, &(&1["topic"] == "work.escalated"))
    assert escalated["payload"]["reason"] == "max_attempts_exceeded"
    assert escalated["payload"]["max_attempts"] == 1

    {:ok, available} = Hive.Work.available(100)
    refute Enum.any?(available, fn item -> (item[:id] || item["id"]) == id end)
  end

  test "a completion with tool calls completes even with a blank output" do
    id = create_task!("empty-completion-with-calls")
    bid!(id, "busy-worker")
    assert allocate!(id)["claimed_by"] == "busy-worker"

    done =
      post_json("/work/#{id}/complete", %{
        "agent_id" => "busy-worker",
        "state" => "completed",
        "result" => %{"output" => "", "tool_call_count" => 2, "feedback" => feedback()}
      })

    assert done.status == 200

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "completed"

    {:ok, events} = Hive.Work.events(id)
    assert Enum.any?(events, &(&1["topic"] == "engineering.completed"))
  end

  test "a completion with non-blank output but zero tool calls completes" do
    id = create_task!("empty-completion-with-output")
    bid!(id, "talkative-worker")
    assert allocate!(id)["claimed_by"] == "talkative-worker"

    done =
      post_json("/work/#{id}/complete", %{
        "agent_id" => "talkative-worker",
        "state" => "completed",
        "result" => %{
          "output" => "did the thing",
          "tool_call_count" => 0,
          "feedback" => feedback()
        }
      })

    assert done.status == 200

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "completed"
  end

  test "a completion without tool_call_count completes (older workers)" do
    id = create_task!("empty-completion-legacy")
    bid!(id, "legacy-worker")
    assert allocate!(id)["claimed_by"] == "legacy-worker"

    done =
      post_json("/work/#{id}/complete", %{
        "agent_id" => "legacy-worker",
        "state" => "completed",
        "result" => %{"output" => "", "feedback" => feedback()}
      })

    assert done.status == 200

    {:ok, work} = Hive.Work.get(id)
    assert state_of(work) == "completed"
  end

  test "attach_remote without a remote state does not mark the task completed" do
    task = Hive.Tasks.attach_remote("task_attach_default", %{"id" => "task_attach_default"})
    assert task.state == "unknown"

    stated = Hive.Tasks.attach_remote("task_attach_stated", %{"status" => %{"state" => "failed"}})
    assert stated.state == "failed"
  end

  defp state_of(work), do: work[:state] || work["state"]
  defp attempt_of(work), do: work[:attempt] || work["attempt"]

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
      "params" => %{
        "message" => %{"parts" => [%{"text" => text}]},
        "metadata" => %{"slug" => text}
      }
    })
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
    |> get_in(["result", "id"])
  end

  defp bid!(work_id, agent_id) do
    response =
      post_json("/work/#{work_id}/bids", %{
        "agent_id" => agent_id,
        "confidence" => 0.9,
        "approach" => "test",
        "estimated_cost" => 1,
        "expected_benefit" => 5
      })

    assert response.status == 201
  end

  defp allocate!(work_id) do
    post_json("/work/#{work_id}/allocate", %{"lease_seconds" => 900})
    |> Map.fetch!(:resp_body)
    |> Jason.decode!()
  end

  defp feedback do
    %{
      "calibration" => %{
        "estimated_cost" => 1,
        "actual_cost" => 1,
        "estimated_benefit" => 1,
        "actual_benefit" => 1
      },
      "friction" => ""
    }
  end
end
