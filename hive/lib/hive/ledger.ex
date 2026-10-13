defmodule Hive.Ledger do
  @moduledoc """
  Cost ledger: one row per allocation (`work_id`, `attempt`) recording what the
  winning bid promised and how the lease ended, so cost-aware allocation can be
  audited. Pure helpers only; `Hive.Work` persists the rows (table
  `company.hive_cost_ledger`, or an in-memory list without a database).

  Outcomes: `allocated` (still in flight), `completed`, `failed`, `lease_expired`,
  `escalated` (lease expired and the attempt limit was spent).
  """

  @closed_outcomes ~w(completed failed lease_expired escalated)

  def closed_outcomes, do: @closed_outcomes

  def new_row(work_id, slug, attempt, agent_id, bid, agent, tier, selection, now) do
    capabilities = (agent && agent["capabilities"]) || %{}

    %{
      "ledger_id" => "led_" <> Base.encode16(:crypto.strong_rand_bytes(8), case: :lower),
      "work_id" => work_id,
      "slug" => slug,
      "attempt" => attempt,
      "agent_id" => agent_id,
      "estimated_cost" => bid && bid["estimated_cost"],
      "cost_factor" => number_or_nil(capabilities["cost_factor"]),
      "tier" => tier,
      "agent_tier" => capabilities["tier"],
      "selection" => selection,
      "allocated_at" => now,
      "outcome" => "allocated",
      "outcome_at" => nil,
      "runtime_seconds" => nil
    }
  end

  defp number_or_nil(value) when is_number(value), do: value * 1.0
  defp number_or_nil(_), do: nil

  @doc "Closes the open row for (work, attempt, agent) in an in-memory ledger list."
  def close(rows, work_id, attempt, agent_id, outcome, now) when outcome in @closed_outcomes do
    Enum.map(rows, fn
      %{
        "work_id" => ^work_id,
        "attempt" => ^attempt,
        "agent_id" => ^agent_id,
        "outcome" => "allocated"
      } = row ->
        row
        |> Map.put("outcome", outcome)
        |> Map.put("outcome_at", now)
        |> Map.put(
          "runtime_seconds",
          DateTime.diff(now, row["allocated_at"], :millisecond) / 1000
        )

      row ->
        row
    end)
  end

  @doc "Per-agent roll-up plus totals."
  def summarize(rows) do
    agents =
      rows
      |> Enum.group_by(& &1["agent_id"])
      |> Enum.map(fn {agent_id, agent_rows} -> agent_summary(agent_id, agent_rows) end)
      |> Enum.sort_by(& &1["agent_id"])

    %{
      "agents" => agents,
      "totals" => %{
        "allocations" => Enum.sum(Enum.map(agents, & &1["allocations"])),
        "failures" => Enum.sum(Enum.map(agents, & &1["failures"])),
        "runtime_seconds" => Enum.sum(Enum.map(agents, & &1["runtime_seconds"])),
        "total_estimated_cost" => Enum.sum(Enum.map(agents, & &1["total_estimated_cost"]))
      }
    }
  end

  defp agent_summary(agent_id, rows) do
    count = fn outcome -> Enum.count(rows, &(&1["outcome"] == outcome)) end

    %{
      "agent_id" => agent_id,
      "allocations" => length(rows),
      "completed" => count.("completed"),
      "failed" => count.("failed"),
      "lease_expired" => count.("lease_expired"),
      "escalated" => count.("escalated"),
      "in_flight" => count.("allocated"),
      "failures" => count.("failed") + count.("lease_expired") + count.("escalated"),
      "runtime_seconds" => rows |> Enum.map(&(&1["runtime_seconds"] || 0)) |> Enum.sum(),
      "total_estimated_cost" => rows |> Enum.map(&(&1["estimated_cost"] || 0)) |> Enum.sum()
    }
  end
end
