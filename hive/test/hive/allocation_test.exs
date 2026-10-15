defmodule Hive.AllocationTest do
  # Cost-aware allocation and the cost ledger, through the HTTP router. Runs on the
  # in-memory store, or on a dedicated *_test Postgres when COMPANY_DATABASE_URL is
  # set (see hive/README.md "Test database safety").
  use ExUnit.Case
  import Plug.Test

  @opts Hive.Router.init([])

  setup do
    on_exit(fn -> Hive.Work.unsubscribe(self()) end)
    :ok
  end

  test "allocates the cheapest eligible bidder, not the best score" do
    id = create_task!("alloc-cheapest")
    bid!(id, "claude-like", 0.95, 1.0, 9)
    bid!(id, "glm-like", 0.6, 0.1, 2)

    assert [%{"agent_id" => "glm-like", "selected" => true}, %{"agent_id" => "claude-like"}] =
             get_json("/work/#{id}/bids")["bids"]

    allocated = allocate!(id)
    assert allocated["claimed_by"] == "glm-like"

    {:ok, events} = Hive.Work.events(id)
    payload = Enum.find(events, &(&1["topic"] == "work.allocated"))["payload"]
    assert payload["selection"] == "cheapest_eligible"
    assert payload["effective_cost"] == 0.1
  end

  test "a cheap bid under the confidence floor loses to an eligible bid" do
    id = create_task!("alloc-low-confidence")
    bid!(id, "cheap-unsure", 0.3, 0.05, 5)
    bid!(id, "pricey-sure", 0.9, 1.0, 5)

    assert allocate!(id)["claimed_by"] == "pricey-sure"
  end

  test "falls back to score ranking when nothing is eligible" do
    id = create_task!("alloc-fallback")
    bid!(id, "weak", 0.2, 0.1, 1)
    bid!(id, "weaker", 0.1, 0.1, 1)

    assert allocate!(id)["claimed_by"] == "weak"
    {:ok, events} = Hive.Work.events(id)

    assert Enum.find(events, &(&1["topic"] == "work.allocated"))["payload"]["selection"] ==
             "score_fallback"
  end

  test "an expired lease is not re-allocated to the same agent while another bidder is eligible" do
    id = create_task!("alloc-expired-retry")
    bid!(id, "flaky-cheap", 0.9, 0.1, 5)
    bid!(id, "steady", 0.8, 1.0, 5)
    assert allocate!(id, 1)["claimed_by"] == "flaky-cheap"

    Process.sleep(1100)
    assert {:ok, _} = Hive.Work.available(10)

    # The flaky agent re-bids even cheaper; the other eligible bidder still wins the retry.
    bid!(id, "flaky-cheap", 0.9, 0.01, 5)
    assert allocate!(id)["claimed_by"] == "steady"
  end

  test "repeated lease_expired failures demote an agent across work items" do
    for n <- 1..2 do
      id = create_task!("alloc-demote-#{n}")
      bid!(id, "ghost-agent", 0.9, 0.1, 5)
      assert allocate!(id, 1)["claimed_by"] == "ghost-agent"
      Process.sleep(1100)
      assert {:ok, _} = Hive.Work.available(100)
    end

    id = create_task!("alloc-demote-final")
    bid!(id, "ghost-agent", 0.9, 0.01, 5)
    bid!(id, "dependable", 0.8, 2.0, 5)

    ranked = get_json("/work/#{id}/bids")["bids"]
    ghost = Enum.find(ranked, &(&1["agent_id"] == "ghost-agent"))
    assert "repeated_lease_expired" in ghost["ineligible_reasons"]
    assert allocate!(id)["claimed_by"] == "dependable"

    # With nobody else eligible the demoted agent still gets work (fallback), so it can recover.
    solo = create_task!("alloc-demote-solo")
    bid!(solo, "ghost-agent", 0.9, 0.01, 5)
    assert allocate!(solo)["claimed_by"] == "ghost-agent"
  end

  test "allocation waits for connected pool workers that have not bid, then picks the cheapest" do
    for {agent, cost} <- [{"pool-power", 1.0}, {"pool-cheap", 0.1}] do
      post_json("/agents/register", %{
        "id" => agent,
        "capabilities" => %{
          "tier" => if(cost < 1, do: "cheap", else: "power"),
          "cost_factor" => cost
        }
      })
    end

    :ok = Hive.Work.subscribe(self(), "pool-power")
    :ok = Hive.Work.subscribe(self(), "pool-cheap")

    id = create_task!("alloc-bid-window")
    bid!(id, "pool-power", 0.9, 1.0, 5)

    deferred = post_json("/work/#{id}/allocate", %{"lease_seconds" => 900})
    assert deferred.status == 202
    body = Jason.decode!(deferred.resp_body)
    assert body["deferred"] == true
    assert body["reason"] == "bid_window_open"
    assert body["retry_after_seconds"] >= 1

    bid!(id, "pool-cheap", 0.7, 0.1, 5)
    assert allocate!(id)["claimed_by"] == "pool-cheap"
  end

  test "the bid window closes when its time is up" do
    Application.put_env(:hive, :bid_window_seconds, 0)
    on_exit(fn -> Application.delete_env(:hive, :bid_window_seconds) end)

    post_json("/agents/register", %{"id" => "slowpoke", "capabilities" => %{"tier" => "cheap"}})
    :ok = Hive.Work.subscribe(self(), "slowpoke")

    id = create_task!("alloc-window-closed")
    bid!(id, "someone", 0.9, 1.0, 5)
    assert allocate!(id)["claimed_by"] == "someone"
  end

  describe "requires_tools routing" do
    test "registration stores capabilities.tools as a sorted unique string list" do
      post_json("/agents/register", %{
        "id" => "tools-reg",
        "capabilities" => %{
          "tier" => "power",
          "tools" => ["kubectl-deploy", "browser", "browser", 7]
        }
      })

      agent = Enum.find(get_json("/agents")["agents"], &(&1["id"] == "tools-reg"))
      assert agent["capabilities"]["tools"] == ["browser", "kubectl-deploy"]
      assert agent["capabilities"]["tier"] == "power"
    end

    test "an agent registered without tools still registers (backward compatible)" do
      post_json("/agents/register", %{
        "id" => "tools-none",
        "capabilities" => %{"tier" => "cheap"}
      })

      agent = Enum.find(get_json("/agents")["agents"], &(&1["id"] == "tools-none"))
      refute Map.has_key?(agent["capabilities"], "tools")
    end

    test "work stores requires_tools, rejects malformed values, and omits the key when unset" do
      id = create_task!("rt-store", %{"requires_tools" => ["hindsight", "browser", "browser"]})
      {:ok, work} = Hive.Work.get(id)
      assert (work[:payload] || work["payload"])["requires_tools"] == ["browser", "hindsight"]

      plain = create_task!("rt-store-plain")
      {:ok, work} = Hive.Work.get(plain)
      refute Map.has_key?(work[:payload] || work["payload"], "requires_tools")

      bad =
        post_json("/", %{
          "jsonrpc" => "2.0",
          "id" => 1,
          "method" => "message/send",
          "params" => %{
            "message" => %{"parts" => [%{"text" => "rt-bad"}]},
            "metadata" => %{"slug" => "rt-bad", "requires_tools" => "browser"}
          }
        })

      assert bad.status == 422
    end

    test "an incapable agent that bids is marked missing_tools and never allocated, a capable one wins" do
      post_json("/agents/register", %{"id" => "rt-blind", "capabilities" => %{"tools" => []}})

      post_json("/agents/register", %{
        "id" => "rt-seer",
        "capabilities" => %{"tools" => ["browser-rt1"]}
      })

      id = create_task!("rt-route", %{"requires_tools" => ["browser-rt1"]})
      bid!(id, "rt-blind", 0.95, 0.01, 5)

      [only] = get_json("/work/#{id}/bids")["bids"]
      assert only["ineligible_reasons"] == ["missing_tools"]
      assert only["missing_tools"] == ["browser-rt1"]

      refused = post_json("/work/#{id}/allocate", %{"lease_seconds" => 900})
      assert refused.status == 409

      {:ok, work} = Hive.Work.get(id)
      assert (work[:state] || work["state"]) == "available"

      bid!(id, "rt-seer", 0.8, 1.0, 5)
      assert allocate!(id)["claimed_by"] == "rt-seer"

      {:ok, events} = Hive.Work.events(id)
      allocated = Enum.find(events, &(&1["topic"] == "work.allocated"))["payload"]
      assert allocated["required_tools"] == ["browser-rt1"]
    end

    test "work without requires_tools is allocated to the cheapest bidder regardless of tools" do
      post_json("/agents/register", %{"id" => "rt-plain-a", "capabilities" => %{"tools" => []}})
      id = create_task!("rt-plain-route")
      bid!(id, "rt-plain-a", 0.9, 0.01, 5)
      bid!(id, "rt-plain-b", 0.9, 1.0, 5)

      assert allocate!(id)["claimed_by"] == "rt-plain-a"
      {:ok, events} = Hive.Work.events(id)
      allocated = Enum.find(events, &(&1["topic"] == "work.allocated"))["payload"]
      refute Map.has_key?(allocated, "required_tools")
    end
  end

  describe "cost ledger" do
    test "records an allocation, closes it on completion and summarizes per agent" do
      post_json("/agents/register", %{
        "id" => "ledger-cheap",
        "capabilities" => %{"tier" => "cheap", "cost_factor" => 0.1}
      })

      id = create_task!("ledger-complete", %{"tier" => "power"})
      bid!(id, "ledger-cheap", 0.9, 0.25, 5)
      assert allocate!(id)["claimed_by"] == "ledger-cheap"

      row = ledger_row(id)
      assert row["agent_id"] == "ledger-cheap"
      assert row["slug"] == "ledger-complete"
      assert row["attempt"] == 1
      assert row["estimated_cost"] == 0.25
      assert row["cost_factor"] == 0.1
      assert row["tier"] == "power"
      assert row["agent_tier"] == "cheap"
      assert row["outcome"] == "allocated"
      assert row["allocated_at"]

      Process.sleep(50)

      done =
        post_json("/work/#{id}/complete", %{
          "agent_id" => "ledger-cheap",
          "state" => "completed",
          "result" => %{"ok" => true, "feedback" => feedback()}
        })

      assert done.status == 200

      row = ledger_row(id)
      assert row["outcome"] == "completed"
      assert row["outcome_at"]
      assert row["runtime_seconds"] > 0

      summary = get_json("/ledger?limit=200")
      agent = Enum.find(summary["agents"], &(&1["agent_id"] == "ledger-cheap"))
      assert agent["allocations"] >= 1
      assert agent["completed"] >= 1
      assert agent["total_estimated_cost"] >= 0.25
      assert agent["runtime_seconds"] > 0
      assert is_map(summary["totals"])
    end

    test "records failed and lease_expired outcomes" do
      failed = create_task!("ledger-failed")
      bid!(failed, "ledger-flaky", 0.9, 1.0, 5)
      assert allocate!(failed)["claimed_by"] == "ledger-flaky"

      post_json("/work/#{failed}/complete", %{
        "agent_id" => "ledger-flaky",
        "state" => "failed",
        "result" => %{"error" => "boom", "feedback" => feedback()}
      })

      assert ledger_row(failed)["outcome"] == "failed"

      expired = create_task!("ledger-expired")
      bid!(expired, "ledger-flaky", 0.9, 1.0, 5)
      assert allocate!(expired, 1)["claimed_by"] == "ledger-flaky"
      Process.sleep(1100)
      assert {:ok, _} = Hive.Work.available(100)

      assert ledger_row(expired)["outcome"] == "lease_expired"

      agent =
        get_json("/ledger?limit=200")["agents"] |> Enum.find(&(&1["agent_id"] == "ledger-flaky"))

      assert agent["failed"] >= 1
      assert agent["lease_expired"] >= 1
      assert agent["failures"] >= 2
    end
  end

  defp ledger_row(work_id) do
    {:ok, work} = Hive.Work.get(work_id)
    id = work[:id] || work["id"]
    get_json("/ledger?limit=200")["recent"] |> Enum.find(&(&1["work_id"] == id))
  end

  defp get_json(path),
    do: conn(:get, path) |> Hive.Router.call(@opts) |> Map.fetch!(:resp_body) |> Jason.decode!()

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
      "params" => %{
        "message" => %{"parts" => [%{"text" => text}]},
        "metadata" => Map.put(metadata, "slug", text)
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
