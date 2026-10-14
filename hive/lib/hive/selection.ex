defmodule Hive.Selection do
  @moduledoc """
  Pure allocation policy: pick the CHEAPEST bidder that can deliver.

  A bid "can deliver" when it is interested, confident enough
  (`min_confidence`, default 0.5), from a recently seen agent, and the agent has
  no unfinished-lease history that makes it a bad bet: it did not already let the
  lease on this very work expire, and it has fewer than `strike_limit`
  consecutive `lease_expired` failures since it last completed anything.

  Among eligible bids the winner has the lowest effective cost: the expected
  price per SUCCESSFUL completion, `estimated_cost / p_complete`
  (`estimated_cost` already includes the worker's `AGENT_COST_FACTOR` and the
  tier-mismatch penalty, so it is NOT scaled again). `p_complete` blends the
  bid's own confidence with the agent's recorded completion rate
  (`engineering.completed` vs `engineering.failed` events in the history
  window) as a Beta-style estimate: the confidence counts as
  `history_prior_weight` pseudo-observations, so a proven agent is judged on
  results and an unproven one on its stated confidence. A flaky cheap worker
  therefore loses to a reliable pricier one once its failures outweigh the
  price gap, and Claude (high price, high success) only wins where cheap
  workers decline or are ineligible.

  Exploration: an agent with fewer than `explore_outcomes` recorded outcomes
  gets its effective cost multiplied by `explore_discount`, so a new worker or
  freshly rolled image wins allocations until it has a track record.

  Ties are broken by the old score (`confidence * benefit / cost`) and then by
  the earliest bid. When no bid is eligible the old score ranking is used as a
  fallback, so work never starves.

  Configuration (application env under `:hive` wins over the OS env var):

  | key                         | env                                 | default            |
  |-----------------------------|-------------------------------------|--------------------|
  | `:selection_mode`           | `HIVE_SELECTION_MODE`               | `cheapest_eligible` (or `score`) |
  | `:min_confidence`           | `HIVE_MIN_CONFIDENCE`               | 0.5                |
  | `:health_window_seconds`    | `HIVE_AGENT_HEALTH_WINDOW_SECONDS`  | 900                |
  | `:strike_limit`             | `HIVE_LEASE_EXPIRED_STRIKES`        | 2                  |
  | `:strike_window_seconds`    | `HIVE_LEASE_EXPIRED_WINDOW_SECONDS` | 21600              |
  | `:bid_window_seconds`       | `HIVE_BID_WINDOW_SECONDS`           | 60                 |
  | `:history_window_seconds`   | `HIVE_HISTORY_WINDOW_SECONDS`       | 604800 (7 days)    |
  | `:history_prior_weight`     | `HIVE_HISTORY_PRIOR_WEIGHT`         | 4                  |
  | `:explore_outcomes`         | `HIVE_EXPLORE_OUTCOMES`             | 3                  |
  | `:explore_discount`         | `HIVE_EXPLORE_DISCOUNT`             | 0.5                |
  """

  @settings [
    {:selection_mode, "HIVE_SELECTION_MODE", "cheapest_eligible"},
    {:min_confidence, "HIVE_MIN_CONFIDENCE", 0.5},
    {:health_window_seconds, "HIVE_AGENT_HEALTH_WINDOW_SECONDS", 900},
    {:strike_limit, "HIVE_LEASE_EXPIRED_STRIKES", 2},
    {:strike_window_seconds, "HIVE_LEASE_EXPIRED_WINDOW_SECONDS", 21_600},
    {:bid_window_seconds, "HIVE_BID_WINDOW_SECONDS", 60},
    {:history_window_seconds, "HIVE_HISTORY_WINDOW_SECONDS", 604_800},
    {:history_prior_weight, "HIVE_HISTORY_PRIOR_WEIGHT", 4.0},
    {:explore_outcomes, "HIVE_EXPLORE_OUTCOMES", 3},
    {:explore_discount, "HIVE_EXPLORE_DISCOUNT", 0.5}
  ]

  @doc "Resolved configuration map (app env, then OS env, then defaults)."
  def config do
    Map.new(@settings, fn {key, env, default} ->
      raw = Application.get_env(:hive, key) || System.get_env(env)
      {key, parse(raw, default)}
    end)
  end

  defp parse(nil, default), do: default
  defp parse("", default), do: default
  defp parse(value, default) when is_binary(default), do: value

  defp parse(value, default) when is_binary(value) do
    case Float.parse(value) do
      {number, ""} when is_float(default) -> number
      {number, ""} -> round(number)
      _ -> default
    end
  end

  defp parse(value, default) when is_number(value) and is_float(default), do: value * 1.0
  defp parse(value, default) when is_number(value) and is_integer(default), do: round(value)
  defp parse(_value, default), do: default

  @doc """
  Builds the evidence the policy needs from durable events:

    * `expired_here` - agents whose lease on `work_id` already expired
    * `strikes`      - agent id => consecutive lease_expired failures (inside the
      strike window) that no later completion has cleared
    * `outcomes`     - agent id => `{completed, failed}` counts inside the history
      window, feeding the completion-rate estimate and exploration
  """
  def context(events, agents, work_id, live_ids, now, cfg) do
    cutoff = DateTime.add(now, -cfg.strike_window_seconds, :second)
    expired = Enum.filter(events, &lease_expired?/1)

    expired_here =
      expired
      |> Enum.filter(&(&1["task_id"] == work_id))
      |> MapSet.new(&get_in(&1, ["payload", "agent_id"]))

    strikes =
      expired
      |> Enum.filter(&(DateTime.compare(&1["occurred_at"], cutoff) != :lt))
      |> Enum.group_by(&get_in(&1, ["payload", "agent_id"]))
      |> Map.new(fn {agent_id, failures} ->
        cleared_at = last_completion(events, agent_id)

        {agent_id,
         Enum.count(failures, fn failure ->
           is_nil(cleared_at) or DateTime.compare(failure["occurred_at"], cleared_at) == :gt
         end)}
      end)

    %{
      now: now,
      expired_here: expired_here,
      strikes: strikes,
      outcomes: outcomes(events, now, cfg),
      agents: agents,
      live_ids: MapSet.new(live_ids)
    }
  end

  defp outcomes(events, now, cfg) do
    cutoff = DateTime.add(now, -cfg.history_window_seconds, :second)

    events
    |> Enum.filter(&(&1["topic"] in ["engineering.completed", "engineering.failed"]))
    |> Enum.filter(
      &(is_struct(&1["occurred_at"], DateTime) and
          DateTime.compare(&1["occurred_at"], cutoff) != :lt)
    )
    |> Enum.reduce(%{}, fn event, acc ->
      agent_id = get_in(event, ["payload", "agent_id"])
      done = if event["topic"] == "engineering.completed", do: 1, else: 0
      Map.update(acc, agent_id, {done, 1 - done}, fn {c, f} -> {c + done, f + 1 - done} end)
    end)
  end

  defp lease_expired?(event),
    do:
      event["topic"] == "engineering.failed" and
        get_in(event, ["payload", "reason"]) == "lease_expired"

  defp last_completion(events, agent_id) do
    events
    |> Enum.filter(
      &(&1["topic"] == "engineering.completed" and get_in(&1, ["payload", "agent_id"]) == agent_id)
    )
    |> Enum.map(& &1["occurred_at"])
    |> Enum.max(DateTime, fn -> nil end)
  end

  @doc """
  Orders `bids` (maps with string keys incl. `"score"` and `"created_at"`).

  Returns `%{ordered: [...], winner: bid | nil, mode: mode}`. Every bid is
  annotated with `"eligible"`, `"ineligible_reasons"`, `"effective_cost"` and
  `"selected"`. `mode` is `"cheapest_eligible"`, `"score_fallback"` (nothing
  was eligible) or `"score"` (policy disabled).
  """
  def rank(bids, ctx, cfg) do
    annotated =
      Enum.map(bids, fn bid ->
        reasons = reasons(bid, ctx, cfg)

        bid
        |> Map.put("eligible", reasons == [])
        |> Map.put("ineligible_reasons", reasons)
        |> Map.put("p_complete", p_complete(bid, ctx, cfg))
        |> Map.put("effective_cost", effective_cost(bid, ctx, cfg))
      end)

    {interested, declined} = Enum.split_with(annotated, & &1["interested"])
    eligible = Enum.filter(interested, & &1["eligible"])

    {ordered_interested, mode} =
      cond do
        cfg.selection_mode == "score" ->
          {Enum.sort(interested, &score_order/2), "score"}

        eligible != [] ->
          {cheapest_first(eligible) ++ score_rest(interested, eligible), "cheapest_eligible"}

        true ->
          {Enum.sort(interested, &score_order/2), "score_fallback"}
      end

    ordered =
      case ordered_interested do
        [first | rest] ->
          [Map.put(first, "selected", true) | Enum.map(rest, &Map.put(&1, "selected", false))]

        [] ->
          []
      end ++ Enum.map(declined, &Map.put(&1, "selected", false))

    %{ordered: ordered, winner: List.first(ordered_interested) && hd(ordered), mode: mode}
  end

  defp outcomes_for(bid, ctx), do: Map.get(Map.get(ctx, :outcomes, %{}), bid["agent_id"], {0, 0})

  # Beta-style estimate: the bid's confidence is worth `history_prior_weight`
  # pseudo-observations, recorded outcomes add real ones.
  defp p_complete(bid, ctx, cfg) do
    {completed, failed} = outcomes_for(bid, ctx)
    weight = cfg.history_prior_weight
    max((weight * bid["confidence"] + completed) / (weight + completed + failed), 0.05)
  end

  defp effective_cost(bid, ctx, cfg) do
    {completed, failed} = outcomes_for(bid, ctx)
    cost = bid["estimated_cost"] / p_complete(bid, ctx, cfg)

    if completed + failed < cfg.explore_outcomes, do: cost * cfg.explore_discount, else: cost
  end

  defp reasons(bid, ctx, cfg) do
    agent_id = bid["agent_id"]

    [
      {not bid["interested"], "not_interested"},
      {bid["confidence"] < cfg.min_confidence, "low_confidence"},
      {not seen_recently?(bid, ctx, cfg), "unhealthy"},
      {MapSet.member?(ctx.expired_here, agent_id), "lease_expired_on_this_work"},
      {Map.get(ctx.strikes, agent_id, 0) >= cfg.strike_limit, "repeated_lease_expired"}
    ]
    |> Enum.flat_map(fn {hit?, reason} -> if hit?, do: [reason], else: [] end)
  end

  # An agent is "recently seen" if it is connected right now, or its registration/
  # heartbeat or this very bid is newer than the health window.
  defp seen_recently?(bid, ctx, cfg) do
    cutoff = DateTime.add(ctx.now, -cfg.health_window_seconds, :second)

    last_seen =
      [get_in(ctx.agents, [bid["agent_id"], "last_seen_at"]), bid["created_at"]]
      |> Enum.filter(&is_struct(&1, DateTime))
      |> Enum.max(DateTime, fn -> nil end)

    MapSet.member?(ctx.live_ids, bid["agent_id"]) or
      (last_seen != nil and DateTime.compare(last_seen, cutoff) != :lt)
  end

  defp cheapest_first(bids), do: Enum.sort(bids, &cost_order/2)

  # Eligible bids lead; the remaining interested bids follow in score order so the
  # list stays a complete ranking.
  defp score_rest(interested, eligible) do
    chosen = MapSet.new(eligible, & &1["agent_id"])

    interested
    |> Enum.reject(&MapSet.member?(chosen, &1["agent_id"]))
    |> Enum.sort(&score_order/2)
  end

  defp cost_order(a, b) do
    cond do
      a["effective_cost"] != b["effective_cost"] -> a["effective_cost"] < b["effective_cost"]
      a["score"] != b["score"] -> a["score"] > b["score"]
      true -> DateTime.compare(a["created_at"], b["created_at"]) != :gt
    end
  end

  defp score_order(a, b) do
    case {a["score"], b["score"]} do
      {score, score} -> DateTime.compare(a["created_at"], b["created_at"]) == :gt
      {a_score, b_score} -> a_score > b_score
    end
  end

  @doc """
  Bid window: while a connected, registered pool worker (the `expected` agent ids)
  has not bid yet, allocation waits - so a fast expensive worker cannot win just by
  bidding first. Returns `:open` (allocate now) or `{:wait, seconds}`.
  """
  def bid_window(available_since, expected, bidder_ids, now, cfg) do
    missing = MapSet.difference(MapSet.new(expected), MapSet.new(bidder_ids))
    deadline = available_since && DateTime.add(available_since, cfg.bid_window_seconds, :second)

    cond do
      cfg.bid_window_seconds <= 0 or MapSet.size(missing) == 0 or is_nil(deadline) -> :open
      DateTime.compare(now, deadline) != :lt -> :open
      true -> {:wait, max(1, DateTime.diff(deadline, now, :second))}
    end
  end
end
