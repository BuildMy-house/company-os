import Config

# Runs before the application starts, so a bad URL fails before Hive.Work connects.
Code.require_file("test_db_guard.exs", __DIR__)
Hive.TestDbGuard.check!(System.get_env("COMPANY_DATABASE_URL"))
