#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Launch the GUI with no arguments, otherwise run headless CLI control."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from lightstick_demo.cli import main as cli_main

        return cli_main(sys.argv[1:])

    # Keep tkinter out of CLI processes, including packaged CLI entry points.
    from lightstick_demo.ui import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
