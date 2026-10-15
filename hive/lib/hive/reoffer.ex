defmodule Hive.Reoffer do
  @moduledoc false

  # Policy for work nobody wants. An item that stays `available` with no interested bid
  # for `HIVE_REOFFER_AFTER_SECONDS` is re-offered on easier terms instead of rotting:
  # earlier bids (declines) are cleared so every worker can look again, the required tier
  # drops one step (power -> standard -> cheap), and an `incentive` multiplier on the
  # payload grows by 0.5 per re-offer for workers to apply to their own cost. After
  # `HIVE_MAX_REOFFERS` re-offers the item escalates to a human or manager.

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
  Decides what happens to a stale unbid item. Returns `{:reoffer, payload, event_payload}`
  with the updated work payload, or `{:escalate, event_payload}` once re-offers are spent.
  """
  def plan(payload) do
    done = payload["reoffers"] || 0
    slug = payload["slug"]
    tier = payload["tier"] || "standard"

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
      new_tier = Map.get(@lower, tier, tier)
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
