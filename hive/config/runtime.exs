import Config

config :hive,
  engineering_a2a_url: System.get_env("ENGINEERING_A2A_URL", "http://engineering-agent:8001"),
  agent_card: %{
    name: System.get_env("HIVE_NAME", "buildmy.house Hive"),
    description:
      System.get_env("HIVE_DESCRIPTION", "Coordinates work between buildmy.house agents"),
    url: System.get_env("HIVE_URL", "http://hive-coordinator:4100")
  }
