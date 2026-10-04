defmodule Hive.Tasks do
  use Agent

  def start_link(_opts), do: Agent.start_link(fn -> %{} end, name: __MODULE__)

  def create(task_id, message, title, slug) do
    Agent.update(
      __MODULE__,
      &Map.put(&1, task_id, %{
        id: task_id,
        title: title,
        slug: slug,
        message: message,
        state: "available",
        attempt: 0,
        last_failure: nil,
        claimed_by: nil,
        lease_expires_at: nil,
        remote: nil
      })
    )

    get(task_id)
  end

  def get(id_or_slug) do
    Agent.get(__MODULE__, fn tasks ->
      Map.get(tasks, id_or_slug) ||
        Enum.find_value(tasks, fn {_id, task} -> if task.slug == id_or_slug, do: task end)
    end)
  end

  def attach_remote(id_or_slug, remote) do
    Agent.update(__MODULE__, fn tasks ->
      task =
        Map.get(tasks, id_or_slug) ||
          Enum.find_value(tasks, fn {_id, task} -> if task.slug == id_or_slug, do: task end)

      task_id = (task && task.id) || remote["id"] || id_or_slug
      state = get_in(remote, ["status", "state"]) || "completed"

      task =
        task ||
          %{
            id: task_id,
            title: nil,
            slug: nil,
            message: [],
            state: state,
            attempt: 0,
            last_failure: nil,
            claimed_by: nil,
            lease_expires_at: nil,
            remote: nil
          }

      Map.put(tasks, task_id, %{task | remote: remote, state: state})
    end)

    get(id_or_slug)
  end
end
