defmodule Hive.Application do
  use Application

  @impl true
  def start(_type, _args) do
    children = [
      Hive.Tasks,
      Hive.Work,
      {Plug.Cowboy,
       scheme: :http,
       plug: Hive.Router,
       options: [port: port(), protocol_options: [idle_timeout: :infinity, reset_idle_timeout_on_send: true]]}
    ]

    Supervisor.start_link(children, strategy: :one_for_one, name: Hive.Supervisor)
  end

  defp port, do: String.to_integer(System.get_env("PORT", "4100"))
end
