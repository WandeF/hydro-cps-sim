#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entry point for the persistent closed-loop runtime."""
from __future__ import annotations

from .persistent_closed_loop import main


if __name__ == "__main__":
    raise SystemExit(main())
