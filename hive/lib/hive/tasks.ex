defmodule Hive.Tasks do
  use Agent

  def start_link(_opts), do: Agent.start_link(fn -> %{} end, name: __MODULE__)

  def create(task_id, message) do
    Agent.update(
      __MODULE__,
      &Map.put(&1, task_id, %{id: task_id, message: message, state: "available", remote: nil})
    )

    get(task_id)
  end

  def get(task_id), do: Agent.get(__MODULE__, &Map.get(&1, task_id))

  def attach_remote(task_id, remote) do
    Agent.update(__MODULE__, fn tasks ->
      Map.update!(tasks, task_id, fn task ->
        state = get_in(remote, ["status", "state"]) || task.state
        %{task | remote: remote, state: state}
      end)
    end)

    get(task_id)
  end
end
