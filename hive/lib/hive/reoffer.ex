defmodule Hive.Reoffer do
  @moduledoc false

  # Policy for work nobody wants. An item that stays `available` with no interested bid
  # for `HIVE_REOFFER_AFTER_SECONDS` is re-offered on easier terms instead of rotting:
  # earlier bids (declines) are cleared so every worker can look again, the required tier
  # drops one step (power -> standard -> cheap), and an `incentive` multiplier on the
  # payload grows by 0.5 per re-offer for workers to apply to their own cost. After
  # `HIVE_MAX_REOFFERS` re-offers the item escalates to a human or manager.
  #
  # Work with `requires_tools` is stricter: when no registered worker declares every
  # required tool it escalates at once (reason `missing_tools`, naming the tools) instead
  # of being re-offered, and the tier is never lowered to get around the tool filter.

  @lower %{"power" => "standard", "standard" => "cheap", "cheap" => "cheap"}
  @incentive_step 0.5

  def after_seconds do
    Application.get_env(:hive, :reoffer_after_seconds) ||
      "HIVE_REOFFER_AFTER_SECONDS" |> System.get_env("1800") |> String.to_integer()
  end

  def max_reoffers do
    Application.get_env(:hive, :max_reoffers) ||
      "HIVE_MAX_REOFFERS" |> System.get_env("3") |> String.to_integer()
  end

  # An interval of 0 or less disables re-offering.
  def enabled?, do: after_seconds() > 0

  @doc """
  True when an `available` item that has been idle for `after_seconds` needs the sweep:
  no interested bid (from a tool-capable worker, for work with `requires_tools`), or
  nobody registered can do it at all.
  """
  def stale?(payload, interested_agent_ids, agents) do
    case Hive.Selection.required_tools(payload) do
      [] ->
        interested_agent_ids == []

      required ->
        case capable_workers(required, agents) do
          [] -> true
          capable -> not Enum.any?(interested_agent_ids, &(&1 in capable))
        end
    end
  end

  # Registered agents that can take work (execute mode, or no modes declared) and
  # declare every required tool.
  defp capable_workers(required, agents) do
    for {id, agent} <- agents,
        worker?(agent),
        Hive.Selection.missing_tools(agent, required) == [],
        do: id
  end

  defp worker?(agent) do
    case get_in(agent, ["capabilities", "modes"]) do
      modes when is_list(modes) -> "execute" in modes
      _ -> true
    end
  end

  # Required tools missing from the best-equipped worker: the human's shopping list.
  defp missing_for_all(required, agents) do
    agents
    |> Enum.filter(fn {_id, agent} -> worker?(agent) end)
    |> Enum.map(fn {_id, agent} -> Hive.Selection.missing_tools(agent, required) end)
    |> Enum.min_by(&length/1, fn -> required end)
  end

  @doc """
  Decides what happens to a stale unbid item. Returns `{:reoffer, payload, event_payload}`
  with the updated work payload, or `{:escalate, event_payload}` once re-offers are spent.
  """
  def plan(payload, agents \\ %{}) do
    done = payload["reoffers"] || 0
    slug = payload["slug"]
    tier = payload["tier"] || "standard"
    required = Hive.Selection.required_tools(payload)

    if required != [] and capable_workers(required, agents) == [] do
      {:escalate,
       %{
         "reason" => "missing_tools",
         "required_tools" => required,
         "missing_tools" => missing_for_all(required, agents),
         "reoffers" => done,
         "slug" => slug
       }}
    else
      plan_reoffer(payload, done, slug, tier, required != [])
    end
  end

  defp plan_reoffer(payload, done, slug, tier, tools_required?) do
    if done >= max_reoffers() do
      {:escalate,
       %{
         "reason" => "no_bids_after_reoffers",
         "reoffers" => done,
         "max_reoffers" => max_reoffers(),
         "slug" => slug
       }}
    else
      next = done + 1
      # Lowering the tier widens the pool of cheap bidders; for work that needs specific
      # tools it must never be a way around the tool filter, so the tier is held.
      new_tier = if tools_required?, do: tier, else: Map.get(@lower, tier, tier)
      incentive = 1.0 + @incentive_step * next

      updated =
        Map.merge(payload, %{"reoffers" => next, "tier" => new_tier, "incentive" => incentive})

      {:reoffer, updated,
       %{
         "reason" => "no_interested_bids",
         "reoffer" => next,
         "tier_from" => tier,
         "tier_to" => new_tier,
         "incentive" => incentive,
         "slug" => slug
       }}
    end
  end
end
