defmodule Hive.SelectionTest do
  use ExUnit.Case, async: true

  alias Hive.Selection

  @now ~U[2026-10-13 12:00:00Z]
  @cfg %{
    selection_mode: "cheapest_eligible",
    min_confidence: 0.5,
    health_window_seconds: 900,
    strike_limit: 2,
    strike_window_seconds: 21_600,
    bid_window_seconds: 60,
    history_window_seconds: 604_800,
    history_prior_weight: 4.0,
    explore_outcomes: 3,
    explore_discount: 0.5
  }

  defp bid(agent, opts \\ []) do
    %{
      "agent_id" => agent,
      "interested" => Keyword.get(opts, :interested, true),
      "confidence" => Keyword.get(opts, :confidence, 0.8),
      "estimated_cost" => Keyword.get(opts, :cost, 1.0),
      "expected_benefit" => 1.0,
      "score" => Keyword.get(opts, :score, 1.0),
      "created_at" => Keyword.get(opts, :at, DateTime.add(@now, -10, :second))
    }
  end

  defp expired(agent, work, secs_ago),
    do: %{
      "topic" => "engineering.failed",
      "task_id" => work,
      "payload" => %{"agent_id" => agent, "reason" => "lease_expired"},
      "occurred_at" => DateTime.add(@now, -secs_ago, :second)
    }

  defp completed(agent, secs_ago),
    do: %{
      "topic" => "engineering.completed",
      "task_id" => "other",
      "payload" => %{"agent_id" => agent, "state" => "completed"},
      "occurred_at" => DateTime.add(@now, -secs_ago, :second)
    }

  defp failed(agent, secs_ago),
    do: %{
      "topic" => "engineering.failed",
      "task_id" => "other",
      "payload" => %{"agent_id" => agent, "reason" => "error"},
      "occurred_at" => DateTime.add(@now, -secs_ago, :second)
    }

  defp history(agent, done, bad),
    do:
      Enum.map(1..max(done, 1)//1, &completed(agent, &1 * 60))
      |> Enum.take(done)
      |> Kernel.++(Enum.map(1..max(bad, 1)//1, &failed(agent, &1 * 60 + 30)) |> Enum.take(bad))

  defp rank(bids, events \\ [], cfg \\ @cfg, agents \\ %{}, live \\ []) do
    ctx = Selection.context(events, agents, "w1", live, @now, cfg)
    Selection.rank(bids, ctx, cfg)
  end

  test "cheapest eligible bid wins even when a pricier bid scores higher" do
    result =
      rank([
        bid("claude", cost: 1.0, confidence: 0.95, score: 9.0),
        bid("glm", cost: 0.1, confidence: 0.6, score: 2.0)
      ])

    assert result.winner["agent_id"] == "glm"
    assert result.mode == "cheapest_eligible"
    assert Enum.map(result.ordered, & &1["agent_id"]) == ["glm", "claude"]
    assert hd(result.ordered)["selected"]
  end

  test "a cheap bid below the confidence floor is excluded" do
    result = rank([bid("glm", cost: 0.1, confidence: 0.4), bid("claude", cost: 1.0)])

    assert result.winner["agent_id"] == "claude"
    glm = Enum.find(result.ordered, &(&1["agent_id"] == "glm"))
    assert glm["ineligible_reasons"] == ["low_confidence"]
    refute glm["eligible"]
  end

  test "the confidence floor is configurable" do
    result =
      rank([bid("glm", cost: 0.1, confidence: 0.4), bid("claude")], [], %{
        @cfg
        | min_confidence: 0.3
      })

    assert result.winner["agent_id"] == "glm"
  end

  test "an agent with consecutive lease_expired failures is demoted" do
    events = [expired("claude", "a", 500), expired("claude", "b", 400)]
    result = rank([bid("claude", cost: 0.1), bid("glm", cost: 1.0)], events)

    assert result.winner["agent_id"] == "glm"
    claude = Enum.find(result.ordered, &(&1["agent_id"] == "claude"))
    assert "repeated_lease_expired" in claude["ineligible_reasons"]
  end

  test "a completion after the failures clears the demotion" do
    events = [expired("claude", "a", 500), expired("claude", "b", 400), completed("claude", 100)]

    assert rank([bid("claude", cost: 0.1), bid("glm", cost: 1.0)], events).winner["agent_id"] ==
             "claude"
  end

  test "a single expiry does not demote, and old expiries age out of the window" do
    one = [expired("claude", "a", 500)]
    assert rank([bid("claude", cost: 0.1), bid("glm")], one).winner["agent_id"] == "claude"

    old = [expired("claude", "a", 30_000), expired("claude", "b", 29_000)]
    assert rank([bid("claude", cost: 0.1), bid("glm")], old).winner["agent_id"] == "claude"
  end

  test "an agent whose lease on this work already expired is skipped while others are eligible" do
    events = [expired("claude", "w1", 20_000)]
    result = rank([bid("claude", cost: 0.1), bid("glm", cost: 1.0)], events)

    assert result.winner["agent_id"] == "glm"
    claude = Enum.find(result.ordered, &(&1["agent_id"] == "claude"))
    assert "lease_expired_on_this_work" in claude["ineligible_reasons"]
  end

  test "stale bids from agents not seen recently are excluded unless the agent is connected" do
    stale = DateTime.add(@now, -3_600, :second)
    bids = [bid("ghost", cost: 0.1, at: stale), bid("claude", cost: 1.0)]

    assert rank(bids).winner["agent_id"] == "claude"
    assert rank(bids, [], @cfg, %{}, ["ghost"]).winner["agent_id"] == "ghost"

    agents = %{"ghost" => %{"last_seen_at" => DateTime.add(@now, -60, :second)}}
    assert rank(bids, [], @cfg, agents).winner["agent_id"] == "ghost"
  end

  test "falls back to the old score ranking when no bid is eligible" do
    result =
      rank([
        bid("low", cost: 0.1, confidence: 0.2, score: 1.0),
        bid("lower", cost: 0.1, confidence: 0.1, score: 3.0)
      ])

    assert result.mode == "score_fallback"
    assert result.winner["agent_id"] == "lower"
  end

  test "ties on cost fall back to score, then to the earliest bid" do
    early = DateTime.add(@now, -30, :second)
    late = DateTime.add(@now, -5, :second)

    by_score = rank([bid("a", score: 1.0), bid("b", score: 2.0)])
    assert by_score.winner["agent_id"] == "b"

    by_time = rank([bid("late", at: late), bid("early", at: early)])
    assert by_time.winner["agent_id"] == "early"
  end

  test "declined bids are never selected and no bids means no winner" do
    assert rank([bid("x", interested: false)]).winner == nil
    assert rank([]).winner == nil
  end

  test "score mode keeps the legacy ranking" do
    result =
      rank([bid("claude", cost: 1.0, score: 9.0), bid("glm", cost: 0.1, score: 2.0)], [], %{
        @cfg
        | selection_mode: "score"
      })

    assert result.winner["agent_id"] == "claude"
    assert result.mode == "score"
  end

  describe "bid_window/5" do
    test "waits while an expected worker has not bid, until the window closes" do
      since = DateTime.add(@now, -20, :second)
      assert {:wait, 40} = Selection.bid_window(since, ["glm", "claude"], ["claude"], @now, @cfg)
      assert :open = Selection.bid_window(since, ["claude"], ["claude"], @now, @cfg)

      assert :open =
               Selection.bid_window(DateTime.add(@now, -61, :second), ["glm"], [], @now, @cfg)

      assert :open =
               Selection.bid_window(since, ["glm"], [], @now, %{@cfg | bid_window_seconds: 0})
    end
  end

  test "a cheap worker that keeps failing loses to a reliable pricier one" do
    events = history("glm", 2, 10) ++ history("claude", 12, 0)

    result =
      rank(
        [bid("glm", cost: 0.1, confidence: 0.8), bid("claude", cost: 0.2, confidence: 0.9)],
        events
      )

    assert result.winner["agent_id"] == "claude"
    glm = Enum.find(result.ordered, &(&1["agent_id"] == "glm"))
    assert glm["p_complete"] < 0.4
  end

  test "a cheap worker with a good record still beats a pricier one" do
    events = history("glm", 10, 1) ++ history("claude", 12, 0)

    result =
      rank(
        [bid("glm", cost: 0.1, confidence: 0.8), bid("claude", cost: 1.0, confidence: 0.9)],
        events
      )

    assert result.winner["agent_id"] == "glm"
  end

  test "a new agent with no track record is explored ahead of a slightly cheaper veteran" do
    events = history("old", 10, 0)

    result =
      rank(
        [bid("old", cost: 0.4, confidence: 0.8), bid("new", cost: 0.6, confidence: 0.8)],
        events
      )

    assert result.winner["agent_id"] == "new"
  end

  test "exploration ends once the agent has enough recorded outcomes" do
    events = history("old", 10, 0) ++ history("new", 3, 0)

    result =
      rank(
        [bid("old", cost: 0.4, confidence: 0.8), bid("new", cost: 0.6, confidence: 0.8)],
        events
      )

    assert result.winner["agent_id"] == "old"
  end

  test "outcomes outside the history window are ignored" do
    events = Enum.map(1..10, &failed("glm", 700_000 + &1))
    result = rank([bid("glm", confidence: 0.8)], events)

    assert hd(result.ordered)["p_complete"] == 0.8
  end
end
