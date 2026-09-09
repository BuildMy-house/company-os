#!/usr/bin/env python3
"""
companyd CLI tool wrapper — delegates to company_ops.companyd_cli module.

This script provides CLI access to deployment management and status queries.
For programmatic access, import directly from company_ops.companyd_cli.
"""

import sys
from company_ops.companyd_cli import main

if __name__ == "__main__":
    sys.exit(main())
